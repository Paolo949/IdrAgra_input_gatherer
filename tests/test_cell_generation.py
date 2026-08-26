import json
import math
import tempfile
import unittest
from pathlib import Path

from IdrAgraGather.core.cells import build_simulation_cells
from IdrAgraGather.core.landuses import LandUseAllocation, LandUseDefinition
from IdrAgraGather.core.models import BoundingBox
from IdrAgraGather.core.staging import StagingArea
from IdrAgraGather.core.vector_clip import aoi_geometry

try:
    from osgeo import gdal, ogr, osr
    import numpy as np
except ImportError:  # The ordinary lightweight test interpreter has no GDAL.
    gdal = ogr = osr = np = None


@unittest.skipIf(gdal is None, "GDAL/OGR is provided by QGIS")
class CellGenerationTests(unittest.TestCase):
    def test_grid_and_vector_outputs_from_normalized_inputs(self):
        bbox = BoundingBox(9.0, 45.0, 9.02, 45.02)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("soil", "landuse", "topography"):
                (root / name).mkdir()
            spatial_reference = osr.SpatialReference()
            spatial_reference.ImportFromEPSG(32632)
            aoi_path = StagingArea(root).write_aoi(
                bbox,
                geometry={
                    "type": "Polygon",
                    "coordinates": [[[9.0, 45.0], [9.02, 45.0], [9.02, 45.02], [9.0, 45.02], [9.0, 45.0]]],
                },
            )
            aoi = aoi_geometry(aoi_path, spatial_reference, ogr, osr)
            min_x, max_x, min_y, max_y = aoi.GetEnvelope()
            resolution = 100.0
            left = math.floor(min_x / resolution) * resolution
            top = math.ceil(max_y / resolution) * resolution
            columns = math.ceil((max_x - left) / resolution)
            rows = math.ceil((top - min_y) / resolution)
            extent = (left, top - rows * resolution, left + columns * resolution, top)
            self._write_raster(
                root / "topography" / "elevation_m_asl.tif",
                np.full((rows, columns), 10.0, dtype=np.float32),
                (left, resolution, 0, top, 0, -resolution),
                spatial_reference,
            )
            slope_values = np.full((rows, columns), 2.0, dtype=np.float32)
            slope_values[:, -1] = 70.0
            self._write_raster(
                root / "topography" / "slope_pct.tif",
                slope_values,
                (left, resolution, 0, top, 0, -resolution),
                spatial_reference,
            )
            self._write_polygon_layer(
                root / "soil" / "soil_profiles.gpkg",
                "GPKG",
                "soil_profiles",
                "profile_id",
                [(1, self._rectangle(*extent))],
                spatial_reference,
                ogr.OFTInteger,
            )
            middle = (extent[0] + extent[2]) / 2.0
            self._write_polygon_layer(
                root / "landuse" / "landuse.shp",
                "ESRI Shapefile",
                "landuse",
                "landuse",
                [
                    ("A", self._rectangle(extent[0], extent[1], middle, extent[3])),
                    ("B", self._rectangle(middle, extent[1], extent[2], extent[3])),
                ],
                spatial_reference,
                ogr.OFTString,
            )
            landuses = [LandUseDefinition(1, "Use A"), LandUseDefinition(2, "Use B")]
            allocations = [
                LandUseAllocation("A", 1, 100.0),
                LandUseAllocation("B", 2, 100.0),
            ]

            grid = build_simulation_cells(
                root,
                mode="grid",
                crops=[],
                landuses=landuses,
                allocations=allocations,
                cell_width_m=500.0,
            )
            self.assertGreater(grid.cell_count, 0)
            self.assertEqual(len(grid.raster_paths), 4)
            self.assertTrue(all(path.is_file() for path in grid.paths))
            cells = ogr.Open(str(grid.cells_path), 0)
            layer = cells.GetLayerByName("simulation_cells")
            self.assertEqual(layer.GetFeatureCount(), grid.cell_count)
            self.assertGreaterEqual(layer.GetLayerDefn().GetFieldIndex("latitude"), 0)
            slopes = []
            for feature in layer:
                slopes.append(feature.GetField("slope_pct"))
                self.assertAlmostEqual(feature.GetGeometryRef().GetArea(), 500.0 ** 2)
                self.assertEqual(feature.GetField("aoi_fraction"), 1.0)
            self.assertIn(2.0, slopes)
            cells = None

            crossing_grid = build_simulation_cells(
                root,
                mode="grid",
                crops=[],
                landuses=landuses,
                allocations=allocations,
                cell_width_m=500.0,
                grid_boundary_policy="intersect",
            )
            self.assertGreater(crossing_grid.cell_count, grid.cell_count)
            crossing_cells = ogr.Open(str(crossing_grid.cells_path), 0)
            crossing_layer = crossing_cells.GetLayerByName("simulation_cells")
            fractions = []
            for feature in crossing_layer:
                fractions.append(feature.GetField("aoi_fraction"))
                self.assertAlmostEqual(feature.GetGeometryRef().GetArea(), 500.0 ** 2)
            self.assertTrue(any(fraction < 1.0 for fraction in fractions))
            crossing_cells = None
            manifest = json.loads((root / "manifest.json").read_text())
            cell_asset = next(
                item for item in manifest["assets"]
                if item["path"] == "cells/simulation_cells.gpkg"
            )
            self.assertEqual(
                cell_asset["request"]["grid_boundary_policy"], "intersect"
            )

            fine_grid = build_simulation_cells(
                root,
                mode="grid",
                crops=[],
                landuses=landuses,
                allocations=allocations,
                cell_width_m=50.0,
            )
            fine_cells = ogr.Open(str(fine_grid.cells_path), 0)
            fine_layer = fine_cells.GetLayerByName("simulation_cells")
            self.assertGreater(fine_layer.GetFeatureCount(), grid.cell_count)
            for feature in fine_layer:
                self.assertIsNotNone(feature.GetField("elevation_m_asl"))
                self.assertIsNotNone(feature.GetField("slope_pct"))
            fine_cells = None

            vector = build_simulation_cells(
                root,
                mode="vector",
                crops=[],
                landuses=landuses,
                allocations=allocations,
            )
            self.assertEqual(vector.cell_count, 2)
            self.assertEqual(vector.raster_paths, ())
            self.assertTrue(all(not path.exists() for path in grid.raster_paths))

    @staticmethod
    def _write_raster(path, values, geotransform, spatial_reference):
        dataset = gdal.GetDriverByName("GTiff").Create(
            str(path), values.shape[1], values.shape[0], 1, gdal.GDT_Float32
        )
        dataset.SetGeoTransform(geotransform)
        dataset.SetProjection(spatial_reference.ExportToWkt())
        band = dataset.GetRasterBand(1)
        band.WriteArray(values)
        band.SetNoDataValue(-9999.0)
        band = dataset = None

    @staticmethod
    def _write_polygon_layer(
        path, driver_name, layer_name, field_name, features, spatial_reference, field_type
    ):
        database = ogr.GetDriverByName(driver_name).CreateDataSource(str(path))
        layer = database.CreateLayer(layer_name, spatial_reference, ogr.wkbPolygon)
        layer.CreateField(ogr.FieldDefn(field_name, field_type))
        for value, geometry in features:
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField(field_name, value)
            feature.SetGeometry(geometry)
            layer.CreateFeature(feature)
        layer = database = None

    @staticmethod
    def _rectangle(left, bottom, right, top):
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in (
            (left, bottom), (right, bottom), (right, top), (left, top), (left, bottom)
        ):
            ring.AddPoint_2D(x, y)
        polygon = ogr.Geometry(ogr.wkbPolygon)
        polygon.AddGeometry(ring)
        return polygon


if __name__ == "__main__":
    unittest.main()
