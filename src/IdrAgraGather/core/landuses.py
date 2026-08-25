"""Crop-rotation catalogue and source-class allocation helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class CropDefinition:
    crop_id: str
    name: str
    parameter_file: str


@dataclass(frozen=True)
class LandUseDefinition:
    landuse_id: int
    name: str
    crop1_id: str | None = None
    crop2_id: str | None = None


@dataclass(frozen=True)
class LandUseAllocation:
    source_class: str
    landuse_id: int
    share_pct: float


# This compact starter catalogue mirrors the IdrAgra-ready example supplied for
# development. Parameter-file contents remain an exporter concern; the cell
# builder only needs stable crop and rotation references.
DEFAULT_CROPS = (
    CropDefinition("autumn_sown_grain", "Autumn-sown grain", "2_autumn-sow.tab"),
    CropDefinition("orchard", "Mixed orchard", "12_orchard.tab"),
    CropDefinition("maize", "Maize", "13_maize.tab"),
    CropDefinition("tomato", "Tomato", "27_tomato.tab"),
    CropDefinition("cover_crop", "Cover crop", "33_cover_crop.tab"),
    CropDefinition("soybean", "Second-crop soybean", "34_soybean.tab"),
)

DEFAULT_LANDUSES = (
    LandUseDefinition(1, "Winter wheat", "autumn_sown_grain"),
    LandUseDefinition(2, "Maize", "maize"),
    LandUseDefinition(3, "Winter wheat -> maize", "autumn_sown_grain", "maize"),
    LandUseDefinition(4, "Cover crop -> maize", "cover_crop", "maize"),
    LandUseDefinition(5, "Second-crop soybean", "soybean"),
    LandUseDefinition(6, "Tomato", "tomato"),
    LandUseDefinition(7, "Mixed orchard", "orchard"),
    LandUseDefinition(8, "Non-agricultural land"),
)


def write_configuration(
    path: str | Path,
    crops: Sequence[CropDefinition],
    landuses: Sequence[LandUseDefinition],
    allocations: Sequence[LandUseAllocation],
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(configuration_dict(crops, landuses, allocations), indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return output


def configuration_dict(
    crops: Sequence[CropDefinition],
    landuses: Sequence[LandUseDefinition],
    allocations: Sequence[LandUseAllocation],
) -> dict:
    validate_catalog(crops, landuses)
    validate_allocations(allocations, landuses)
    return {
        "schema_version": 1,
        "crops": [asdict(item) for item in crops],
        "landuses": [asdict(item) for item in landuses],
        "allocations": [asdict(item) for item in allocations],
    }


def read_configuration(
    path: str | Path,
) -> tuple[list[CropDefinition], list[LandUseDefinition], list[LandUseAllocation]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported land-use configuration schema.")
    crops = [CropDefinition(**item) for item in data.get("crops", [])]
    landuses = [LandUseDefinition(**item) for item in data.get("landuses", [])]
    allocations = [LandUseAllocation(**item) for item in data.get("allocations", [])]
    validate_catalog(crops, landuses)
    validate_allocations(allocations, landuses)
    return crops, landuses, allocations


def validate_allocations(
    allocations: Sequence[LandUseAllocation],
    landuses: Sequence[LandUseDefinition],
    *,
    source_classes: Iterable[str] | None = None,
    tolerance: float = 1e-6,
) -> None:
    known_ids = {item.landuse_id for item in landuses}
    grouped: dict[str, float] = {}
    seen_pairs: set[tuple[str, int]] = set()
    for item in allocations:
        source = item.source_class.strip()
        if not source:
            raise ValueError("Every allocation must name a source land-use class.")
        if item.landuse_id not in known_ids:
            raise ValueError(f"Allocation for {source!r} uses unknown land-use ID {item.landuse_id}.")
        if not 0 < item.share_pct <= 100:
            raise ValueError(f"Allocation share for {source!r} must be greater than 0 and at most 100.")
        pair = (source, item.landuse_id)
        if pair in seen_pairs:
            raise ValueError(f"Land-use ID {item.landuse_id} is repeated for source class {source!r}.")
        seen_pairs.add(pair)
        grouped[source] = grouped.get(source, 0.0) + float(item.share_pct)

    expected = {str(value).strip() for value in (source_classes or ())}
    missing = sorted(value for value in expected if value and value not in grouped)
    if missing:
        preview = ", ".join(repr(value) for value in missing[:5])
        suffix = "..." if len(missing) > 5 else ""
        raise ValueError(f"No IdrAgra land-use allocation for {preview}{suffix}")
    for source, total in sorted(grouped.items()):
        if abs(total - 100.0) > tolerance:
            raise ValueError(f"Allocations for {source!r} total {total:g}%; expected 100%.")


# Read an IdrAgra ``soil_uses.txt`` rotation table.
#
# Crop IDs are derived from parameter-file stems. The trailing comment is
# used as the human-readable rotation name when present.
def parse_idragra_landuses(
    path: str | Path,
) -> tuple[list[CropDefinition], list[LandUseDefinition]]:
    source = Path(path)
    crops_by_file: dict[str, CropDefinition] = {}
    landuses: list[LandUseDefinition] = []
    for line_number, original in enumerate(source.read_text(encoding="utf-8-sig").splitlines(), start=1):
        body, _, comment = original.partition("#")
        tokens = body.split()
        if not tokens or tokens[0].casefold() in {"cr_id", "endtable"}:
            continue
        if len(tokens) < 2:
            raise ValueError(f"Invalid land-use row at {source}:{line_number}")
        try:
            landuse_id = int(tokens[0])
        except ValueError as exc:
            raise ValueError(f"Invalid land-use ID at {source}:{line_number}: {tokens[0]!r}") from exc
        files = [token for token in tokens[1:3] if token != "*"]
        crop_ids: list[str] = []
        for filename in files:
            crop_id = Path(filename).stem.replace("-", "_")
            crop_ids.append(crop_id)
            crops_by_file.setdefault(
                filename,
                CropDefinition(
                    crop_id,
                    Path(filename).stem.replace("_", " ").replace("-", " ").title(),
                    filename,
                ),
            )
        name = comment.strip() or f"Land use {landuse_id}"
        landuses.append(
            LandUseDefinition(
                landuse_id,
                name,
                crop_ids[0] if crop_ids else None,
                crop_ids[1] if len(crop_ids) > 1 else None,
            )
        )
    crops = list(crops_by_file.values())
    validate_catalog(crops, landuses)
    return crops, landuses


def validate_catalog(crops: Sequence[CropDefinition], landuses: Sequence[LandUseDefinition]) -> None:
    crop_ids = [crop.crop_id.strip() for crop in crops]
    if any(not crop_id for crop_id in crop_ids):
        raise ValueError("Every crop must have a non-empty identifier.")
    if len(set(crop_ids)) != len(crop_ids):
        raise ValueError("Crop identifiers must be unique.")
    landuse_ids = [item.landuse_id for item in landuses]
    if any(item <= 0 for item in landuse_ids):
        raise ValueError("Land-use IDs must be positive integers.")
    if len(set(landuse_ids)) != len(landuse_ids):
        raise ValueError("Land-use IDs must be unique.")
    available_crops = set(crop_ids)
    for item in landuses:
        if not item.name.strip():
            raise ValueError(f"Land use {item.landuse_id} has no name.")
        if item.crop2_id and not item.crop1_id:
            raise ValueError(f"Land use {item.landuse_id} has crop 2 but no crop 1.")
        for crop_id in (item.crop1_id, item.crop2_id):
            if crop_id and crop_id not in available_crops:
                raise ValueError(f"Land use {item.landuse_id} refers to unknown crop {crop_id!r}.")
