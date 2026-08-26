import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from .era5_normalize import (
    DAILY_FIELDS,
    DailySlice,
    NormalizationResult,
    WIND_10M_TO_2M,
    write_daily_geopackage,
)
from .manifest import Manifest
from .models import BoundingBox, DateWindow
from .providers.eobs import DATASET, VARIABLES, VERSION, _buffered_grid_bounds


OUTPUT_NAME = "weather_daily_points.gpkg"
UTC = timezone.utc
RADIATION_W_M2_TO_MJ_M2_DAY = 0.0864
CF_TIME = re.compile(r"^\s*(days|hours|seconds)\s+since\s+(.+?)\s*$", re.IGNORECASE)


# Crop E-OBS ensemble means and write the canonical daily weather layer.
def normalize_eobs_files(
    source_paths: Iterable[str | Path],
    output_root: str | Path,
    bbox: BoundingBox,
    window: DateWindow,
    *,
    on_status: Callable[[str], None] | None = None,
) -> NormalizationResult:
    paths = tuple(Path(path).resolve() for path in source_paths)
    if not paths:
        raise ValueError("at least one E-OBS NetCDF file is required")
    if on_status is not None:
        on_status(f"Reading and subsetting {len(paths)} E-OBS NetCDF file(s).")

    raw, latitudes, longitudes, available_window = read_eobs_daily(paths, bbox, window)
    daily = _to_canonical_daily(raw, available_window)
    for item in daily:
        unavailable = [field for field in DAILY_FIELDS if not np.any(np.isfinite(item.values[field]))]
        if unavailable:
            raise ValueError(
                f"E-OBS has no published {', '.join(unavailable)} data for {item.day}. "
                "Running-year files can lag behind the current date."
            )
    valid_locations = _complete_location_mask(daily)
    location_count = int(np.count_nonzero(valid_locations))
    if location_count == 0:
        raise ValueError(
            "E-OBS has no complete land grid points for all requested variables in or around the selected study area."
        )

    warnings = []
    if available_window != window:
        warnings.append(
            "E-OBS provisional data do not cover the full requested interval "
            f"{window.start} to {window.end}; normalized the common available "
            f"interval {available_window.start} to {available_window.end}."
        )
    warnings.extend(
        (
            "E-OBS provides daily mean relative humidity only; rhmin_pct and "
            "rhmax_pct were estimated from RHmean, Tmin, and Tmax using a "
            "constant-actual-vapour-pressure approximation.",
            "E-OBS is land-only; grid points with any missing requested daily value were excluded from the normalized layer.",
        )
    )
    output = Path(output_root).resolve() / "weather" / OUTPUT_NAME
    output.parent.mkdir(parents=True, exist_ok=True)
    if on_status is not None:
        on_status(f"Writing {len(daily)} daily rows for each of {location_count} complete E-OBS grid points.")
    write_daily_geopackage(
        output,
        latitudes,
        longitudes,
        daily,
        valid_locations=valid_locations,
        location_prefix="eobs",
    )

    Manifest(Path(output_root)).add_asset(
        output,
        category="weather",
        provider="idragather",
        dataset=f"{DATASET} {VERSION} daily IdrAgra weather",
        source="; ".join(str(path) for path in paths),
        request={
            "bbox": bbox.as_dict(),
            "requested_date_window": window.as_dict(),
            "date_window": available_window.as_dict(),
            "fields": list(DAILY_FIELDS),
            "wind_height_conversion": "FAO-56 logarithmic 10 m to 2 m",
            "radiation_conversion": "daily mean W/m2 multiplied by 0.0864",
            "humidity_extrema": "estimated from RHmean, Tmin, Tmax",
            "warnings": warnings,
        },
    )
    return NormalizationResult(output, tuple(warnings), location_count, len(daily))


