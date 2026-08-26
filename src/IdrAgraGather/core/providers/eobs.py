from dataclasses import dataclass
from datetime import date
import hashlib
import os
from pathlib import Path
from typing import Callable, Iterable

from ..manifest import Manifest
from ..models import BoundingBox, DateWindow


DATASET = "E-OBS daily gridded observations"
VERSION = "33.0e"
GRID_RESOLUTION = "0.1deg"
GRID_DEGREES = 0.1
SPATIAL_BUFFER_CELLS = 1
BASE_URL = "https://knmi-ecad-assets-prd.s3.amazonaws.com/ensembles/data"
FINAL_PERIODS = ((1950, 1964), (1965, 1979), (1980, 1994), (1995, 2010), (2011, 2025))
VARIABLES = ("tn", "tx", "rr", "hu", "fg", "qq")
# E-OBS wind is only published from 1980 onward. Wind is required by the
# canonical IdrAgra weather table, so earlier windows cannot be normalized.
EARLIEST_COMPLETE_YEAR = 1980


@dataclass(frozen=True)
class EobsJob:
    variable: str
    period: str
    target_name: str
    url: str
    provisional: bool = False


# Save AOI/date subsets using range reads against official NetCDF files.
def fetch(
    output_root: str | Path,
    bbox: BoundingBox,
    window: DateWindow,
    *,
    overwrite: bool = False,
    is_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int, Path], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    subsetter=None,
) -> list[Path]:
    root = Path(output_root).resolve()
    destination_dir = root / "raw" / "weather" / "eobs"
    destination_dir.mkdir(parents=True, exist_ok=True)
    jobs = plan_jobs(bbox, window)
    if subsetter is None:
        subsetter = _subset_remote_job
    manifest = Manifest(root)
    manifest.configure(aoi=bbox.as_dict(), date_window=window.as_dict())
    previous_assets = [
        root / asset["path"]
        for asset in manifest.read().get("assets", [])
        if asset.get("provider") == "eobs-knmi" and Path(asset["path"]).suffix == ".nc"
    ]
    subset_west, subset_south, subset_east, subset_north = _buffered_grid_bounds(bbox)
    outputs: list[Path] = []

    for index, job in enumerate(jobs, start=1):
        if is_cancelled is not None and is_cancelled():
            break
        destination = destination_dir / job.target_name
        # Running-year files are replaced on each monthly update, so refresh
        # them rather than silently treating a stale copy as finalized data.
        reusable = destination.exists() and destination.stat().st_size > 0 and not overwrite and not job.provisional
        if reusable:
            if on_status is not None:
                qualifier = "provisional " if job.provisional else ""
                on_status(f"Reusing {qualifier}{job.variable.upper()} {job.period}.")
        else:
            if on_status is not None:
                on_status(f"Reading and saving the {job.variable.upper()} {job.period} AOI/date subset ({index}/{len(jobs)}).")
            partial = destination.with_suffix(destination.suffix + ".part")
            partial.unlink(missing_ok=True)
            try:
                subsetter(job, partial, bbox, window, is_cancelled)
                if is_cancelled is not None and is_cancelled():
                    partial.unlink(missing_ok=True)
                    break
                if not partial.exists() or partial.stat().st_size == 0:
                    raise RuntimeError(f"E-OBS did not produce {partial}")
                partial.replace(destination)
            except Exception:
                partial.unlink(missing_ok=True)
                raise

        manifest.add_asset(
            destination,
            category="weather",
            provider="eobs-knmi",
            dataset=f"{DATASET} {VERSION} ensemble mean",
            source=job.url,
            request={
                "variable": job.variable,
                "period": job.period,
                "grid_resolution": GRID_RESOLUTION,
                "spatial_buffer_grid_cells": SPATIAL_BUFFER_CELLS,
                "subset_bbox": {
                    "crs": "EPSG:4326",
                    "west": subset_west,
                    "south": subset_south,
                    "east": subset_east,
                    "north": subset_north,
                },
                "provisional": job.provisional,
                "requested_bbox": bbox.as_dict(),
                "requested_window": window.as_dict(),
            },
        )
        outputs.append(destination)
        if on_progress is not None:
            on_progress(index, len(jobs), destination)
    if len(outputs) == len(jobs):
        current_outputs = set(outputs)
        manifest.remove_assets(path for path in previous_assets if path not in current_outputs)
    return outputs


