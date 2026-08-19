from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import numpy as np

from .manifest import Manifest
from .models import DateWindow


REQUIRED_VARIABLES = ("t2m", "d2m", "u10", "v10", "ssrd", "tp")
DAILY_FIELDS = (
    "tmax_c",
    "tmin_c",
    "rhmax_pct",
    "rhmin_pct",
    "wind2m_m_s",
    "solar_rad_mj_m2_day",
    "precip_mm",
)
OUTPUT_LAYER = "weather_daily_points"
UTC = timezone.utc
HOUR = timedelta(hours=1)
WIND_10M_TO_2M = 4.87 / math.log(67.8 * 10.0 - 5.42)


@dataclass(frozen=True)
class Era5Cube:
    times: tuple[datetime, ...]
    latitudes: np.ndarray
    longitudes: np.ndarray
    values: dict[str, np.ndarray]


@dataclass(frozen=True)
class DailySlice:
    day: date
    values: dict[str, np.ndarray]


@dataclass(frozen=True)
class NormalizationResult:
    path: Path
    warnings: tuple[str, ...]
    location_count: int
    day_count: int


def normalize_era5_files(
    source_paths: Iterable[str | Path],
    output_root: str | Path,
    window: DateWindow,
    *,
    timezone_name: str = "Europe/Rome",
    on_status: Callable[[str], None] | None = None,
) -> NormalizationResult:
    """Convert raw ERA5-Land NetCDF files to a daily IdrAgra-ready GeoPackage."""

    paths = tuple(Path(path).resolve() for path in source_paths)
    if not paths:
        raise ValueError("at least one ERA5-Land NetCDF file is required")
    if on_status is not None:
        on_status(f"Reading {len(paths)} ERA5-Land NetCDF file(s).")

    cube = read_era5_cube(paths)
    daily, warnings = aggregate_daily(cube, window, timezone_name=timezone_name)
    output = Path(output_root).resolve() / "weather" / "weather_daily_points.gpkg"
    output.parent.mkdir(parents=True, exist_ok=True)
    if on_status is not None:
        on_status(
            f"Writing {len(daily)} daily rows for each of "
            f"{cube.latitudes.size * cube.longitudes.size} grid points."
        )
    write_daily_geopackage(output, cube.latitudes, cube.longitudes, daily)

    Manifest(Path(output_root)).add_asset(
        output,
        category="weather",
        provider="idragather",
        dataset="ERA5-Land daily IdrAgra weather",
        source="; ".join(str(path) for path in paths),
        request={
            "timezone": timezone_name,
            "edge_policy": "nearest available hourly value",
            "fields": list(DAILY_FIELDS),
            "warnings": warnings,
        },
    )
    return NormalizationResult(
        output,
        tuple(warnings),
        cube.latitudes.size * cube.longitudes.size,
        len(daily),
    )


