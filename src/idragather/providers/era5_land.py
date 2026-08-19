from __future__ import annotations

import calendar
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable

from ..manifest import Manifest
from ..models import BoundingBox, DateWindow


DATASET = "reanalysis-era5-land"
VARIABLES = (
    "2m_temperature",
    "2m_dewpoint_temperature",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "surface_solar_radiation_downwards",
    "total_precipitation",
)
TIMES = tuple(f"{hour:02d}:00" for hour in range(24))
GRID_DEGREES = 0.1


@dataclass(frozen=True)
class Era5Job:
    year: int
    month: int
    target_name: str
    request: dict[str, Any]


def plan_jobs(
    bbox: BoundingBox,
    window: DateWindow,
    *,
    variables: Iterable[str] = VARIABLES,
) -> list[Era5Job]:
    selected_variables = tuple(dict.fromkeys(variables))
    unknown = sorted(set(selected_variables) - set(VARIABLES))
    if unknown:
        raise ValueError(f"unsupported ERA5-Land variables: {', '.join(unknown)}")
    if not selected_variables:
        raise ValueError("at least one variable is required")

    request_bbox = _snap_bbox_outward(bbox, GRID_DEGREES)
    jobs: list[Era5Job] = []
    for year, month in _months(window.start, window.end):
        first = max(window.start, date(year, month, 1))
        last = min(window.end, date(year, month, calendar.monthrange(year, month)[1]))
        days = [f"{day:02d}" for day in range(first.day, last.day + 1)]
        request = {
            "variable": list(selected_variables),
            "year": str(year),
            "month": f"{month:02d}",
            "day": days,
            "time": list(TIMES),
            # CDS uses north, west, south, east.
            # Snap outward to the native grid so even a very small AOI contains
            # at least one grid point. The original AOI remains in the manifest.
            "area": [
                request_bbox.north,
                request_bbox.west,
                request_bbox.south,
                request_bbox.east,
            ],
            "data_format": "netcdf",
            "download_format": "unarchived",
        }
        jobs.append(Era5Job(year, month, f"{year}-{month:02d}.nc", request))
    return jobs


def fetch(
    output_root: str | Path,
    bbox: BoundingBox,
    window: DateWindow,
    *,
    overwrite: bool = False,
    client: Any | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int, Path], None] | None = None,
    on_status: Callable[[str], None] | None = None,
) -> list[Path]:
    """Download monthly raw NetCDF files, resuming completed files."""

    if client is None:
        try:
            import cdsapi
        except ImportError as exc:
            raise RuntimeError(
                "ERA5 fetching requires the cdsapi package in QGIS and a "
                "configured %USERPROFILE%\\.cdsapirc file"
            ) from exc
        callback = _cds_log_callback(on_status)
        client = cdsapi.Client(
            quiet=True,
            progress=False,
            info_callback=callback,
            warning_callback=callback,
            error_callback=callback,
            # Debug output can contain the CDS key in some client versions.
            debug_callback=_ignore_cds_log,
        )

    root = Path(output_root).resolve()
    destination_dir = root / "raw" / "weather" / "era5_land"
    destination_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(root)
    manifest.configure(aoi=bbox.as_dict(), date_window=window.as_dict())

    outputs: list[Path] = []
    jobs = plan_jobs(bbox, window)
    for index, job in enumerate(jobs, start=1):
        if is_cancelled is not None and is_cancelled():
            break
        destination = destination_dir / job.target_name
        if destination.exists() and destination.stat().st_size > 0 and not overwrite:
            outputs.append(destination)
            manifest.add_asset(
                destination,
                category="weather",
                provider="copernicus-cds",
                dataset=DATASET,
                source="https://cds.climate.copernicus.eu/",
                request=job.request,
            )
            if on_progress is not None:
                on_progress(index, len(jobs), destination)
            continue

        partial = destination.with_suffix(destination.suffix + ".part")
        # A forced QGIS shutdown can leave an incomplete file from an earlier
        # attempt. It is not a verified resumable asset, so start clean.
        partial.unlink(missing_ok=True)
        try:
            if on_status is not None:
                on_status(f"Submitting ERA5-Land job for {job.year}-{job.month:02d}.")
            client.retrieve(DATASET, job.request, str(partial))
            if not partial.exists() or partial.stat().st_size == 0:
                raise RuntimeError(f"CDS did not produce {partial}")
            partial.replace(destination)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

        manifest.add_asset(
            destination,
            category="weather",
            provider="copernicus-cds",
            dataset=DATASET,
            source="https://cds.climate.copernicus.eu/",
            request=job.request,
        )
        outputs.append(destination)
        if on_progress is not None:
            on_progress(index, len(jobs), destination)
    return outputs


def _cds_log_callback(on_status: Callable[[str], None] | None):
    """Adapt logging-style CDS callbacks without writing to QGIS console streams."""

    def report(message, *args, **_kwargs):
        if on_status is None:
            return
        try:
            text = str(message) % args if args else str(message)
        except (TypeError, ValueError):
            text = " ".join(str(item) for item in (message, *args))
        if text.strip():
            on_status(text.strip())

    return report


def _ignore_cds_log(*_args, **_kwargs):
    pass


def write_plan(
    output_root: str | Path,
    bbox: BoundingBox,
    window: DateWindow,
) -> Path:
    """Write the exact CDS jobs so the QGIS prototype can run without credentials."""

    root = Path(output_root).resolve()
    output = root / "raw" / "weather" / "era5_land" / "era5_plan.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    jobs = plan_jobs(bbox, window)
    payload = {
        "dataset": DATASET,
        "aoi": bbox.as_dict(),
        "date_window": window.as_dict(),
        "jobs": [{"target": job.target_name, "request": job.request} for job in jobs],
    }
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    manifest = Manifest(root)
    manifest.configure(aoi=bbox.as_dict(), date_window=window.as_dict())
    manifest.add_asset(
        output,
        category="weather",
        provider="copernicus-cds",
        dataset=DATASET,
        source="https://cds.climate.copernicus.eu/",
        request={"mode": "plan-only", "job_count": len(jobs)},
    )
    return output


def _months(start: date, end: date):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1


def _snap_bbox_outward(bbox: BoundingBox, grid: float) -> BoundingBox:
    """Return a grid-aligned request extent that fully contains *bbox*."""

    if grid <= 0:
        raise ValueError("grid spacing must be positive")

    # Rounding removes binary floating-point noise from values such as 46.2.
    precision = max(0, int(round(-math.log10(grid))))
    west = round(math.floor((bbox.west + 1e-12) / grid) * grid, precision)
    south = round(math.floor((bbox.south + 1e-12) / grid) * grid, precision)
    east = round(math.ceil((bbox.east - 1e-12) / grid) * grid, precision)
    north = round(math.ceil((bbox.north - 1e-12) / grid) * grid, precision)

    # A zero-width/height grid-aligned AOI still needs one cell of coverage.
    if east <= west:
        east = round(west + grid, precision)
    if north <= south:
        north = round(south + grid, precision)
    return BoundingBox(west, south, east, north)