def read_eobs_daily(
    paths: Iterable[Path],
    bbox: BoundingBox,
    window: DateWindow,
) -> tuple[dict[str, dict[date, np.ndarray]], np.ndarray, np.ndarray, DateWindow]:
    try:
        from osgeo import gdal
    except ImportError as exc:
        raise RuntimeError("E-OBS normalization requires GDAL's Python bindings in QGIS") from exc

    gdal.UseExceptions()
    values: dict[str, dict[date, np.ndarray]] = {name: {} for name in VARIABLES}
    output_latitudes: np.ndarray | None = None
    output_longitudes: np.ndarray | None = None

    for path in paths:
        dataset = gdal.OpenEx(str(path), gdal.OF_MULTIDIM_RASTER)
        if dataset is None or dataset.GetRootGroup() is None:
            raise ValueError(f"GDAL could not open E-OBS NetCDF: {path}")
        root = dataset.GetRootGroup()
        variable_name = _variable_from_root(root, path)
        latitudes = _read_coordinate(root, "latitude")
        longitudes = _read_coordinate(root, "longitude")
        raw_time, time_variable = _read_coordinate_with_array(root, "time")
        dates = _decode_cf_dates(raw_time, time_variable.GetUnit())

        date_indices = [index for index, day in enumerate(dates) if window.start <= day <= window.end]
        if not date_indices:
            continue
        if date_indices != list(range(date_indices[0], date_indices[-1] + 1)):
            raise ValueError(f"E-OBS time coordinate is not contiguous in {path}")

        row_indices, column_indices = _spatial_indices(latitudes, longitudes, bbox)
        selected_latitudes = latitudes[row_indices]
        selected_longitudes = longitudes[column_indices]
        if output_latitudes is None or output_longitudes is None:
            output_latitudes = selected_latitudes
            output_longitudes = selected_longitudes
        # Published variables differ by up to about 0.00014 degrees in their
        # encoding of otherwise nominally identical grid centres.
        elif not np.allclose(output_latitudes, selected_latitudes, rtol=0.0, atol=1e-3) or not np.allclose(
            output_longitudes, selected_longitudes, rtol=0.0, atol=1e-3
        ):
            raise ValueError("E-OBS files do not use the same selected coordinate grid")

        array = root.OpenMDArray(variable_name)
        data = _read_subset(
            array,
            date_indices[0],
            len(date_indices),
            row_indices,
            column_indices,
            variable_name,
        )
        for offset, time_index in enumerate(date_indices):
            day = dates[time_index]
            if day in values[variable_name]:
                raise ValueError(f"duplicate E-OBS {variable_name} value for {day}")
            values[variable_name][day] = data[offset]

    if output_latitudes is None or output_longitudes is None:
        raise ValueError("none of the E-OBS files cover the requested date window")
    available_window = _common_date_window(
        {name: set(values[name]) for name in VARIABLES},
        context="in the requested window",
    )
    return values, output_latitudes, output_longitudes, available_window


# Return the date interval present in every staged E-OBS variable.
def common_date_coverage(paths: Iterable[str | Path]) -> DateWindow:
    try:
        from osgeo import gdal
    except ImportError as exc:
        raise RuntimeError("E-OBS coverage inspection requires GDAL in QGIS") from exc

    gdal.UseExceptions()
    dates_by_variable: dict[str, set[date]] = {name: set() for name in VARIABLES}
    for item in paths:
        path = Path(item)
        dataset = gdal.OpenEx(str(path), gdal.OF_MULTIDIM_RASTER)
        if dataset is None or dataset.GetRootGroup() is None:
            raise ValueError(f"GDAL could not inspect E-OBS NetCDF: {path}")
        root = dataset.GetRootGroup()
        variable_name = _variable_from_root(root, path)
        raw_time, time_variable = _read_coordinate_with_array(root, "time")
        dates_by_variable[variable_name].update(_decode_cf_dates(raw_time, time_variable.GetUnit()))
    return _common_date_window(dates_by_variable, context="in the staged files")


def _common_date_window(dates_by_variable: dict[str, set[date]], *, context: str) -> DateWindow:
    common = set.intersection(*(dates_by_variable[name] for name in VARIABLES))
    if not common:
        raise ValueError(f"the E-OBS variables have no common dates {context}")
    window = DateWindow(min(common), max(common))
    expected = set(_days(window))
    for variable_name in VARIABLES:
        missing = sorted(expected - dates_by_variable[variable_name])
        if missing:
            raise ValueError(
                f"E-OBS {variable_name.upper()} has {len(missing)} internal missing day(s); first missing date: {missing[0]}"
            )
    return window


def _to_canonical_daily(raw: dict[str, dict[date, np.ndarray]], window: DateWindow) -> list[DailySlice]:
    result: list[DailySlice] = []
    for day in _days(window):
        # Reconstructs daily minimum and maximum humidity using FAO-56's approach (equation 19)
        # Hourly temperatures would improve this reconstruction; a future version
        # could supplement E-OBS with ERA5 hourly temperatures.
        tmin = raw["tn"][day]
        tmax = raw["tx"][day]
        rhmean = raw["hu"][day]
        saturation_min = _saturation_vapour_pressure(tmin)
        saturation_max = _saturation_vapour_pressure(tmax)
        actual = (rhmean / 100.0) * (saturation_min + saturation_max) / 2.0
        rhmax = np.clip(100.0 * actual / saturation_min, 0.0, 100.0)
        rhmin = np.clip(100.0 * actual / saturation_max, 0.0, 100.0)

        result.append(
            DailySlice(
                day,
                {
                    "tmax_c": tmax,
                    "tmin_c": tmin,
                    "rhmax_pct": rhmax,
                    "rhmin_pct": rhmin,
                    "wind2m_m_s": raw["fg"][day] * WIND_10M_TO_2M,
                    "solar_rad_mj_m2_day": raw["qq"][day] * RADIATION_W_M2_TO_MJ_M2_DAY,
                    "precip_mm": raw["rr"][day],
                },
            )
        )
    return result