def aggregate_daily(
    cube: Era5Cube,
    window: DateWindow,
    *,
    timezone_name: str = "Europe/Rome",
) -> tuple[list[DailySlice], list[str]]:
    """Aggregate a validated hourly cube into the seven IdrAgra daily fields."""

    _validate_cube(cube)
    zone = ZoneInfo(timezone_name)
    order = np.argsort(np.array([item.timestamp() for item in cube.times]))
    times: tuple[datetime, ...] = tuple(cube.times[index].astimezone(UTC) for index in order)
    values: dict[str, np.ndarray] = {name: array[order] for name, array in cube.values.items()}
    if len(set(times)) != len(times):
        raise ValueError("ERA5 input contains duplicate valid_time values")
    for previous, current in zip(times, times[1:]):
        if current - previous != HOUR:
            raise ValueError(f"ERA5 input has a non-hourly gap between {previous} and {current}")

    temperature_c = values["t2m"] - 273.15
    dewpoint_c = values["d2m"] - 273.15
    relative_humidity = _relative_humidity(temperature_c, dewpoint_c)
    wind2m = np.hypot(values["u10"], values["v10"]) * WIND_10M_TO_2M

    warnings: list[str] = []
    radiation = _deaccumulate_era5_land(values["ssrd"], times, "ssrd", warnings)
    precipitation = _deaccumulate_era5_land(values["tp"], times, "tp", warnings)
    time_index = {stamp: index for index, stamp in enumerate(times)}

    result: list[DailySlice] = []
    current_day = window.start
    while current_day <= window.end:
        instant_hours, accumulation_hours = _daily_utc_hours(current_day, zone)
        instant_indices = _indices_with_edge_fill(
            instant_hours, times, time_index, current_day, "instantaneous", warnings
        )
        accumulation_indices = _indices_with_edge_fill(
            accumulation_hours, times, time_index, current_day, "accumulated", warnings
        )

        instant = np.asarray(instant_indices, dtype=int)
        accumulated = np.asarray(accumulation_indices, dtype=int)
        daily_values = {
            "tmax_c": np.max(temperature_c[instant], axis=0),
            "tmin_c": np.min(temperature_c[instant], axis=0),
            "rhmax_pct": np.max(relative_humidity[instant], axis=0),
            "rhmin_pct": np.min(relative_humidity[instant], axis=0),
            "wind2m_m_s": np.mean(wind2m[instant], axis=0),
            "solar_rad_mj_m2_day": np.sum(radiation[accumulated], axis=0) / 1_000_000.0,
            "precip_mm": np.sum(precipitation[accumulated], axis=0) * 1_000.0,
        }
        for field, array in daily_values.items():
            if not np.all(np.isfinite(array)):
                raise ValueError(f"non-finite {field} value on {current_day}")
        result.append(DailySlice(current_day, daily_values))
        current_day += timedelta(days=1)
    return result, list(dict.fromkeys(warnings))


def read_era5_cube(paths: Iterable[Path]) -> Era5Cube:
    """Read CDS NetCDF-4 files through GDAL's multidimensional API."""

    try:
        from osgeo import gdal
    except ImportError as exc:
        raise RuntimeError("ERA5 normalization requires GDAL's Python bindings in QGIS") from exc

    gdal.UseExceptions()
    pieces: list[Era5Cube] = [_read_era5_file(path, gdal) for path in paths]
    first = pieces[0]
    for piece in pieces[1:]:
        if not np.array_equal(piece.latitudes, first.latitudes) or not np.array_equal(
            piece.longitudes, first.longitudes
        ):
            raise ValueError("ERA5 NetCDF files do not use the same latitude/longitude grid")
    times = tuple(stamp for piece in pieces for stamp in piece.times)
    values = {
        name: np.concatenate([piece.values[name] for piece in pieces], axis=0)
        for name in REQUIRED_VARIABLES
    }
    return Era5Cube(times, first.latitudes, first.longitudes, values)


def _read_era5_file(path: Path, gdal) -> Era5Cube:
    dataset = gdal.OpenEx(str(path), gdal.OF_MULTIDIM_RASTER)
    if dataset is None or dataset.GetRootGroup() is None:
        raise ValueError(f"GDAL could not open ERA5 NetCDF as a multidimensional dataset: {path}")
    root = dataset.GetRootGroup()
    latitudes = _read_coordinate(root, "latitude")
    longitudes = _read_coordinate(root, "longitude")
    raw_times = _read_coordinate(root, "valid_time")
    times = tuple(datetime.fromtimestamp(float(value), UTC) for value in raw_times)
    values = {
        name: _read_weather_array(root, name, len(times), len(latitudes), len(longitudes))
        for name in REQUIRED_VARIABLES
    }
    return Era5Cube(times, latitudes, longitudes, values)


def _read_coordinate(root, name: str) -> np.ndarray:
    variable = root.OpenMDArray(name)
    if variable is None:
        raise ValueError(f"NetCDF is missing coordinate {name!r}")
    values = np.asarray(variable.ReadAsArray()).reshape(-1)
    if values.size == 0 or not np.all(np.isfinite(values)):
        raise ValueError(f"NetCDF coordinate {name!r} is empty or invalid")
    return values


