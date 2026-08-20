from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from .manifest import Manifest
from .models import BoundingBox
from .vector_clip import aoi_geometry


OUTPUT_NAME = "landuse.shp"
OUTPUT_LAYER = "landuse"
CODE_FIELD = "Code_18"
CATEGORY_FIELD = "landuse"
NOMENCLATURE_URL = (
    "https://land.copernicus.eu/content/"
    "corine-land-cover-nomenclature-guidelines/html/"
)

# Official level-three CORINE Land Cover nomenclature. Keeping this lookup in
# the normalizer makes the downloaded source code an implementation detail;
# the editable output exposes the meaningful class name instead.
CORINE_CATEGORIES = {
    111: "Continuous urban fabric",
    112: "Discontinuous urban fabric",
    121: "Industrial or commercial units",
    122: "Road and rail networks and associated land",
    123: "Port areas",
    124: "Airports",
    131: "Mineral extraction sites",
    132: "Dump sites",
    133: "Construction sites",
    141: "Green urban areas",
    142: "Sport and leisure facilities",
    211: "Non-irrigated arable land",
    212: "Permanently irrigated land",
    213: "Rice fields",
    221: "Vineyards",
    222: "Fruit trees and berry plantations",
    223: "Olive groves",
    231: "Pastures",
    241: "Annual crops associated with permanent crops",
    242: "Complex cultivation patterns",
    243: "Land principally occupied by agriculture, with significant areas of natural vegetation",
    244: "Agro-forestry areas",
    311: "Broad-leaved forest",
    312: "Coniferous forest",
    313: "Mixed forest",
    321: "Natural grasslands",
    322: "Moors and heathland",
    323: "Sclerophyllous vegetation",
    324: "Transitional woodland-shrub",
    331: "Beaches, dunes, sands",
    332: "Bare rocks",
    333: "Sparsely vegetated areas",
    334: "Burnt areas",
    335: "Glaciers and perpetual snow",
    411: "Inland marshes",
    412: "Peat bogs",
    421: "Salt marshes",
    422: "Salines",
    423: "Intertidal flats",
    511: "Water courses",
    512: "Water bodies",
    521: "Coastal lagoons",
    522: "Estuaries",
    523: "Sea and ocean",
}

_SHAPEFILE_SUFFIXES = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx")


@dataclass(frozen=True)
class CorineNormalizationResult:
    path: Path
    polygon_count: int
    category_count: int


def find_staged_file(root: str | Path) -> Path:
    path = Path(root).resolve() / "raw" / "landuse" / "corine" / "clc2018.geojson"
    if not path.is_file():
        raise ValueError(f"No staged CORINE GeoJSON found at {path}")
    return path


def corine_category(code: object) -> str:
    """Return the official level-three category for a CORINE class code."""

    if isinstance(code, bool) or code is None:
        raise ValueError(f"invalid CORINE class code: {code!r}")
    text = str(code).strip()
    try:
        numeric = float(text)
        integer = int(numeric)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid CORINE class code: {code!r}") from exc
    if numeric != integer:
        raise ValueError(f"invalid CORINE class code: {code!r}")
    try:
        return CORINE_CATEGORIES[integer]
    except KeyError as exc:
        raise ValueError(f"unsupported CORINE class code: {integer}") from exc


