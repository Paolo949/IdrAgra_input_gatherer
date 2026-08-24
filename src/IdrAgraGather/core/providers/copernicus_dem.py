import json
import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..manifest import Manifest
from ..models import BoundingBox


PROCESS_URL = "https://sh.dataspace.copernicus.eu/process/v1"
TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
    "protocol/openid-connect/token"
)
PROVIDER = "copernicus-dem-sentinel-hub"
DATASET_DOI = "https://doi.org/10.5270/ESA-c5d3d65"
DATASET_PAGE = (
    "https://dataspace.copernicus.eu/explore-data/data-collections/"
    "copernicus-contributing-missions/collections-description/COP-DEM"
)
ATTRIBUTION = {
    "COPERNICUS_30": (
        "© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 "
        "provided under COPERNICUS by the European Union and ESA; all rights reserved"
    ),
    "COPERNICUS_90": (
        "Copernicus WorldDEM-90 © DLR e.V. 2010-2014 and © Airbus Defence and "
        "Space GmbH 2014-2018 provided under COPERNICUS by the European Union "
        "and ESA; all rights reserved"
    ),
}
INSTANCES = {
    "COPERNICUS_30": {"resolution_degrees": 1.0 / 3600.0, "resolution_m": 30},
    "COPERNICUS_90": {"resolution_degrees": 3.0 / 3600.0, "resolution_m": 90},
}
MAX_TILE_PIXELS = 2500
EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: ["DEM"],
    output: { id: "default", bands: 1, sampleType: SampleType.FLOAT32 }
  };
}
function evaluatePixel(sample) {
  return [sample.DEM];
}
"""


@dataclass(frozen=True)
class CopernicusDemJob:
    row: int
    column: int
    bbox: BoundingBox
    width: int
    height: int
    instance: str
    acquisition_id: str

    @property
    def target_name(self) -> str:
        resolution = INSTANCES[self.instance]["resolution_m"]
        return f"copernicus_dem_{resolution}m_r{self.row:03d}_c{self.column:03d}.tif"

    def request_payload(self) -> dict:
        return {
            "input": {
                "bounds": {
                    "properties": {
                        "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
                    },
                    "bbox": [
                        self.bbox.west,
                        self.bbox.south,
                        self.bbox.east,
                        self.bbox.north,
                    ],
                },
                "data": [
                    {
                        "type": "dem",
                        "dataFilter": {"demInstance": self.instance},
                        "processing": {
                            "egm": False,
                            "upsampling": "BILINEAR",
                            "downsampling": "BILINEAR",
                        },
                    }
                ],
            },
            "output": {
                "width": self.width,
                "height": self.height,
                "responses": [
                    {"identifier": "default", "format": {"type": "image/tiff"}}
                ],
            },
            "evalscript": EVALSCRIPT,
        }


def plan_jobs(
    bbox: BoundingBox,
    *,
    instance: str = "COPERNICUS_30",
    max_tile_pixels: int = MAX_TILE_PIXELS,
) -> list[CopernicusDemJob]:
    """Split an AOI into Process API requests at the DEM's native grid spacing."""

    if instance not in INSTANCES:
        raise ValueError(f"unsupported Copernicus DEM instance: {instance}")
    if max_tile_pixels < 1:
        raise ValueError("max_tile_pixels must be positive")
    resolution = INSTANCES[instance]["resolution_degrees"]
    total_width = max(1, math.ceil((bbox.east - bbox.west) / resolution))
    total_height = max(1, math.ceil((bbox.north - bbox.south) / resolution))
    acquisition_id = _acquisition_id(bbox, instance)
    jobs = []
    row_count = math.ceil(total_height / max_tile_pixels)
    column_count = math.ceil(total_width / max_tile_pixels)
    for row in range(row_count):
        top_pixel = row * max_tile_pixels
        tile_height = min(max_tile_pixels, total_height - top_pixel)
        north = bbox.north - top_pixel * resolution
        south = (
            bbox.south
            if row == row_count - 1
            else bbox.north - (top_pixel + tile_height) * resolution
        )
        for column in range(column_count):
            left_pixel = column * max_tile_pixels
            tile_width = min(max_tile_pixels, total_width - left_pixel)
            west = bbox.west + left_pixel * resolution
            east = (
                bbox.east
                if column == column_count - 1
                else bbox.west + (left_pixel + tile_width) * resolution
            )
            jobs.append(
                CopernicusDemJob(
                    row + 1,
                    column + 1,
                    BoundingBox(west, south, east, north),
                    tile_width,
                    tile_height,
                    instance,
                    acquisition_id,
                )
            )
    return jobs


def fetch_access_token(
    client_id: str | None = None,
    client_secret: str | None = None,
    *,
    opener=urlopen,
) -> str:
    """Exchange a CDSE Sentinel Hub OAuth client for one reusable access token."""

    client_id = (client_id or os.environ.get("SH_CLIENT_ID") or "").strip()
    client_secret = (client_secret or os.environ.get("SH_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        raise ValueError(
            "Copernicus DEM acquisition requires a Sentinel Hub OAuth client ID and "
            "secret (enter them in the dialog or set SH_CLIENT_ID and SH_CLIENT_SECRET)."
        )
    body = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    request = Request(
        TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "IdrAgraGather/0.11",
        },
        method="POST",
    )
    try:
        with opener(request, timeout=60) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(_http_error("Copernicus authentication failed", exc)) from exc
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Copernicus authentication returned no access token.")
    return token


