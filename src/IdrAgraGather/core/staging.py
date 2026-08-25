import json
import shutil
from pathlib import Path

from .manifest import Manifest, sha256_file
from .models import BoundingBox


CATEGORIES = ("weather", "soil", "landuse", "topography")


# Each project keeps one dataset per provider; resolve its files without relying on storage paths.
def find_staged_files(
    root: str | Path,
    *,
    provider: str,
    suffix: str | None = None,
) -> tuple[Path, ...]:
    root_path = Path(root).resolve()
    paths: list[Path] = []
    for asset in Manifest(root_path).read().get("assets", []):
        if asset.get("provider") != provider:
            continue
        path = root_path / asset["path"]
        if suffix is None or path.suffix == suffix:
            paths.append(path)

    if not paths:
        file_type = f" {suffix}" if suffix is not None else ""
        raise ValueError(f"No staged{file_type} files found for provider {provider!r}")

    missing = [path for path in paths if not path.is_file()]
    if missing:
        names = ", ".join(path.name for path in missing)
        raise ValueError(f"Staged files recorded for provider {provider!r} are missing: {names}")

    return tuple(sorted(paths))


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
                    f"{destination} already exists with different contents; use --overwrite or rename the input"
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

    # Write the requested area as a small EPSG:4326 GeoJSON layer.
    def write_aoi(self, bbox: BoundingBox) -> Path:
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
