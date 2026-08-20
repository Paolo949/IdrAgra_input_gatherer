from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

import numpy as np

from .manifest import Manifest
from .models import BoundingBox
from .providers.soilgrids import DEPTHS, PROPERTIES, ensure_raster_crs
from .vector_clip import aoi_geometry


OUTPUT_NAME = "soil_profiles.gpkg"
OUTPUT_LAYER = "soil_profiles"
DEPTH_BOUNDS = ((0, 5), (5, 15), (15, 30), (30, 60), (60, 100), (100, 200))
VALUE_NAMES = ("sand_pct", "silt_pct", "clay_pct", "skel_pct", "oc_pct", "bd_g_cm3")
ROUND_DECIMALS = 4
DEFAULT_MAX_CLASSES = 20
SIMILARITY_SCALES = { # in defining soil classes (individual IDs), a change in sand content of "sand_pct"% holds the same weight as a change in organic carbon of "oc_pct"%
    "sand_pct": 5.0,
    "silt_pct": 5.0,
    "clay_pct": 5.0,
    "skel_pct": 10.0,
    "oc_pct": 0.5,
    "bd_g_cm3": 0.1,
}


@dataclass(frozen=True)
class NormalizedSoilGrid:
    fields: tuple[str, ...]
    profiles: np.ndarray
    zone_ids: np.ndarray
    valid_mask: np.ndarray
    exact_profile_count: int
    filled_nodata_cells: int


@dataclass(frozen=True)
class SoilNormalizationResult:
    path: Path
    profile_count: int
    polygon_count: int
    exact_profile_count: int
    filled_nodata_cells: int


def find_staged_files(root: str | Path) -> tuple[Path, ...]:
    """Return a complete, canonically ordered SoilGrids mean-coverage set."""

    directory = Path(root).resolve() / "raw" / "soil" / "soilgrids"
    paths = tuple(
        directory / f"{name}_{depth}_mean.tif"
        for depth in DEPTHS
        for name in PROPERTIES
    )
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        preview = ", ".join(missing[:5])
        if len(missing) > 5:
            preview += f", and {len(missing) - 5} more"
        raise ValueError(f"Incomplete SoilGrids input in {directory}; missing {preview}")
    return paths