def plan_jobs(
    bbox: BoundingBox,
    window: DateWindow,
    *,
    variables: Iterable[str] = VARIABLES,
    today: date | None = None,
) -> list[EobsJob]:
    selected = tuple(dict.fromkeys(variables))
    unknown = sorted(set(selected) - set(VARIABLES))
    if unknown:
        raise ValueError(f"unsupported E-OBS variables: {', '.join(unknown)}")
    if not selected:
        raise ValueError("at least one E-OBS variable is required")
    if window.start.year < EARLIEST_COMPLETE_YEAR:
        raise ValueError(
            "E-OBS wind speed starts in 1980; choose a period beginning in 1980 or later for a complete IdrAgra weather dataset."
        )

    current_year = (today or date.today()).year
    periods: list[tuple[str, bool]] = []
    for first, last in FINAL_PERIODS:
        if window.start.year <= last and window.end.year >= first:
            periods.append((f"{first}-{last}", False))

    if window.end.year > FINAL_PERIODS[-1][1]:
        if window.start.year <= current_year <= window.end.year:
            periods.append((str(current_year), True))
        unsupported = [
            year for year in range(max(window.start.year, FINAL_PERIODS[-1][1] + 1), window.end.year + 1) if year != current_year
        ]
        if unsupported:
            raise ValueError(
                "E-OBS finalized v33.0e ends in 2025, and only the running-year "
                f"provisional file is available after that (unsupported: {unsupported[0]})."
            )

    jobs: list[EobsJob] = []
    subset_bounds = _buffered_grid_bounds(bbox)
    for period, provisional in periods:
        for variable in selected:
            if provisional:
                target_name = f"{variable}_0.1deg_day_{period}_grid_ensmean.nc"
                url = f"{BASE_URL}/months/ens/{target_name}"
            else:
                target_name = f"{variable}_ens_mean_0.1deg_reg_{period}_v{VERSION}.nc"
                url = f"{BASE_URL}/Grid_0.1deg_reg_ensemble/{target_name}"
            signature = hashlib.sha256(
                (f"{subset_bounds}|{window.start}|{window.end}|buffer={SPATIAL_BUFFER_CELLS}").encode("ascii")
            ).hexdigest()[:12]
            local_name = f"{variable}_{period}_{signature}.nc"
            jobs.append(EobsJob(variable, period, local_name, url, provisional))
    if not jobs:
        raise ValueError("the requested dates are outside the supported E-OBS periods")
    return jobs


def _subset_remote_job(
    job: EobsJob,
    target: Path,
    bbox: BoundingBox,
    window: DateWindow,
    is_cancelled: Callable[[], bool] | None,
) -> None:
    try:
        from osgeo import gdal
    except ImportError as exc:
        raise RuntimeError("E-OBS range subsetting requires GDAL in QGIS") from exc

    gdal.UseExceptions()
    source = "/vsicurl/" + job.url
    first, last = _job_date_intersection(job, window)
    west, south, east, north = _buffered_grid_bounds(bbox)
    origin = date(1950, 1, 1)
    time_start = (first - origin).days
    time_end = (last - origin).days

    def progress(_complete, _message, _data):
        return 0 if is_cancelled is not None and is_cancelled() else 1

    previous_extensions = gdal.GetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS")
    previous_capi = gdal.GetConfigOption("GDAL_HTTP_USE_CAPI_STORE")
    gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".nc")
    if os.name == "nt" and previous_capi is None:
        # Use certificates trusted by Windows (including organization proxy
        # certificates) while retaining full HTTPS verification.
        gdal.SetConfigOption("GDAL_HTTP_USE_CAPI_STORE", "YES")
    try:
        options = gdal.MultiDimTranslateOptions(
            format="netCDF",
            subsetSpecs=[
                f"time({time_start},{time_end})",
                f"latitude({south},{north})",
                f"longitude({west},{east})",
            ],
            callback=progress,
        )
        result = gdal.MultiDimTranslate(str(target), source, options=options)
    except RuntimeError as exc:
        message = str(exc)
        if "certificate" in message.lower() or "ssl" in message.lower():
            raise RuntimeError(
                "GDAL could not verify the HTTPS certificate for the E-OBS server. "
                "Configure QGIS/GDAL to use your system or organization CA bundle; "
                "certificate verification was not disabled."
            ) from exc
        raise
    finally:
        gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", previous_extensions)
        if os.name == "nt" and previous_capi is None:
            gdal.SetConfigOption("GDAL_HTTP_USE_CAPI_STORE", None)
    if result is None and not (is_cancelled is not None and is_cancelled()):
        raise RuntimeError(f"GDAL could not subset E-OBS source {job.url}")
    result = None


# Expand the AOI by one grid spacing so the selection includes a centroid beyond every side.
def _buffered_grid_bounds(bbox: BoundingBox) -> tuple[float, float, float, float]:
    buffer_degrees = GRID_DEGREES * SPATIAL_BUFFER_CELLS
    west = max(-180.0, round(bbox.west - buffer_degrees, 12))
    south = max(-90.0, round(bbox.south - buffer_degrees, 12))
    east = min(180.0, round(bbox.east + buffer_degrees, 12))
    north = min(90.0, round(bbox.north + buffer_degrees, 12))
    return west, south, east, north


def _job_date_intersection(job: EobsJob, window: DateWindow) -> tuple[date, date]:
    if "-" in job.period:
        first_year, last_year = (int(value) for value in job.period.split("-", 1))
    else:
        first_year = last_year = int(job.period)
    first = max(window.start, date(first_year, 1, 1))
    last = min(window.end, date(last_year, 12, 31))
    return first, last