def normalize_corine_file(
    source_path: str | Path,
    output_root: str | Path,
    *,
    bbox: BoundingBox | None = None,
    on_status: Callable[[str], None] | None = None,
) -> CorineNormalizationResult:
    """Create a minimal categorized shapefile from a raw CORINE vector layer.

    The shapefile has OGR's intrinsic feature ID plus one text attribute named
    ``landuse``. Source identifiers, area estimates, remarks, and the numeric
    CORINE code remain available only in the raw provenance file.
    """

    source = Path(source_path).resolve()
    if not source.is_file():
        raise ValueError(f"CORINE input does not exist: {source}")
    output_root = Path(output_root).resolve()
    output = output_root / "landuse" / OUTPUT_NAME
    output.parent.mkdir(parents=True, exist_ok=True)
    if on_status is not None:
        on_status(f"Reading CORINE polygons from {source}.")

    try:
        from osgeo import ogr, osr
    except ImportError as exc:
        raise RuntimeError("CORINE normalization requires GDAL/OGR in QGIS") from exc

    ogr.UseExceptions()
    osr.UseExceptions()
    source_database = ogr.Open(str(source), 0)
    if source_database is None:
        raise ValueError(f"OGR could not open CORINE input: {source}")
    source_layer = source_database.GetLayer(0)
    if source_layer is None:
        source_database = None
        raise ValueError(f"CORINE input contains no vector layer: {source}")
    source_definition = source_layer.GetLayerDefn()
    code_index = next(
        (
            index
            for index in range(source_definition.GetFieldCount())
            if source_definition.GetFieldDefn(index).GetName().casefold()
            == CODE_FIELD.casefold()
        ),
        -1,
    )
    if code_index < 0:
        source_database = None
        raise ValueError(f"CORINE input has no {CODE_FIELD!r} field: {source}")

    polygon_count = 0
    categories: set[str] = set()
    clip_geometry = (
        aoi_geometry(bbox, source_layer.GetSpatialRef(), ogr, osr)
        if bbox is not None
        else None
    )
    try:
        with TemporaryDirectory(prefix=".landuse-", dir=output.parent) as temporary:
            temporary_output = Path(temporary) / OUTPUT_NAME
            driver = ogr.GetDriverByName("ESRI Shapefile")
            if driver is None:
                raise RuntimeError("OGR's ESRI Shapefile driver is unavailable")
            database = driver.CreateDataSource(str(temporary_output))
            if database is None:
                raise RuntimeError(f"could not create {temporary_output}")
            layer = database.CreateLayer(
                OUTPUT_LAYER,
                source_layer.GetSpatialRef(),
                ogr.wkbMultiPolygon,
                options=["ENCODING=UTF-8"],
            )
            if layer is None:
                database = None
                raise RuntimeError(f"could not create {OUTPUT_LAYER!r} in {temporary_output}")
            category_field = ogr.FieldDefn(CATEGORY_FIELD, ogr.OFTString)
            category_field.SetWidth(120)
            if layer.CreateField(category_field) != 0:
                database = None
                raise RuntimeError(f"failed to create shapefile field {CATEGORY_FIELD!r}")

            output_definition = layer.GetLayerDefn()
            source_layer.ResetReading()
            for source_feature in source_layer:
                category = corine_category(source_feature.GetField(code_index))
                geometry = source_feature.GetGeometryRef()
                if geometry is None or geometry.IsEmpty():
                    raise ValueError(
                        f"CORINE feature {source_feature.GetFID()} has no polygon geometry"
                    )
                geometry = geometry.Clone()
                if clip_geometry is not None:
                    geometry = geometry.Intersection(clip_geometry)
                    if geometry is None or geometry.IsEmpty():
                        continue
                geometry_type = ogr.GT_Flatten(geometry.GetGeometryType())
                if geometry_type == ogr.wkbPolygon:
                    geometry = ogr.ForceToMultiPolygon(geometry)
                elif geometry_type != ogr.wkbMultiPolygon:
                    if clip_geometry is not None:
                        continue
                    raise ValueError(
                        f"CORINE feature {source_feature.GetFID()} is not a polygon"
                    )

                feature = ogr.Feature(output_definition)
                feature.SetField(CATEGORY_FIELD, category)
                feature.SetGeometry(geometry)
                if layer.CreateFeature(feature) != 0:
                    raise RuntimeError(
                        f"failed to write CORINE feature {source_feature.GetFID()}"
                    )
                feature = None
                polygon_count += 1
                categories.add(category)
            layer.SyncToDisk()
            output_definition = None
            layer = None
            database = None
            _replace_shapefile(temporary_output, output)
    finally:
        source_definition = None
        source_layer = None
        source_database = None

    Manifest(output_root).add_asset(
        output,
        category="landuse",
        provider="idragather",
        dataset="normalized CORINE Land Cover 2018",
        source=str(source),
        request={
            "source_code_field": CODE_FIELD,
            "output_fields": [CATEGORY_FIELD],
            "classification": {
                "name": "CORINE level-three nomenclature",
                "source": NOMENCLATURE_URL,
            },
            "numeric_code_retained": False,
            "clip_aoi": bbox.as_dict() if bbox is not None else None,
            "polygon_count": polygon_count,
            "category_count": len(categories),
        },
    )
    if on_status is not None:
        on_status(
            f"Wrote {polygon_count} polygon(s) in {len(categories)} land-use category(ies)."
        )
    return CorineNormalizationResult(output, polygon_count, len(categories))


def _replace_shapefile(temporary: Path, target: Path) -> None:
    """Replace all shapefile components while retaining a recoverable old set."""

    new_components = [temporary.with_suffix(suffix) for suffix in _SHAPEFILE_SUFFIXES]
    new_components = [path for path in new_components if path.exists()]
    if not temporary.exists() or not new_components:
        raise RuntimeError(f"OGR did not create the expected shapefile: {temporary}")

    backup_directory = temporary.parent / "old"
    backup_directory.mkdir()
    moved_old: list[tuple[Path, Path]] = []
    installed_new: list[Path] = []
    try:
        for suffix in _SHAPEFILE_SUFFIXES:
            old = target.with_suffix(suffix)
            if old.exists():
                backup = backup_directory / old.name
                old.replace(backup)
                moved_old.append((old, backup))
        for component in new_components:
            installed = target.with_suffix(component.suffix)
            component.replace(installed)
            installed_new.append(installed)
    except PermissionError as exc:
        for installed in installed_new:
            installed.unlink(missing_ok=True)
        for old, backup in moved_old:
            if backup.exists():
                backup.replace(old)
        raise RuntimeError(
            "Could not replace the normalized land-use shapefile because it is open "
            f"in QGIS or another application: {target}"
        ) from exc
    except Exception:
        for installed in installed_new:
            installed.unlink(missing_ok=True)
        for old, backup in moved_old:
            if backup.exists():
                backup.replace(old)
        raise