def fetch(
    root: str | Path,
    bbox: BoundingBox,
    *,
    instance: str = "COPERNICUS_30",
    client_id: str | None = None,
    client_secret: str | None = None,
    access_token: str | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int, Path], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    opener=urlopen,
    token_opener=urlopen,
) -> list[Path]:
    """Download an AOI-clipped orthometric Copernicus DEM as tiled GeoTIFFs."""

    jobs = plan_jobs(bbox, instance=instance)
    root_path = Path(root).resolve()
    output_dir = (
        root_path
        / "raw"
        / "topography"
        / "copernicus_dem"
        / jobs[0].acquisition_id
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    token = access_token
    manifest = Manifest(root_path)
    outputs = []
    for index, job in enumerate(jobs, start=1):
        if is_cancelled is not None and is_cancelled():
            break
        target = output_dir / job.target_name
        if target.exists() and target.stat().st_size > 0:
            if on_status:
                on_status(f"Reusing DEM tile {index}/{len(jobs)}.")
        else:
            if token is None:
                if on_status:
                    on_status("Authenticating with Copernicus Data Space Ecosystem.")
                token = fetch_access_token(
                    client_id,
                    client_secret,
                    opener=token_opener,
                )
            if on_status:
                on_status(f"Downloading DEM tile {index}/{len(jobs)}.")
            request = Request(
                PROCESS_URL,
                data=json.dumps(job.request_payload()).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "image/tiff",
                    "Content-Type": "application/json",
                    "User-Agent": "IdrAgraGather/0.11",
                },
                method="POST",
            )
            temporary = target.with_suffix(".tif.part")
            try:
                try:
                    with opener(request, timeout=300) as response, temporary.open(
                        "wb"
                    ) as stream:
                        content_type = response.headers.get("Content-Type", "")
                        if "json" in content_type.lower() or "text" in content_type.lower():
                            detail = response.read(4096).decode("utf-8", errors="replace")
                            raise RuntimeError(
                                f"Copernicus DEM service returned {content_type}: {detail}"
                            )
                        shutil.copyfileobj(response, stream)
                except HTTPError as exc:
                    raise RuntimeError(
                        _http_error("Copernicus DEM request failed", exc)
                    ) from exc
                if not _is_tiff(temporary):
                    raise RuntimeError("Copernicus DEM response was not a GeoTIFF.")
                temporary.replace(target)
            finally:
                if temporary.exists():
                    temporary.unlink()

        manifest.add_asset(
            target,
            category="topography",
            provider=PROVIDER,
            dataset=f"Copernicus DEM {INSTANCES[instance]['resolution_m']} m",
            source=PROCESS_URL,
            request={
                "acquisition_bbox": bbox.as_dict(),
                "tile_bbox": job.bbox.as_dict(),
                "instance": instance,
                "resolution_degrees": INSTANCES[instance]["resolution_degrees"],
                "resolution_m_nominal": INSTANCES[instance]["resolution_m"],
                "width": job.width,
                "height": job.height,
                "vertical_reference": "EGM2008 orthometric height",
                "unit": "m",
                "dataset_doi": DATASET_DOI,
                "dataset_page": DATASET_PAGE,
                "attribution": ATTRIBUTION[instance],
            },
        )
        outputs.append(target)
        if on_progress:
            on_progress(index, len(jobs), target)
    return outputs


def find_tiles(
    root: str | Path,
    bbox: BoundingBox,
    *,
    instance: str = "COPERNICUS_30",
) -> tuple[Path, ...]:
    """Locate the complete staged tile set for this AOI and DEM instance."""

    root_path = Path(root).resolve()
    expected = {job.target_name for job in plan_jobs(bbox, instance=instance)}
    acquisition_id = _acquisition_id(bbox, instance)
    directory = root_path / "raw" / "topography" / "copernicus_dem" / acquisition_id
    paths = tuple(sorted(directory / name for name in expected))
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise ValueError(
            "No complete Copernicus DEM acquisition was found for the selected AOI "
            f"and {instance}; missing {len(missing)} of {len(paths)} tile(s)."
        )
    return paths


def _acquisition_id(bbox: BoundingBox, instance: str) -> str:
    import hashlib

    text = (
        f"{instance}|{bbox.west:.12g}|{bbox.south:.12g}|"
        f"{bbox.east:.12g}|{bbox.north:.12g}"
    )
    digest = hashlib.sha256(text.encode("ascii")).hexdigest()[:12]
    return f"{instance.lower()}_{digest}"


def _is_tiff(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(4) in {b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"}


def _http_error(prefix: str, error: HTTPError) -> str:
    try:
        detail = error.read(4096).decode("utf-8", errors="replace").strip()
    except Exception:
        detail = ""
    suffix = f": {detail}" if detail else ""
    return f"{prefix} (HTTP {error.code}){suffix}"
