from __future__ import annotations

import json
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..manifest import Manifest
from ..models import BoundingBox


QUERY_URL = (
    "https://image.discomap.eea.europa.eu/arcgis/rest/services/"
    "Corine/CLC2018_WM/MapServer/0/query"
)
PAGE_SIZE = 1000
DATASET = "Corine Land Cover 2018 vector"


def build_query_url(bbox: BoundingBox, *, offset: int = 0) -> str:
    query = {
        "where": "1=1",
        "geometry": f"{bbox.west},{bbox.south},{bbox.east},{bbox.north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "OBJECTID,Code_18,Remark,Area_Ha,ID",
        "returnGeometry": "true",
        "outSR": "4326",
        "orderByFields": "OBJECTID",
        "resultOffset": str(offset),
        "resultRecordCount": str(PAGE_SIZE),
        "f": "geojson",
    }
    return QUERY_URL + "?" + urlencode(query)


def fetch(
    root: str | Path,
    bbox: BoundingBox,
    *,
    is_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int, Path], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    opener=urlopen,
) -> list[Path]:
    root_path = Path(root).resolve()
    output_dir = root_path / "raw" / "landuse" / "corine"
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "clc2018.geojson"

    if target.exists() and target.stat().st_size > 0:
        if on_status:
            on_status("Reusing the existing CORINE 2018 GeoJSON.")
    else:
        features = []
        offset = 0
        while True:
            if is_cancelled is not None and is_cancelled():
                return []
            url = build_query_url(bbox, offset=offset)
            if on_status:
                on_status(f"Downloading CORINE 2018 features from offset {offset}.")
            request = Request(url, headers={"User-Agent": "IdrAgraGather/0.9"})
            with opener(request, timeout=180) as response:
                payload = json.load(response)
            if "error" in payload:
                raise RuntimeError(f"CORINE service rejected the query: {payload['error']}")
            page = payload.get("features")
            if not isinstance(page, list):
                raise RuntimeError("CORINE service returned an invalid GeoJSON response.")
            features.extend(page)
            if len(page) < PAGE_SIZE and not payload.get("exceededTransferLimit"):
                break
            offset += len(page)
            if not page:
                raise RuntimeError("CORINE service pagination did not advance.")

        collection = {
            "type": "FeatureCollection",
            "name": "clc2018",
            "crs": {
                "type": "name",
                "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
            },
            "features": features,
        }
        temporary = target.with_suffix(".geojson.part")
        try:
            temporary.write_text(
                json.dumps(collection, ensure_ascii=False), encoding="utf-8"
            )
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()
        if on_status:
            on_status(f"Saved {len(features)} CORINE 2018 feature(s).")

    Manifest(root_path).add_asset(
        target,
        category="landuse",
        provider="eea-corine-arcgis-rest",
        dataset="clc2018-vector",
        source=QUERY_URL,
        request={"bbox": bbox.as_dict(), "spatial_relation": "intersects"},
    )
    if on_progress:
        on_progress(1, 1, target)
    return [target]