def _complete_location_mask(daily: list[DailySlice]) -> np.ndarray:
    first = next(iter(daily[0].values.values()))
    valid = np.ones(first.shape, dtype=bool)
    for item in daily:
        for field in DAILY_FIELDS:
            valid &= np.isfinite(item.values[field])
    return valid


def _saturation_vapour_pressure(temperature_c: np.ndarray) -> np.ndarray:
    return 0.6108 * np.exp(17.27 * temperature_c / (temperature_c + 237.3))


def _variable_from_root(root, path: Path) -> str:
    array_names = set(root.GetMDArrayNames())
    available = [name for name in VARIABLES if name in array_names]
    if len(available) != 1:
        raise ValueError(f"expected one E-OBS weather variable in {path}, found: {', '.join(available) or 'none'}")
    return available[0]


def _read_coordinate(root, name: str) -> np.ndarray:
    values, _ = _read_coordinate_with_array(root, name)
    return values


def _read_coordinate_with_array(root, name: str):
    variable = root.OpenMDArray(name)
    if variable is None:
        raise ValueError(f"E-OBS NetCDF is missing coordinate {name!r}")
    values = np.asarray(variable.ReadAsArray()).reshape(-1)
    if values.size == 0 or not np.all(np.isfinite(values)):
        raise ValueError(f"E-OBS coordinate {name!r} is empty or invalid")
    return values, variable


def _decode_cf_dates(values: np.ndarray, unit: str | None) -> tuple[date, ...]:
    match = CF_TIME.match(unit or "")
    if match is None:
        raise ValueError(f"unsupported E-OBS time unit: {unit!r}")
    scale = {"days": 86400.0, "hours": 3600.0, "seconds": 1.0}[match.group(1).lower()]
    origin_text = match.group(2).strip().replace("Z", "+00:00")
    try:
        origin = datetime.fromisoformat(origin_text)
    except ValueError:
        origin = datetime.combine(date.fromisoformat(origin_text.split()[0]), datetime.min.time())
    if origin.tzinfo is None:
        origin = origin.replace(tzinfo=UTC)
    return tuple((origin + timedelta(seconds=float(value) * scale)).date() for value in values)


def _spatial_indices(latitudes: np.ndarray, longitudes: np.ndarray, bbox: BoundingBox) -> tuple[np.ndarray, np.ndarray]:
    west, south, east, north = _buffered_grid_bounds(bbox)
    rows = np.flatnonzero((latitudes >= south - 1e-9) & (latitudes <= north + 1e-9))
    columns = np.flatnonzero((longitudes >= west - 1e-9) & (longitudes <= east + 1e-9))
    if rows.size == 0 or columns.size == 0:
        raise ValueError("the selected AOI does not contain an E-OBS 0.1-degree grid point")
    if not np.all(np.diff(rows) == 1) or not np.all(np.diff(columns) == 1):
        raise ValueError("selected E-OBS coordinate indices are not contiguous")
    return rows, columns


def _read_subset(
    variable,
    time_start: int,
    time_count: int,
    rows: np.ndarray,
    columns: np.ndarray,
    name: str,
) -> np.ndarray:
    dimensions = [dimension.GetName().rsplit("/", 1)[-1] for dimension in variable.GetDimensions()]
    wanted = {
        "time": (time_start, time_count),
        "latitude": (int(rows[0]), len(rows)),
        "longitude": (int(columns[0]), len(columns)),
    }
    if set(dimensions) != set(wanted):
        raise ValueError(f"unexpected dimensions for E-OBS {name!r}: {dimensions}")
    starts = [wanted[dimension][0] for dimension in dimensions]
    counts = [wanted[dimension][1] for dimension in dimensions]
    data = np.asarray(variable.ReadAsArray(starts, counts), dtype=np.float64)
    data = np.transpose(data, [dimensions.index(item) for item in ("time", "latitude", "longitude")])
    missing = (np.abs(data) > 1e20) | (data <= -999.0)
    scale = variable.GetScale()
    offset = variable.GetOffset()
    if scale is not None:
        data *= float(scale)
    if offset is not None:
        data += float(offset)
    data[missing] = np.nan
    return data


def _days(window: DateWindow):
    current = window.start
    while current <= window.end:
        yield current
        current += timedelta(days=1)