def _read_weather_array(root, name: str, nt: int, ny: int, nx: int) -> np.ndarray:
    variable = root.OpenMDArray(name)
    if variable is None:
        raise ValueError(f"NetCDF is missing ERA5 variable {name!r}")
    data = np.asarray(variable.ReadAsArray(), dtype=np.float64)
    dimensions = [dimension.GetName().rsplit("/", 1)[-1] for dimension in variable.GetDimensions()]

    for axis in range(len(dimensions) - 1, -1, -1):
        if dimensions[axis] in {"valid_time", "latitude", "longitude"}:
            continue
        if data.shape[axis] != 1:
            raise ValueError(
                f"unsupported non-singleton {dimensions[axis]!r} dimension in {name!r}"
            )
        data = np.take(data, 0, axis=axis)
        dimensions.pop(axis)

    expected = ["valid_time", "latitude", "longitude"]
    if sorted(dimensions) != sorted(expected):
        raise ValueError(f"unexpected dimensions for {name!r}: {dimensions}")
    data = np.transpose(data, [dimensions.index(item) for item in expected])
    if data.shape != (nt, ny, nx):
        raise ValueError(f"unexpected shape for {name!r}: {data.shape}")
    data[np.abs(data) > 1e30] = np.nan
    return data


def write_daily_geopackage(
    path: Path,
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    daily: list[DailySlice],
) -> None:
    try:
        from osgeo import ogr, osr
    except ImportError as exc:
        raise RuntimeError("GeoPackage output requires GDAL/OGR in QGIS") from exc

    temporary = path.with_name(path.stem + ".tmp.gpkg")
    temporary.unlink(missing_ok=True)
    driver = ogr.GetDriverByName("GPKG")
    database = driver.CreateDataSource(str(temporary))
    if database is None:
        raise RuntimeError(f"could not create {temporary}")

    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(4326)
    layer = database.CreateLayer(OUTPUT_LAYER, spatial_reference, ogr.wkbPoint)
    _add_ogr_field(layer, ogr, "date", ogr.OFTString, width=10)
    _add_ogr_field(layer, ogr, "location_id", ogr.OFTString, width=40)
    for field in DAILY_FIELDS:
        _add_ogr_field(layer, ogr, field, ogr.OFTReal)

    location_ids: dict[tuple[int, int], str] = {}
    for row, latitude in enumerate(latitudes):
        for column, longitude in enumerate(longitudes):
            location_id = f"era5_{float(latitude):.4f}_{float(longitude):.4f}"
            location_ids[(row, column)] = location_id
    database.StartTransaction()
    try:
        for item in daily:
            for row in range(len(latitudes)):
                for column in range(len(longitudes)):
                    feature = ogr.Feature(layer.GetLayerDefn())
                    feature.SetField("date", item.day.isoformat())
                    feature.SetField("location_id", location_ids[(row, column)])
                    for field in DAILY_FIELDS:
                        feature.SetField(field, float(item.values[field][row, column]))
                    geometry = ogr.Geometry(ogr.wkbPoint)
                    geometry.AddPoint(
                        float(longitudes[column]), float(latitudes[row])
                    )
                    feature.SetGeometry(geometry)
                    if layer.CreateFeature(feature) != 0:
                        raise RuntimeError("failed to write daily weather row")
        database.CommitTransaction()
        database.ExecuteSQL(
            "CREATE INDEX IF NOT EXISTS weather_daily_points_location_date "
            "ON weather_daily_points (location_id, date)"
        )
    except Exception:
        database.RollbackTransaction()
        raise
    finally:
        database = None

    path.unlink(missing_ok=True)
    temporary.replace(path)


def _add_ogr_field(layer, ogr, name, field_type, *, width: int | None = None) -> None:
    field = ogr.FieldDefn(name, field_type)
    if width is not None:
        field.SetWidth(width)
    if layer.CreateField(field) != 0:
        raise RuntimeError(f"failed to create GeoPackage field {name!r}")