def normalize_soilgrids_arrays(
    raw: Mapping[tuple[str, str], np.ndarray],
    valid_masks: Mapping[tuple[str, str], np.ndarray] | None = None,
    *,
    max_classes: int = DEFAULT_MAX_CLASSES,
    fill_nodata: bool = True,
) -> NormalizedSoilGrid:
    """Convert SoilGrids maps into a bounded set of representative profiles.

    Texture fractions are closed to exactly 100 percent for every horizon.
    Similarity is evaluated jointly over all horizons in parameter-scaled space.
    Missing-data cells are assigned the nearest valid soil class so the output
    covers the complete acquired AOI raster.
    """

    if isinstance(max_classes, bool) or int(max_classes) != max_classes:
        raise ValueError("maximum soil classes must be an integer")
    max_classes = int(max_classes)
    if max_classes < 1:
        raise ValueError("maximum soil classes must be at least 1")

    expected = tuple((depth, name) for depth in DEPTHS for name in PROPERTIES)
    missing = [
        f"{name}_{depth}"
        for depth, name in expected
        if (depth, name) not in raw
    ]
    if missing:
        raise ValueError(f"missing SoilGrids arrays: {', '.join(missing)}")

    shape = np.asarray(raw[expected[0]]).shape
    if len(shape) != 2 or 0 in shape:
        raise ValueError("SoilGrids arrays must be non-empty two-dimensional rasters")
    valid = np.ones(shape, dtype=bool)
    arrays: dict[tuple[str, str], np.ndarray] = {}
    for key in expected:
        array = np.asarray(raw[key], dtype=np.float64)
        if array.shape != shape:
            raise ValueError(f"SoilGrids array {key} has shape {array.shape}, expected {shape}")
        arrays[key] = array
        valid &= np.isfinite(array)
        if valid_masks is not None and key in valid_masks:
            mask = np.asarray(valid_masks[key], dtype=bool)
            if mask.shape != shape:
                raise ValueError(f"validity mask {key} does not match the raster grid")
            valid &= mask

    normalized: dict[str, np.ndarray] = {}
    fields: list[str] = []
    for horizon, depth in enumerate(DEPTHS, start=1):
        sand = arrays[(depth, "sand")] / 10.0
        silt = arrays[(depth, "silt")] / 10.0
        clay = arrays[(depth, "clay")] / 10.0
        texture_total = sand + silt + clay
        valid &= (sand >= 0.0) & (silt >= 0.0) & (clay >= 0.0)
        valid &= texture_total > 0.0
        with np.errstate(divide="ignore", invalid="ignore"):
            sand_pct = np.round(sand * 100.0 / texture_total, ROUND_DECIMALS)
            silt_pct = np.round(silt * 100.0 / texture_total, ROUND_DECIMALS)
            values = {
                "sand_pct": sand_pct,
                "silt_pct": silt_pct,
                # Calculate the last fraction as the remainder so the stored
                # values retain exact texture closure at the chosen precision.
                "clay_pct": 100.0 - sand_pct - silt_pct,
                "skel_pct": arrays[(depth, "cfvo")] / 10.0,
                # SoilGrids soc / 10 gives g/kg; another / 10 gives mass percent.
                "oc_pct": arrays[(depth, "soc")] / 100.0,
                "bd_g_cm3": arrays[(depth, "bdod")] / 100.0,
            }
        valid &= (values["skel_pct"] >= 0.0) & (values["skel_pct"] <= 100.0)
        valid &= values["oc_pct"] >= 0.0
        valid &= values["bd_g_cm3"] > 0.0
        for name in VALUE_NAMES:
            field = f"h{horizon}_{name}"
            fields.append(field)
            normalized[field] = np.round(values[name], ROUND_DECIMALS)
            valid &= np.isfinite(normalized[field])

    matrix = np.column_stack([normalized[field].reshape(-1) for field in fields])
    flat_valid = valid.reshape(-1)
    if not np.any(flat_valid):
        raise ValueError("SoilGrids input has no cells complete across all six horizons")
    exact_profiles, inverse = np.unique(
        matrix[flat_valid], axis=0, return_inverse=True
    )
    counts = np.bincount(inverse, minlength=len(exact_profiles))
    profiles, exact_class_ids = _cluster_profiles(
        exact_profiles, counts, max_classes
    )
    zones = np.zeros(matrix.shape[0], dtype=np.int32)
    zones[flat_valid] = exact_class_ids[inverse].astype(np.int32) + 1
    zones = zones.reshape(shape)
    filled_nodata_cells = 0
    if fill_nodata:
        zones, filled_nodata_cells = _fill_nodata_cells(zones)
    output_mask = zones > 0
    return NormalizedSoilGrid(
        tuple(fields),
        profiles,
        zones,
        output_mask,
        len(exact_profiles),
        filled_nodata_cells,
    )


def normalize_soilgrids_files(
    source_paths: Iterable[str | Path],
    output_root: str | Path,
    *,
    bbox: BoundingBox | None = None,
    max_classes: int = DEFAULT_MAX_CLASSES,
    fill_nodata: bool = True,
    on_status: Callable[[str], None] | None = None,
) -> SoilNormalizationResult:
    paths = tuple(Path(path).resolve() for path in source_paths)
    if on_status is not None:
        on_status(f"Reading {len(paths)} SoilGrids coverage(s).")
    raw, masks, geotransform, projection = _read_rasters(paths)
    normalized = normalize_soilgrids_arrays(
        raw,
        masks,
        max_classes=max_classes,
        fill_nodata=fill_nodata,
    )
    output = Path(output_root).resolve() / "soil" / OUTPUT_NAME
    output.parent.mkdir(parents=True, exist_ok=True)
    if on_status is not None:
        on_status(
            f"Grouped {normalized.exact_profile_count} exact profile(s) into "
            f"{len(normalized.profiles)} soil class(es); filled "
            f"{normalized.filled_nodata_cells} NoData cell(s)."
        )
    polygon_count = _write_geopackage(
        output, normalized, geotransform, projection, bbox=bbox
    )

    Manifest(Path(output_root)).add_asset(
        output,
        category="soil",
        provider="idragather",
        dataset="normalized ISRIC SoilGrids profiles",
        source="; ".join(str(path) for path in paths),
        request={
            "depth_intervals_cm": [list(bounds) for bounds in DEPTH_BOUNDS],
            "fields": list(normalized.fields),
            "units": {
                "sand_pct": "mass percent of fine earth",
                "silt_pct": "mass percent of fine earth",
                "clay_pct": "mass percent of fine earth",
                "skel_pct": "volume percent coarse fragments",
                "oc_pct": "mass percent organic carbon",
                "bd_g_cm3": "g/cm3",
            },
            "texture_policy": "rescale sand+silt+clay to 100 percent per horizon",
            "classification": {
                "method": "deterministic cell-weighted k-means",
                "maximum_classes": max_classes,
                "exact_profile_count": normalized.exact_profile_count,
                "output_class_count": len(normalized.profiles),
                "similarity_scales": SIMILARITY_SCALES,
            },
            "missing_data_policy": (
                "fill every NoData cell in the acquired AOI raster from the "
                "nearest valid soil class"
                if fill_nodata
                else "exclude cells incomplete in any property or horizon"
            ),
            "filled_nodata_cells": normalized.filled_nodata_cells,
            "clip_aoi": bbox.as_dict() if bbox is not None else None,
            "ptf_applied": False,
        },
    )
    return SoilNormalizationResult(
        output,
        len(normalized.profiles),
        polygon_count,
        normalized.exact_profile_count,
        normalized.filled_nodata_cells,
    )


