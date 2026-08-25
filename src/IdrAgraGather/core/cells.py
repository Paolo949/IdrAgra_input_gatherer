"""Build version-neutral IdrAgra simulation cells from normalized inputs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .landuses import (
    CropDefinition,
    LandUseAllocation,
    LandUseDefinition,
    validate_allocations,
    validate_catalog,
    write_configuration,
)
from .manifest import Manifest
from .models import BoundingBox
from .vector_clip import aoi_geometry


OUTPUT_DIRECTORY = "cells"
CELLS_NAME = "simulation_cells.gpkg"
CELLS_LAYER = "simulation_cells"
CONFIGURATION_NAME = "landuse_configuration.json"
GRID_SOIL_NAME = "soil_id.tif"
GRID_LANDUSE_NAME = "landuse_id.tif"
GRID_ELEVATION_NAME = "elevation_m_asl.tif"
GRID_SLOPE_NAME = "slope_pct.tif"
NODATA = -9999.0


@dataclass(frozen=True)
class CellBuildResult:
    cells_path: Path
    configuration_path: Path
    raster_paths: tuple[Path, ...]
    cell_count: int
    requested_area_by_landuse: dict[int, float]
    generated_area_by_landuse: dict[int, float]
    warnings: tuple[str, ...]

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.cells_path, self.configuration_path, *self.raster_paths)


@dataclass
class _Cell:
    geometry: object
    soil_id: int
    source_landuse: str
    soil_coverage_pct: float
    landuse_coverage_pct: float
    elevation_m_asl: float | None
    slope_pct: float | None
    row: int | None = None
    column: int | None = None
    aoi_fraction: float = 1.0
    landuse_id: int | None = None
    cell_id: int | None = None

    @property
    def area_m2(self) -> float:
        return float(self.geometry.GetArea())


# Return the median within the most populated numeric band.
def dominant_continuous_value(values, *, bin_width: float = 1.0) -> float:
    from collections import Counter
    from statistics import median

    samples = [float(value) for value in values if math.isfinite(float(value))]
    if not samples:
        raise ValueError("Cannot calculate a dominant value from no valid samples.")
    if bin_width <= 0:
        raise ValueError("Dominant-value bin width must be positive.")
    bins = [math.floor(value / bin_width + 0.5) for value in samples]
    counts = Counter(bins)
    maximum = max(counts.values())
    candidates = [key for key, count in counts.items() if count == maximum]
    centre = median(samples) / bin_width
    winner = int(min(candidates, key=lambda item: (abs(item - centre), item)))
    return float(median(value for value, band in zip(samples, bins) if band == winner))


# Create a canonical cell GeoPackage and, in grid mode, aligned rasters.
def build_simulation_cells(
    output_root: str | Path,
    *,
    mode: str,
    crops: Sequence[CropDefinition],
    landuses: Sequence[LandUseDefinition],
    allocations: Sequence[LandUseAllocation],
    cell_width_m: float = 250.0,
    grid_boundary_policy: str = "inside",
    elevation_method: str = "median",
    slope_method: str = "dominant",
    bbox: BoundingBox | None = None,
    soil_path: str | Path | None = None,
    landuse_path: str | Path | None = None,
    elevation_path: str | Path | None = None,
    slope_path: str | Path | None = None,
    on_status: Callable[[str], None] | None = None,
) -> CellBuildResult:
    mode = str(mode).strip().casefold()
    if mode not in {"grid", "vector"}:
        raise ValueError("Cell mode must be 'grid' or 'vector'.")
    if cell_width_m <= 0:
        raise ValueError("Grid cell width must be positive.")
    grid_boundary_policy = str(grid_boundary_policy).strip().casefold()
    if grid_boundary_policy not in {"inside", "intersect"}:
        raise ValueError("Grid boundary policy must be 'inside' or 'intersect'.")
    if elevation_method not in {"mean", "median", "dominant", "centroid"}:
        raise ValueError(f"Unsupported elevation aggregation: {elevation_method}")
    if slope_method not in {"mean", "median", "dominant", "centroid"}:
        raise ValueError(f"Unsupported slope aggregation: {slope_method}")
    validate_catalog(crops, landuses)
    validate_allocations(allocations, landuses)

    root = Path(output_root).resolve()
    soil_path = Path(soil_path or root / "soil" / "soil_profiles.gpkg").resolve()
    landuse_path = Path(landuse_path or root / "landuse" / "landuse.shp").resolve()
    elevation_path = Path(elevation_path or root / "topography" / "elevation_m_asl.tif").resolve()
    slope_path = Path(slope_path or root / "topography" / "slope_pct.tif").resolve()
    for label, path in (
        ("soil profiles", soil_path),
        ("normalized land use", landuse_path),
        ("elevation", elevation_path),
        ("slope", slope_path),
    ):
        if not path.is_file():
            raise ValueError(f"Required {label} input does not exist: {path}")
    if bbox is None:
        bbox = _read_workspace_bbox(root)

    try:
        from osgeo import gdal, ogr, osr
    except ImportError as exc:
        raise RuntimeError("Cell generation requires GDAL/OGR in QGIS.") from exc
    gdal.UseExceptions()
    ogr.UseExceptions()
    osr.UseExceptions()

    elevation = _RasterSampler(elevation_path, gdal, ogr, osr)
    slope = _RasterSampler(slope_path, gdal, ogr, osr)
    if not elevation.spatial_reference.IsSame(slope.spatial_reference):
        raise ValueError("Elevation and slope rasters use different coordinate systems.")
    if on_status:
        on_status("Reading normalized soil and land-use polygons.")
    soil_features = _read_features(soil_path, "profile_id", elevation.spatial_reference, ogr, osr, integer=True)
    landuse_features = _read_features(landuse_path, "landuse", elevation.spatial_reference, ogr, osr)
    source_classes = sorted({str(value) for value, _ in landuse_features})
    validate_allocations(allocations, landuses, source_classes=source_classes)
    aoi = aoi_geometry(bbox, elevation.spatial_reference, ogr, osr)

    if mode == "grid":
        if on_status:
            boundary_text = "fully inside the AOI" if grid_boundary_policy == "inside" else "intersecting the AOI"
            on_status(f"Building a {cell_width_m:g} m regular cell grid {boundary_text}.")
        cells, grid_spec = _build_grid_cells(
            elevation,
            slope,
            soil_features,
            landuse_features,
            aoi,
            cell_width_m,
            grid_boundary_policy,
            elevation_method,
            slope_method,
            ogr,
        )
    else:
        if on_status:
            on_status("Intersecting contiguous soil and land-use regions.")
        cells = _build_vector_cells(
            elevation,
            slope,
            soil_features,
            landuse_features,
            aoi,
            ogr,
        )
        grid_spec = None
    if not cells:
        raise ValueError("The normalized inputs produced no simulation cells.")

    allocation_items = []
    for index, cell in enumerate(cells):
        identity = f"r{cell.row}:c{cell.column}" if cell.row is not None else f"v{index}:{_centroid_key(cell.geometry)}"
        allocation_items.append((f"{cell.source_landuse}\0{identity}", cell.area_m2))
    assigned, requested, generated = allocate_landuse_ids(allocation_items, allocations)
    for cell, (allocation_key, _) in zip(cells, allocation_items):
        cell.landuse_id = assigned[allocation_key]

    if mode == "grid":
        cells.sort(key=lambda cell: (cell.row, cell.column))
    else:
        cells.sort(key=lambda cell: _spatial_sort_key(cell.geometry))
    for cell_id, cell in enumerate(cells, start=1):
        cell.cell_id = cell_id

    warnings: list[str] = []
    missing_topography = sum(cell.elevation_m_asl is None or cell.slope_pct is None for cell in cells)
    if missing_topography:
        warnings.append(f"{missing_topography} cell(s) have missing elevation or slope values.")
    if mode == "vector":
        for landuse_id in sorted(set(requested) | set(generated)):
            target = requested.get(landuse_id, 0.0)
            actual = generated.get(landuse_id, 0.0)
            if target and abs(actual - target) / target > 0.05:
                warnings.append(
                    f"Vector allocation for land-use ID {landuse_id} differs from "
                    "the requested area by more than 5%; whole contiguous regions "
                    "are not subdivided in this first version."
                )

    output_dir = root / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    cells_path = output_dir / CELLS_NAME
    configuration_path = output_dir / CONFIGURATION_NAME
    landuse_by_id = {item.landuse_id: item for item in landuses}
    if on_status:
        on_status(f"Writing {len(cells)} simulation cell(s).")
    _write_cells(cells_path, cells, mode, landuse_by_id, elevation.spatial_reference, ogr, osr)
    write_configuration(configuration_path, crops, landuses, allocations)

    raster_paths: tuple[Path, ...] = ()
    if grid_spec is not None:
        import numpy as np

        rows, columns, geotransform = grid_spec
        soil_values = np.full((rows, columns), int(NODATA), dtype=np.int32)
        landuse_values = np.full((rows, columns), int(NODATA), dtype=np.int32)
        elevation_values = np.full((rows, columns), NODATA, dtype=np.float32)
        slope_values = np.full((rows, columns), NODATA, dtype=np.float32)
        for cell in cells:
            row = cell.row - 1
            column = cell.column - 1
            soil_values[row, column] = cell.soil_id
            landuse_values[row, column] = cell.landuse_id
            if cell.elevation_m_asl is not None:
                elevation_values[row, column] = cell.elevation_m_asl
            if cell.slope_pct is not None:
                slope_values[row, column] = cell.slope_pct
        raster_paths = (
            output_dir / GRID_SOIL_NAME,
            output_dir / GRID_LANDUSE_NAME,
            output_dir / GRID_ELEVATION_NAME,
            output_dir / GRID_SLOPE_NAME,
        )
        _write_grid_raster(
            raster_paths[0],
            soil_values,
            geotransform,
            elevation.projection,
            gdal,
            unit="",
            description="dominant soil profile ID",
        )
        _write_grid_raster(
            raster_paths[1],
            landuse_values,
            geotransform,
            elevation.projection,
            gdal,
            unit="",
            description="allocated IdrAgra land-use ID",
        )
        _write_grid_raster(
            raster_paths[2],
            elevation_values,
            geotransform,
            elevation.projection,
            gdal,
            unit="m",
            description="aggregated elevation above mean sea level",
        )
        _write_grid_raster(
            raster_paths[3],
            slope_values,
            geotransform,
            elevation.projection,
            gdal,
            unit="percent",
            description="aggregated terrain slope",
        )

    stale_grid_paths = tuple(
        output_dir / name
        for name in (
            GRID_SOIL_NAME,
            GRID_LANDUSE_NAME,
            GRID_ELEVATION_NAME,
            GRID_SLOPE_NAME,
        )
    )
    if mode == "vector":
        for stale in stale_grid_paths:
            try:
                stale.unlink(missing_ok=True)
            except PermissionError as exc:
                raise RuntimeError(f"Could not remove stale grid output {stale}; close its QGIS layer and try again.") from exc

    manifest = Manifest(root)
    if mode == "vector":
        manifest.remove_assets(stale_grid_paths)
    common_request = {
        "mode": mode,
        "cell_width_m": float(cell_width_m) if mode == "grid" else None,
        "grid_boundary_policy": grid_boundary_policy if mode == "grid" else None,
        "elevation_method": elevation_method if mode == "grid" else "centroid_bilinear",
        "slope_method": slope_method if mode == "grid" else "centroid_bilinear",
        "grid_topography_policy": (
            "bilinear interpolation when cells are finer than the source raster; "
            "selected aggregation when cells are equal or coarser"
            if mode == "grid"
            else None
        ),
        "soil_source": str(soil_path),
        "landuse_source": str(landuse_path),
        "elevation_source": str(elevation_path),
        "slope_source": str(slope_path),
        "cell_count": len(cells),
        "warnings": warnings,
    }
    manifest.add_asset(
        cells_path,
        category="cells",
        provider="idragather",
        dataset="canonical IdrAgra simulation cells",
        source="normalized soil, land use, elevation and slope",
        request=common_request,
    )
    manifest.add_asset(
        configuration_path,
        category="cells",
        provider="idragather",
        dataset="crop-rotation catalogue and source-class allocations",
        source="user-edited cell-builder configuration",
        request={"schema_version": 1},
    )
    for raster_path in raster_paths:
        manifest.add_asset(
            raster_path,
            category="cells",
            provider="idragather",
            dataset=f"simulation-cell grid {raster_path.stem}",
            source=str(cells_path),
            request=common_request,
        )
    elevation.close()
    slope.close()
    if on_status:
        on_status(f"Cell view ready in {output_dir}.")
    return CellBuildResult(
        cells_path,
        configuration_path,
        raster_paths,
        len(cells),
        requested,
        generated,
        tuple(warnings),
    )


# Assign indivisible areas deterministically while approaching target shares.
def allocate_landuse_ids(
    items: Sequence[tuple[str, float]],
    allocations: Sequence[LandUseAllocation],
) -> tuple[dict[str, int], dict[int, float], dict[int, float]]:
    if not items:
        return {}, {}, {}
    by_source: dict[str, list[tuple[str, float]]] = {}
    for key, area in items:
        if area <= 0:
            raise ValueError("Cell allocation areas must be positive.")
        source, _, identity = key.partition("\0")
        by_source.setdefault(source, []).append((identity, float(area)))
    rules: dict[str, list[LandUseAllocation]] = {}
    for rule in allocations:
        rules.setdefault(rule.source_class.strip(), []).append(rule)

    assigned: dict[str, int] = {}
    requested: dict[int, float] = {}
    generated: dict[int, float] = {}
    for source, source_items in sorted(by_source.items()):
        source_rules = rules.get(source)
        if not source_rules:
            raise ValueError(f"No IdrAgra land-use allocation for {source!r}.")
        total_area = sum(area for _, area in source_items)
        targets = {rule.landuse_id: total_area * rule.share_pct / 100.0 for rule in source_rules}
        actual = {landuse_id: 0.0 for landuse_id in targets}
        for landuse_id, area in targets.items():
            requested[landuse_id] = requested.get(landuse_id, 0.0) + area
        ordered = sorted(
            source_items,
            key=lambda pair: hashlib.sha256(f"{source}\0{pair[0]}".encode("utf-8")).digest(),
        )
        for identity, area in ordered:
            landuse_id = max(
                sorted(targets),
                key=lambda candidate: targets[candidate] - actual[candidate],
            )
            assigned[f"{source}\0{identity}"] = landuse_id
            actual[landuse_id] += area
            generated[landuse_id] = generated.get(landuse_id, 0.0) + area
    return assigned, requested, generated


class _RasterSampler:
    def __init__(self, path, gdal, ogr, osr):
        self.gdal = gdal
        self.ogr = ogr
        self.dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
        if self.dataset is None or self.dataset.RasterCount < 1:
            raise ValueError(f"GDAL could not open raster: {path}")
        self.band = self.dataset.GetRasterBand(1)
        self.geotransform = self.dataset.GetGeoTransform()
        if self.geotransform[2] or self.geotransform[4]:
            raise ValueError(f"Rotated rasters are not supported: {path}")
        self.inverse = gdal.InvGeoTransform(self.geotransform)
        if len(self.inverse) == 2 and isinstance(self.inverse[0], (bool, int)):
            if not self.inverse[0]:
                raise ValueError(f"Raster geotransform cannot be inverted: {path}")
            self.inverse = self.inverse[1]
        self.projection = self.dataset.GetProjectionRef()
        if not self.projection:
            raise ValueError(f"Raster has no coordinate system: {path}")
        self.spatial_reference = osr.SpatialReference()
        self.spatial_reference.ImportFromWkt(self.projection)
        self.nodata = self.band.GetNoDataValue()

    @property
    def extent(self):
        gt = self.geotransform
        return (
            gt[0],
            gt[3] + self.dataset.RasterYSize * gt[5],
            gt[0] + self.dataset.RasterXSize * gt[1],
            gt[3],
        )

    def aggregate(self, geometry, method):
        import numpy as np

        if method == "centroid":
            point = geometry.Centroid()
            return self.sample_bilinear(point.GetX(), point.GetY())
        values = self.values(geometry)
        if values.size == 0:
            return None
        if method == "mean":
            return float(np.mean(values))
        if method == "median":
            return float(np.median(values))
        return dominant_continuous_value(values)

    def values(self, geometry):
        import numpy as np

        min_x, max_x, min_y, max_y = geometry.GetEnvelope()
        corners = [
            self.gdal.ApplyGeoTransform(self.inverse, x, y)
            for x, y in ((min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y))
        ]
        col0 = max(0, int(math.floor(min(item[0] for item in corners))))
        col1 = min(
            self.dataset.RasterXSize,
            int(math.ceil(max(item[0] for item in corners))),
        )
        row0 = max(0, int(math.floor(min(item[1] for item in corners))))
        row1 = min(
            self.dataset.RasterYSize,
            int(math.ceil(max(item[1] for item in corners))),
        )
        if col1 <= col0 or row1 <= row0:
            return np.empty(0, dtype=np.float64)
        array = self.band.ReadAsArray(col0, row0, col1 - col0, row1 - row0)
        local_gt = list(self.geotransform)
        local_gt[0], local_gt[3] = self.gdal.ApplyGeoTransform(self.geotransform, col0, row0)
        mask = self.gdal.GetDriverByName("MEM").Create("", col1 - col0, row1 - row0, 1, self.gdal.GDT_Byte)
        mask.SetGeoTransform(tuple(local_gt))
        mask.SetProjection(self.projection)
        memory_driver = self.ogr.GetDriverByName("Memory")
        vector = memory_driver.CreateDataSource("")
        layer = vector.CreateLayer("mask", self.spatial_reference, self.ogr.wkbUnknown)
        feature = self.ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(geometry)
        layer.CreateFeature(feature)
        self.gdal.RasterizeLayer(mask, [1], layer, burn_values=[1])
        selected = mask.GetRasterBand(1).ReadAsArray().astype(bool)
        valid = selected & np.isfinite(array)
        if self.nodata is not None:
            valid &= ~np.isclose(array, self.nodata)
        result = np.asarray(array[valid], dtype=np.float64)
        feature = layer = vector = mask = None
        return result

    # Resample the source into one complete value per target grid cell.
    #
    # Aggregation methods only make sense when a target cell covers multiple
    # source pixels. For a finer target grid, bilinear interpolation avoids
    # the empty-cell pattern caused by looking only for source pixel centres.
    def grid_values(self, rows, columns, geotransform, method):
        import numpy as np

        target_resolution = abs(float(geotransform[1]))
        source_resolution = max(abs(float(self.geotransform[1])), abs(float(self.geotransform[5])))
        upsampling = target_resolution < source_resolution
        resampling = (
            "bilinear"
            if upsampling
            else {
                "mean": "average",
                "median": "med",
                "dominant": "mode",
                "centroid": "bilinear",
            }[method]
        )
        source = self.dataset
        quantized = None
        if method == "dominant" and not upsampling:
            # GDAL's mode resampler compares exact values. Quantizing to the
            # documented one-unit bands makes it meaningful for continuous DEM
            # and percent-slope rasters.
            values = self.band.ReadAsArray().astype(np.float32)
            valid = np.isfinite(values)
            if self.nodata is not None:
                valid &= ~np.isclose(values, self.nodata)
            values[valid] = np.floor(values[valid] + 0.5)
            values[~valid] = NODATA
            quantized = self.gdal.GetDriverByName("MEM").Create(
                "",
                self.dataset.RasterXSize,
                self.dataset.RasterYSize,
                1,
                self.gdal.GDT_Float32,
            )
            quantized.SetGeoTransform(self.geotransform)
            quantized.SetProjection(self.projection)
            quantized_band = quantized.GetRasterBand(1)
            quantized_band.WriteArray(values)
            quantized_band.SetNoDataValue(NODATA)
            quantized_band = None
            source = quantized
        left = geotransform[0]
        top = geotransform[3]
        right = left + columns * geotransform[1]
        bottom = top + rows * geotransform[5]
        warped = self.gdal.Warp(
            "",
            source,
            options=self.gdal.WarpOptions(
                format="MEM",
                outputBounds=(left, bottom, right, top),
                width=columns,
                height=rows,
                dstSRS=self.projection,
                resampleAlg=resampling,
                srcNodata=(self.nodata if self.nodata is not None else NODATA),
                dstNodata=NODATA,
                outputType=self.gdal.GDT_Float32,
            ),
        )
        quantized = None
        if warped is None:
            raise RuntimeError("GDAL could not resample topography to the cell grid.")
        result = warped.GetRasterBand(1).ReadAsArray().astype(np.float64)
        warped = None
        return result

    def sample_bilinear(self, x, y):
        import numpy as np

        pixel_x, pixel_y = self.gdal.ApplyGeoTransform(self.inverse, x, y)
        centre_x = pixel_x - 0.5
        centre_y = pixel_y - 0.5
        col0 = int(math.floor(centre_x))
        row0 = int(math.floor(centre_y))
        dx = centre_x - col0
        dy = centre_y - row0
        total = 0.0
        total_weight = 0.0
        for row, wy in ((row0, 1.0 - dy), (row0 + 1, dy)):
            for column, wx in ((col0, 1.0 - dx), (col0 + 1, dx)):
                if not (0 <= row < self.dataset.RasterYSize and 0 <= column < self.dataset.RasterXSize):
                    continue
                value = float(self.band.ReadAsArray(column, row, 1, 1)[0, 0])
                if not np.isfinite(value) or (self.nodata is not None and np.isclose(value, self.nodata)):
                    continue
                weight = wx * wy
                total += value * weight
                total_weight += weight
        return total / total_weight if total_weight else None

    def close(self):
        self.band = None
        self.dataset = None


def _read_workspace_bbox(root: Path) -> BoundingBox:
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("The workspace manifest has no study-area definition.")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    aoi = data.get("aoi") or {}
    try:
        return BoundingBox(
            float(aoi["west"]),
            float(aoi["south"]),
            float(aoi["east"]),
            float(aoi["north"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("The workspace manifest has no valid EPSG:4326 AOI.") from exc


def _read_features(path, field_name, target_srs, ogr, osr, *, integer=False):
    database = ogr.Open(str(path), 0)
    if database is None:
        raise ValueError(f"OGR could not open vector input: {path}")
    layer = database.GetLayer(0)
    if layer is None or layer.GetSpatialRef() is None:
        database = None
        raise ValueError(f"Vector input has no layer or coordinate system: {path}")
    definition = layer.GetLayerDefn()
    field_index = next(
        (
            index
            for index in range(definition.GetFieldCount())
            if definition.GetFieldDefn(index).GetName().casefold() == field_name.casefold()
        ),
        -1,
    )
    if field_index < 0:
        database = None
        raise ValueError(f"Vector input {path} has no {field_name!r} field.")
    transform = None
    source_srs = layer.GetSpatialRef().Clone()
    destination_srs = target_srs.Clone()
    axis_strategy = getattr(osr, "OAMS_TRADITIONAL_GIS_ORDER", None)
    if axis_strategy is not None:
        source_srs.SetAxisMappingStrategy(axis_strategy)
        destination_srs.SetAxisMappingStrategy(axis_strategy)
    if not source_srs.IsSame(destination_srs):
        transform = osr.CoordinateTransformation(source_srs, destination_srs)
    result = []
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            continue
        geometry = geometry.Clone()
        if transform is not None:
            geometry.Transform(transform)
        if hasattr(geometry, "MakeValid") and not geometry.IsValid():
            geometry = geometry.MakeValid()
        value = feature.GetField(field_index)
        result.append((int(value) if integer else str(value).strip(), geometry))
    database = None
    if not result:
        raise ValueError(f"Vector input contains no usable polygon features: {path}")
    return result


def _build_grid_cells(
    elevation,
    slope,
    soil_features,
    landuse_features,
    aoi,
    cell_width,
    boundary_policy,
    elevation_method,
    slope_method,
    ogr,
):
    min_x, min_y, max_x, max_y = elevation.extent
    origin_x = math.floor(min_x / cell_width) * cell_width
    origin_y = math.ceil(max_y / cell_width) * cell_width
    columns = int(math.ceil((max_x - origin_x) / cell_width))
    rows = int(math.ceil((origin_y - min_y) / cell_width))
    grid_geotransform = (origin_x, cell_width, 0.0, origin_y, 0.0, -cell_width)
    elevation_values = elevation.grid_values(rows, columns, grid_geotransform, elevation_method)
    slope_values = slope.grid_values(rows, columns, grid_geotransform, slope_method)
    cells = []
    for row in range(rows):
        top = origin_y - row * cell_width
        bottom = top - cell_width
        for column in range(columns):
            left = origin_x + column * cell_width
            right = left + cell_width
            square = _rectangle(left, bottom, right, top, ogr)
            if not square.Intersects(aoi):
                continue
            active = square.Intersection(aoi)
            if active is None or active.IsEmpty() or active.GetArea() <= 0:
                continue
            square_area = cell_width * cell_width
            aoi_fraction = min(1.0, active.GetArea() / square_area)
            if boundary_policy == "inside" and not math.isclose(aoi_fraction, 1.0, rel_tol=0.0, abs_tol=1e-9):
                continue
            soil_id, soil_coverage = _dominant_category(active, soil_features)
            source_landuse, landuse_coverage = _dominant_category(active, landuse_features)
            if soil_id is None or source_landuse is None:
                continue
            elevation_value = _valid_grid_value(elevation_values[row, column])
            slope_value = _valid_grid_value(slope_values[row, column])
            if elevation_value is None or slope_value is None:
                centroid = active.Centroid()
                if elevation_value is None:
                    elevation_value = elevation.sample_bilinear(centroid.GetX(), centroid.GetY())
                if slope_value is None:
                    slope_value = slope.sample_bilinear(centroid.GetX(), centroid.GetY())
            cells.append(
                _Cell(
                    square,
                    int(soil_id),
                    str(source_landuse),
                    soil_coverage,
                    landuse_coverage,
                    elevation_value,
                    slope_value,
                    row=row + 1,
                    column=column + 1,
                    aoi_fraction=(1.0 if boundary_policy == "inside" else aoi_fraction),
                )
            )
    return cells, (
        rows,
        columns,
        grid_geotransform,
    )


def _valid_grid_value(value):
    value = float(value)
    if not math.isfinite(value) or math.isclose(value, NODATA, abs_tol=1e-4):
        return None
    return value


def _build_vector_cells(elevation, slope, soil_features, landuse_features, aoi, ogr):
    grouped: dict[tuple[int, str], list] = {}
    for soil_id, soil_geometry in soil_features:
        if not soil_geometry.Intersects(aoi):
            continue
        soil_clip = soil_geometry.Intersection(aoi)
        if soil_clip is None or soil_clip.IsEmpty():
            continue
        for source_landuse, landuse_geometry in landuse_features:
            if not _envelopes_overlap(soil_clip, landuse_geometry):
                continue
            if not soil_clip.Intersects(landuse_geometry):
                continue
            intersection = soil_clip.Intersection(landuse_geometry)
            if intersection is None or intersection.IsEmpty():
                continue
            for polygon in _polygon_parts(intersection, ogr):
                if polygon.GetArea() > 0:
                    grouped.setdefault((int(soil_id), str(source_landuse)), []).append(polygon)

    cells = []
    for (soil_id, source_landuse), geometries in sorted(grouped.items()):
        multipolygon = ogr.Geometry(ogr.wkbMultiPolygon)
        for geometry in geometries:
            multipolygon.AddGeometry(geometry)
        dissolved = multipolygon.UnionCascaded()
        for polygon in _polygon_parts(dissolved, ogr):
            if polygon.GetArea() <= 0:
                continue
            centroid = polygon.Centroid()
            cells.append(
                _Cell(
                    polygon,
                    soil_id,
                    source_landuse,
                    100.0,
                    100.0,
                    elevation.sample_bilinear(centroid.GetX(), centroid.GetY()),
                    slope.sample_bilinear(centroid.GetX(), centroid.GetY()),
                )
            )
    return cells


def _dominant_category(active, features):
    areas: dict[object, float] = {}
    for value, geometry in features:
        if not _envelopes_overlap(active, geometry) or not active.Intersects(geometry):
            continue
        intersection = active.Intersection(geometry)
        if intersection is not None and not intersection.IsEmpty():
            areas[value] = areas.get(value, 0.0) + float(intersection.GetArea())
    if not areas:
        return None, 0.0
    value = max(sorted(areas, key=str), key=lambda item: areas[item])
    return value, min(100.0, 100.0 * areas[value] / active.GetArea())


def _rectangle(left, bottom, right, top, ogr):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((left, bottom), (right, bottom), (right, top), (left, top), (left, bottom)):
        ring.AddPoint_2D(x, y)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


def _envelopes_overlap(first, second):
    a_min_x, a_max_x, a_min_y, a_max_y = first.GetEnvelope()
    b_min_x, b_max_x, b_min_y, b_max_y = second.GetEnvelope()
    return not (a_max_x < b_min_x or b_max_x < a_min_x or a_max_y < b_min_y or b_max_y < a_min_y)


def _centroid_key(geometry):
    point = geometry.Centroid()
    return f"{point.GetX():.6f}:{point.GetY():.6f}"


def _spatial_sort_key(geometry):
    point = geometry.Centroid()
    return (-round(point.GetY(), 6), round(point.GetX(), 6))


def _write_cells(path, cells, mode, landuse_by_id, spatial_reference, ogr, osr):
    temporary = path.with_name(path.stem + ".tmp.gpkg")
    temporary.unlink(missing_ok=True)
    driver = ogr.GetDriverByName("GPKG")
    database = driver.CreateDataSource(str(temporary))
    if database is None:
        raise RuntimeError(f"Could not create {temporary}")
    layer = database.CreateLayer(CELLS_LAYER, spatial_reference, ogr.wkbPolygon)
    fields = (
        ("cell_id", ogr.OFTInteger64),
        ("mode", ogr.OFTString),
        ("row", ogr.OFTInteger),
        ("column", ogr.OFTInteger),
        ("soil_id", ogr.OFTInteger),
        ("landuse_id", ogr.OFTInteger),
        ("landuse_name", ogr.OFTString),
        ("source_landuse", ogr.OFTString),
        ("elevation_m_asl", ogr.OFTReal),
        ("slope_pct", ogr.OFTReal),
        ("area_m2", ogr.OFTReal),
        ("centroid_x", ogr.OFTReal),
        ("centroid_y", ogr.OFTReal),
        ("latitude", ogr.OFTReal),
        ("aoi_fraction", ogr.OFTReal),
        ("soil_coverage_pct", ogr.OFTReal),
        ("landuse_coverage_pct", ogr.OFTReal),
    )
    for name, field_type in fields:
        field = ogr.FieldDefn(name, field_type)
        if field_type == ogr.OFTString:
            field.SetWidth(160)
        if layer.CreateField(field) != 0:
            database = None
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"Could not create cell field {name!r}")
    source_srs = spatial_reference.Clone()
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    axis_strategy = getattr(osr, "OAMS_TRADITIONAL_GIS_ORDER", None)
    if axis_strategy is not None:
        source_srs.SetAxisMappingStrategy(axis_strategy)
        wgs84.SetAxisMappingStrategy(axis_strategy)
    to_wgs84 = osr.CoordinateTransformation(source_srs, wgs84)
    definition = layer.GetLayerDefn()
    database.StartTransaction()
    try:
        for cell in cells:
            feature = ogr.Feature(definition)
            feature.SetField("cell_id", cell.cell_id)
            feature.SetField("mode", mode)
            if cell.row is not None:
                feature.SetField("row", cell.row)
                feature.SetField("column", cell.column)
            feature.SetField("soil_id", cell.soil_id)
            feature.SetField("landuse_id", cell.landuse_id)
            feature.SetField("landuse_name", landuse_by_id[cell.landuse_id].name)
            feature.SetField("source_landuse", cell.source_landuse)
            if cell.elevation_m_asl is not None:
                feature.SetField("elevation_m_asl", cell.elevation_m_asl)
            if cell.slope_pct is not None:
                feature.SetField("slope_pct", cell.slope_pct)
            feature.SetField("area_m2", cell.area_m2)
            centroid = cell.geometry.Centroid()
            feature.SetField("centroid_x", centroid.GetX())
            feature.SetField("centroid_y", centroid.GetY())
            geographic = centroid.Clone()
            geographic.Transform(to_wgs84)
            feature.SetField("latitude", geographic.GetY())
            feature.SetField("aoi_fraction", cell.aoi_fraction)
            feature.SetField("soil_coverage_pct", cell.soil_coverage_pct)
            feature.SetField("landuse_coverage_pct", cell.landuse_coverage_pct)
            parts = list(_polygon_parts(cell.geometry, ogr))
            if not parts:
                continue
            feature.SetGeometry(parts[0])
            if layer.CreateFeature(feature) != 0:
                raise RuntimeError(f"Could not write simulation cell {cell.cell_id}")
            feature = None
        database.CommitTransaction()
        database.ExecuteSQL("CREATE INDEX IF NOT EXISTS simulation_cells_landuse_id ON simulation_cells (landuse_id)")
        database.ExecuteSQL("CREATE INDEX IF NOT EXISTS simulation_cells_soil_id ON simulation_cells (soil_id)")
    except Exception:
        database.RollbackTransaction()
        raise
    finally:
        definition = layer = database = None
    _replace_file(temporary, path)


def _polygon_parts(geometry, ogr):
    kind = ogr.GT_Flatten(geometry.GetGeometryType())
    if kind == ogr.wkbPolygon:
        yield geometry.Clone()
    elif kind in {ogr.wkbMultiPolygon, ogr.wkbGeometryCollection}:
        for index in range(geometry.GetGeometryCount()):
            yield from _polygon_parts(geometry.GetGeometryRef(index), ogr)


def _write_grid_raster(path, values, geotransform, projection, gdal, *, unit, description):
    temporary = path.with_name(path.stem + ".tmp.tif")
    temporary.unlink(missing_ok=True)
    data_type = gdal.GDT_Int32 if values.dtype.kind in "iu" else gdal.GDT_Float32
    dataset = gdal.GetDriverByName("GTiff").Create(
        str(temporary),
        values.shape[1],
        values.shape[0],
        1,
        data_type,
        options=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"],
    )
    if dataset is None:
        raise RuntimeError(f"Could not create {temporary}")
    dataset.SetGeoTransform(geotransform)
    dataset.SetProjection(projection)
    band = dataset.GetRasterBand(1)
    band.WriteArray(values)
    band.SetNoDataValue(int(NODATA) if values.dtype.kind in "iu" else NODATA)
    band.SetDescription(description)
    if unit:
        band.SetUnitType(unit)
    band.FlushCache()
    band = dataset = None
    _replace_file(temporary, path)


def _replace_file(temporary: Path, target: Path):
    try:
        target.unlink(missing_ok=True)
        temporary.replace(target)
    except PermissionError as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Could not replace {target}; close its loaded QGIS layer and try again.") from exc