def _validate_cube(cube: Era5Cube) -> None:
    if not cube.times:
        raise ValueError("ERA5 input has no time values")
    if cube.latitudes.ndim != 1 or cube.longitudes.ndim != 1:
        raise ValueError("ERA5 latitude and longitude coordinates must be one-dimensional")
    expected_shape = (len(cube.times), len(cube.latitudes), len(cube.longitudes))
    missing = sorted(set(REQUIRED_VARIABLES) - set(cube.values))
    if missing:
        raise ValueError(f"ERA5 input is missing variables: {', '.join(missing)}")
    for name in REQUIRED_VARIABLES:
        if cube.values[name].shape != expected_shape:
            raise ValueError(f"{name} has shape {cube.values[name].shape}, expected {expected_shape}")
        if not np.all(np.isfinite(cube.values[name])):
            raise ValueError(f"{name} contains missing or non-finite values")


def _relative_humidity(temperature_c: np.ndarray, dewpoint_c: np.ndarray) -> np.ndarray:
    vapour = np.exp(17.27 * dewpoint_c / (dewpoint_c + 237.3))
    saturation = np.exp(17.27 * temperature_c / (temperature_c + 237.3))
    return np.clip(100.0 * vapour / saturation, 0.0, 100.0)


def _deaccumulate_era5_land(
    values: np.ndarray,
    times: tuple[datetime, ...],
    name: str,
    warnings: list[str],
) -> np.ndarray:
    increments = np.empty_like(values)
    increments[0] = np.nan
    for index in range(1, len(times)):
        if times[index].hour == 1:
            increments[index] = values[index]
        else:
            increments[index] = values[index] - values[index - 1]

    if len(times) < 2:
        raise ValueError(f"not enough values to de-accumulate {name}")
    increments[0] = increments[1]
    warnings.append(
        f"{name}: the first hourly increment was estimated from the nearest available hour."
    )
    # CDS stores these cumulative fields as float32. Subtraction can therefore
    # produce tiny negative residues (the supplied ssrd sample reaches -4
    # J/m²), even though the physical hourly increment is zero. The tolerances
    # are negligible after conversion: 0.0001 mm rain or 0.0001 MJ/m² solar.
    tolerance = 1e-7 if name == "tp" else 100.0
    if np.any(increments < -tolerance):
        minimum = float(np.min(increments))
        raise ValueError(f"{name} de-accumulation produced a negative increment ({minimum:g})")
    increments[increments < 0] = 0
    return increments


def _daily_utc_hours(day: date, zone: ZoneInfo) -> tuple[list[datetime], list[datetime]]:
    start_local = datetime.combine(day, time.min, tzinfo=zone)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    start_utc = start_local.astimezone(UTC)
    end_utc = end_local.astimezone(UTC)
    instantaneous: list[datetime] = []
    cursor = start_utc
    while cursor < end_utc:
        instantaneous.append(cursor)
        cursor += HOUR
    # Accumulated values are stamped at the end of their one-hour interval.
    accumulated = [stamp + HOUR for stamp in instantaneous]
    return instantaneous, accumulated


def _indices_with_edge_fill(
    expected: list[datetime],
    available: tuple[datetime, ...],
    time_index: dict[datetime, int],
    day: date,
    kind: str,
    warnings: list[str],
) -> list[int]:
    indices: list[int] = []
    filled = 0
    for stamp in expected:
        index = time_index.get(stamp)
        if index is None:
            if stamp < available[0]:
                index = 0
            elif stamp > available[-1]:
                index = len(available) - 1
            else:
                raise ValueError(f"missing interior ERA5 hour {stamp.isoformat()}")
            filled += 1
        indices.append(index)
    if filled:
        warnings.append(
            f"{day}: filled {filled} missing edge {kind} hour(s) from the nearest available hour."
        )
    return indices