def _cluster_profiles(
    profiles: np.ndarray,
    counts: np.ndarray,
    max_classes: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Return representative profiles and a class index for every input row."""

    class_count = min(max_classes, len(profiles))
    if class_count == len(profiles):
        return profiles.copy(), np.arange(len(profiles), dtype=np.int32)

    scales = np.array(
        [SIMILARITY_SCALES[field.split("_", 1)[1]] for field in _profile_fields()],
        dtype=np.float64,
    )
    scaled = profiles / scales
    centers = np.empty((class_count, profiles.shape[1]), dtype=np.float64)
    first = int(np.argmax(counts))
    centers[0] = scaled[first]
    nearest = np.sum((scaled - centers[0]) ** 2, axis=1)
    for index in range(1, class_count):
        candidate = int(np.argmax(nearest * counts))
        centers[index] = scaled[candidate]
        nearest = np.minimum(
            nearest, np.sum((scaled - centers[index]) ** 2, axis=1)
        )

    labels = np.full(len(profiles), -1, dtype=np.int32)
    for _ in range(100):
        distances = np.empty((len(profiles), class_count), dtype=np.float64)
        for index in range(class_count):
            distances[:, index] = np.sum((scaled - centers[index]) ** 2, axis=1)
        updated = np.argmin(distances, axis=1).astype(np.int32)
        class_sizes = np.bincount(updated, minlength=class_count)
        for empty in np.flatnonzero(class_sizes == 0):
            assigned_distance = distances[np.arange(len(profiles)), updated]
            movable = class_sizes[updated] > 1
            score = np.where(movable, assigned_distance * counts, -1.0)
            candidate = int(np.argmax(score))
            previous = int(updated[candidate])
            class_sizes[previous] -= 1
            updated[candidate] = empty
            class_sizes[empty] += 1
        if np.array_equal(updated, labels):
            break
        labels = updated
        for index in range(class_count):
            members = labels == index
            centers[index] = np.average(
                scaled[members], axis=0, weights=counts[members]
            )

    representatives = np.empty_like(centers)
    for index in range(class_count):
        members = labels == index
        representatives[index] = np.average(
            profiles[members], axis=0, weights=counts[members]
        )
    representatives = _round_representatives(representatives)

    # Stable IDs make reruns reproducible even if the iterative center order changes.
    order = np.array(
        sorted(range(class_count), key=lambda index: tuple(representatives[index])),
        dtype=np.int32,
    )
    remap = np.empty(class_count, dtype=np.int32)
    remap[order] = np.arange(class_count, dtype=np.int32)
    return representatives[order], remap[labels]


def _round_representatives(profiles: np.ndarray) -> np.ndarray:
    result = np.round(profiles, ROUND_DECIMALS)
    fields = _profile_fields()
    for horizon in range(1, len(DEPTHS) + 1):
        sand = fields.index(f"h{horizon}_sand_pct")
        silt = fields.index(f"h{horizon}_silt_pct")
        clay = fields.index(f"h{horizon}_clay_pct")
        result[:, clay] = np.round(
            100.0 - result[:, sand] - result[:, silt], ROUND_DECIMALS
        )
    return result


def _profile_fields() -> tuple[str, ...]:
    return tuple(
        f"h{horizon}_{name}"
        for horizon in range(1, len(DEPTHS) + 1)
        for name in VALUE_NAMES
    )


def _fill_nodata_cells(zone_ids: np.ndarray) -> tuple[np.ndarray, int]:
    """Fill every zero cell from the nearest four-connected soil class."""

    if zone_ids.ndim != 2:
        raise ValueError("soil class zones must be a two-dimensional raster")
    remaining = zone_ids == 0
    if not np.any(remaining):
        return zone_ids.copy(), 0
    fill_count = int(np.count_nonzero(remaining))
    filled = zone_ids.copy()
    while np.any(remaining):
        sentinel = np.iinfo(np.int32).max
        candidates = np.full(zone_ids.shape, sentinel, dtype=np.int32)
        candidates[1:] = np.minimum(
            candidates[1:], np.where(filled[:-1] > 0, filled[:-1], sentinel)
        )
        candidates[:-1] = np.minimum(
            candidates[:-1], np.where(filled[1:] > 0, filled[1:], sentinel)
        )
        candidates[:, 1:] = np.minimum(
            candidates[:, 1:], np.where(filled[:, :-1] > 0, filled[:, :-1], sentinel)
        )
        candidates[:, :-1] = np.minimum(
            candidates[:, :-1], np.where(filled[:, 1:] > 0, filled[:, 1:], sentinel)
        )
        assign = remaining & (candidates != sentinel)
        if not np.any(assign):
            raise RuntimeError("could not propagate a soil class into an enclosed hole")
        filled[assign] = candidates[assign]
        remaining[assign] = False
    return filled, fill_count


def _read_rasters(paths: tuple[Path, ...]):
    try:
        from osgeo import gdal
    except ImportError as exc:
        raise RuntimeError("SoilGrids normalization requires GDAL's Python bindings in QGIS") from exc

    gdal.UseExceptions()
    by_name = {path.name: path for path in paths}
    expected = {
        f"{name}_{depth}_mean.tif": (depth, name)
        for depth in DEPTHS
        for name in PROPERTIES
    }
    missing = sorted(set(expected) - set(by_name))
    if missing:
        raise ValueError(f"missing SoilGrids files: {', '.join(missing)}")
    unexpected = sorted(set(by_name) - set(expected))
    if unexpected:
        raise ValueError(f"unexpected SoilGrids files: {', '.join(unexpected)}")

    raw = {}
    masks = {}
    reference_shape = None
    reference_transform = None
    reference_projection = None
    for filename, key in expected.items():
        ensure_raster_crs(by_name[filename])
        dataset = gdal.Open(str(by_name[filename]), gdal.GA_ReadOnly)
        if dataset is None or dataset.RasterCount < 1:
            raise ValueError(f"GDAL could not open SoilGrids raster: {by_name[filename]}")
        shape = (dataset.RasterYSize, dataset.RasterXSize)
        transform = dataset.GetGeoTransform()
        projection = dataset.GetProjection()
        if reference_shape is None:
            reference_shape = shape
            reference_transform = transform
            reference_projection = projection
        elif (
            shape != reference_shape
            or not np.allclose(transform, reference_transform, rtol=0.0, atol=1e-9)
            or projection != reference_projection
        ):
            raise ValueError(
                "SoilGrids raster is not aligned with the other coverages: "
                f"{by_name[filename]}"
            )
        band = dataset.GetRasterBand(1)
        array = np.asarray(band.ReadAsArray(), dtype=np.float64)
        mask = np.asarray(band.GetMaskBand().ReadAsArray(), dtype=bool)
        nodata = band.GetNoDataValue()
        if nodata is not None:
            mask &= array != float(nodata)
        raw[key] = array
        masks[key] = mask
        dataset = None
    return raw, masks, reference_transform, reference_projection


def _write_geopackage(path, normalized, geotransform, projection, *, bbox=None):
    try:
        from osgeo import gdal, ogr, osr
    except ImportError as exc:
        raise RuntimeError("SoilGrids normalization requires GDAL/OGR in QGIS") from exc

    gdal.UseExceptions()
    ogr.UseExceptions()
    osr.UseExceptions()
    temporary = path.with_name(path.stem + ".tmp.gpkg")
    temporary.unlink(missing_ok=True)
    driver = ogr.GetDriverByName("GPKG")
    database = driver.CreateDataSource(str(temporary))
    if database is None:
        raise RuntimeError(f"could not create {temporary}")
    spatial_reference = osr.SpatialReference()
    if projection:
        spatial_reference.ImportFromWkt(projection)
    layer = database.CreateLayer(OUTPUT_LAYER, spatial_reference, ogr.wkbPolygon)
    if layer is None:
        database = None
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"could not create {OUTPUT_LAYER!r} in {temporary}")
    _add_field(layer, ogr, "profile_id", ogr.OFTInteger)
    for horizon, (top, bottom) in enumerate(DEPTH_BOUNDS, start=1):
        _add_field(layer, ogr, f"h{horizon}_top_cm", ogr.OFTInteger)
        _add_field(layer, ogr, f"h{horizon}_bottom_cm", ogr.OFTInteger)
        for name in VALUE_NAMES:
            _add_field(layer, ogr, f"h{horizon}_{name}", ogr.OFTReal)

    memory = gdal.GetDriverByName("MEM").Create(
        "",
        normalized.zone_ids.shape[1],
        normalized.zone_ids.shape[0],
        2,
        gdal.GDT_Int32,
    )
    memory.SetGeoTransform(geotransform)
    memory.SetProjection(projection)
    zone_band = memory.GetRasterBand(1)
    zone_band.WriteArray(normalized.zone_ids)
    zone_band.SetNoDataValue(0)
    mask_band = memory.GetRasterBand(2)
    mask_band.WriteArray(normalized.valid_mask.astype(np.int32))
    profile_index = layer.GetLayerDefn().GetFieldIndex("profile_id")
    if gdal.Polygonize(zone_band, mask_band, layer, profile_index, []) != 0:
        raise RuntimeError("GDAL failed to polygonize normalized SoilGrids profiles")

    clip_geometry = (
        aoi_geometry(bbox, spatial_reference, ogr, osr) if bbox is not None else None
    )
    database.StartTransaction()
    polygon_count = 0
    try:
        layer.ResetReading()
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if clip_geometry is not None:
                geometry = geometry.Intersection(clip_geometry)
                if geometry is None or geometry.IsEmpty():
                    if layer.DeleteFeature(feature.GetFID()) != 0:
                        raise RuntimeError("failed to remove a soil polygon outside the AOI")
                    continue
            else:
                geometry = geometry.Clone()
            geometry_type = ogr.GT_Flatten(geometry.GetGeometryType())
            if geometry_type == ogr.wkbPolygon:
                polygon_parts = [geometry]
            elif geometry_type == ogr.wkbMultiPolygon:
                polygon_parts = [
                    geometry.GetGeometryRef(index).Clone()
                    for index in range(geometry.GetGeometryCount())
                ]
            else:
                raise RuntimeError("clipping produced a non-polygon soil geometry")
            profile_id = int(feature.GetField("profile_id"))
            row = normalized.profiles[profile_id - 1]
            for horizon, (top, bottom) in enumerate(DEPTH_BOUNDS, start=1):
                feature.SetField(f"h{horizon}_top_cm", top)
                feature.SetField(f"h{horizon}_bottom_cm", bottom)
            for field, value in zip(normalized.fields, row):
                feature.SetField(field, float(value))
            feature.SetGeometry(polygon_parts[0])
            if layer.SetFeature(feature) != 0:
                raise RuntimeError("failed to attach soil attributes to a polygon")
            polygon_count += 1
            for polygon in polygon_parts[1:]:
                split_feature = ogr.Feature(layer.GetLayerDefn())
                split_feature.SetFrom(feature)
                split_feature.SetGeometry(polygon)
                if layer.CreateFeature(split_feature) != 0:
                    raise RuntimeError("failed to write a clipped soil polygon part")
                split_feature = None
                polygon_count += 1
        database.CommitTransaction()
        database.ExecuteSQL(
            "CREATE INDEX IF NOT EXISTS soil_profiles_profile_id ON soil_profiles (profile_id)"
        )
    except Exception:
        database.RollbackTransaction()
        raise
    finally:
        memory = None
        database = None

    try:
        path.unlink(missing_ok=True)
        temporary.replace(path)
    except PermissionError as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            "Could not replace the normalized soil GeoPackage because it is open in QGIS "
            f"or another application: {path}"
        ) from exc
    return polygon_count


def _add_field(layer, ogr, name, field_type):
    if layer.CreateField(ogr.FieldDefn(name, field_type)) != 0:
        raise RuntimeError(f"failed to create GeoPackage field {name!r}")
