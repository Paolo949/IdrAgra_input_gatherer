from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..manifest import Manifest
from ..models import BoundingBox


BASE_URL = "https://maps.isric.org/mapserv"
PROPERTIES = ("sand", "silt", "clay", "cfvo", "soc", "bdod")
DEPTHS = ("0-5cm", "5-15cm", "15-30cm", "30-60cm", "60-100cm", "100-200cm")
WGS84_URI = "http://www.opengis.net/def/crs/EPSG/0/4326"
SOILGRIDS_URI = "http://www.opengis.net/def/crs/EPSG/0/152160"
SOILGRIDS_CRS = "ESRI:54052"


@dataclass(frozen=True)
class SoilGridsJob:
    property_name: str
    depth: str
    url: str

    @property
    def coverage_id(self) -> str:
        return f"{self.property_name}_{self.depth}_mean"

    @property
    def target_name(self) -> str:
        return f"{self.coverage_id}.tif"


def plan_jobs(
    bbox: BoundingBox,
    *,
    properties: Iterable[str] = PROPERTIES,
) -> list[SoilGridsJob]:
    jobs = []
    for property_name in properties:
        if property_name not in PROPERTIES:
            raise ValueError(f"unsupported SoilGrids property: {property_name}")
        for depth in DEPTHS:
            coverage_id = f"{property_name}_{depth}_mean"
            query = [
                ("map", f"/map/{property_name}.map"),
                ("SERVICE", "WCS"),
                ("VERSION", "2.0.1"),
                ("REQUEST", "GetCoverage"),
                ("COVERAGEID", coverage_id),
                ("FORMAT", "GEOTIFF_INT16"),
                ("SUBSET", f"X({bbox.west},{bbox.east})"),
                ("SUBSET", f"Y({bbox.south},{bbox.north})"),
                ("SUBSETTINGCRS", WGS84_URI),
                ("OUTPUTCRS", SOILGRIDS_URI),
            ]
            jobs.append(SoilGridsJob(property_name, depth, BASE_URL + "?" + urlencode(query)))
    return jobs


def fetch(
    root: str | Path,
    bbox: BoundingBox,
    *,
    properties: Iterable[str] = PROPERTIES,
    is_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int, Path], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    opener=urlopen,
) -> list[Path]:
    root_path = Path(root).resolve()
    output_dir = root_path / "raw" / "soil" / "soilgrids"
    output_dir.mkdir(parents=True, exist_ok=True)
    jobs = plan_jobs(bbox, properties=properties)
    manifest = Manifest(root_path)
    outputs = []

    for index, job in enumerate(jobs, start=1):
        if is_cancelled is not None and is_cancelled():
            break
        target = output_dir / job.target_name
        if target.exists() and target.stat().st_size > 0:
            if on_status:
                on_status(f"Reusing {job.coverage_id}.")
        else:
            if on_status:
                on_status(f"Downloading {job.coverage_id} ({index}/{len(jobs)}).")
            temporary = target.with_suffix(".tif.part")
            request = Request(job.url, headers={"User-Agent": "IdrAgraGather/0.7"})
            try:
                with opener(request, timeout=180) as response, temporary.open("wb") as stream:
                    content_type = response.headers.get("Content-Type", "")
                    if "xml" in content_type.lower():
                        detail = response.read(4096).decode("utf-8", errors="replace")
                        raise RuntimeError(f"SoilGrids WCS rejected {job.coverage_id}: {detail}")
                    shutil.copyfileobj(response, stream)
                temporary.replace(target)
            finally:
                if temporary.exists():
                    temporary.unlink()

        if ensure_raster_crs(target) and on_status:
            on_status(f"Assigned the SoilGrids {SOILGRIDS_CRS} CRS to {job.coverage_id}.")

        manifest.add_asset(
            target,
            category="soil",
            provider="isric-soilgrids-wcs",
            dataset=job.coverage_id,
            source=job.url,
            request={"bbox": bbox.as_dict(), "property": job.property_name,
                     "depth": job.depth, "statistic": "mean"},
        )
        outputs.append(target)
        if on_progress:
            on_progress(index, len(jobs), target)
    return outputs


def ensure_raster_crs(path: str | Path) -> bool:
    """Embed the published SoilGrids CRS when a WCS TIFF omits it.

    Returns ``True`` only when the file was changed. GDAL remains an optional
    dependency for the acquisition core; the QGIS plugin always provides it.
    """

    try:
        from osgeo import gdal, osr

        gdal.UseExceptions()
        osr.UseExceptions()
        dataset = gdal.Open(str(path), gdal.GA_Update)
        if dataset is None or dataset.RasterCount < 1:
            return False
        if (dataset.GetProjectionRef() or "").strip():
            dataset = None
            return False
        spatial_reference = osr.SpatialReference()
        spatial_reference.SetFromUserInput(SOILGRIDS_CRS)
        dataset.SetProjection(spatial_reference.ExportToWkt())
        dataset.FlushCache()
        dataset = None
        return True
    except (ImportError, OSError, RuntimeError):
        return False
