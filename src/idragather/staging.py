import json
import shutil
from pathlib import Path

from .manifest import Manifest, sha256_file
from .models import BoundingBox


CATEGORIES = ("weather", "soil", "landuse", "topography")


class StagingArea:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.manifest = Manifest(self.root)

    def stage_local(
        self,
        source: str | Path,
        *,
        category: str,
        source_name: str | None = None,
        overwrite: bool = False,
    ) -> Path:
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of: {', '.join(CATEGORIES)}")
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)

        destination_dir = self.root / "raw" / category / "local"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source_path.name

        if destination.exists() and not overwrite:
            if sha256_file(source_path) != sha256_file(destination):
                raise FileExistsError(
                    f"{destination} already exists with different contents; "
                    "use --overwrite or rename the input"
                )
        elif source_path != destination:
            shutil.copy2(source_path, destination)

        self.manifest.add_asset(
            destination,
            category=category,
            provider="local",
            dataset=source_name or source_path.name,
            source=str(source_path),
        )
        return destination

    def write_aoi(self, bbox: BoundingBox) -> Path:
        """Write the requested area as a small EPSG:4326 GeoJSON layer."""

        self.root.mkdir(parents=True, exist_ok=True)
        output = self.root / "aoi.geojson"
        ring = [
            [bbox.west, bbox.south],
            [bbox.east, bbox.south],
            [bbox.east, bbox.north],
            [bbox.west, bbox.north],
            [bbox.west, bbox.south],
        ]
        geojson = {
            "type": "FeatureCollection",
            "name": "IdrAgra acquisition AOI",
            "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                }
            ],
        }
        output.write_text(json.dumps(geojson, indent=2) + "\n", encoding="utf-8")
        self.manifest.configure(aoi=bbox.as_dict())
        self.manifest.add_asset(
            output,
            category="aoi",
            provider="user-selection",
            dataset="bounding-box",
        )
        return output
