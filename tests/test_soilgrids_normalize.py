import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from idragather.models import BoundingBox
from idragather.providers.soilgrids import DEPTHS, PROPERTIES
from idragather.soilgrids_normalize import (
    find_staged_files,
    normalize_soilgrids_arrays,
    normalize_soilgrids_files,
)


def complete_arrays(shape=(1, 2)):
    values = {
        "sand": np.full(shape, 400.0),
        "silt": np.full(shape, 350.0),
        "clay": np.full(shape, 250.0),
        "cfvo": np.full(shape, 120.0),
        "soc": np.full(shape, 235.0),
        "bdod": np.full(shape, 132.0),
    }
    return {(depth, name): values[name].copy() for depth in DEPTHS for name in PROPERTIES}


class SoilGridsNormalizeTests(unittest.TestCase):
    def test_units_texture_closure_and_six_horizons(self):
        raw = complete_arrays((1, 1))
        raw[(DEPTHS[0], "sand")][0, 0] = 410.0
        result = normalize_soilgrids_arrays(raw)
        self.assertEqual(result.profiles.shape, (1, 36))
        lookup = dict(zip(result.fields, result.profiles[0]))
        self.assertEqual(
            lookup["h1_sand_pct"] + lookup["h1_silt_pct"] + lookup["h1_clay_pct"],
            100.0,
        )
        self.assertEqual(lookup["h1_skel_pct"], 12.0)
        self.assertEqual(lookup["h1_oc_pct"], 2.35)
        self.assertEqual(lookup["h1_bd_g_cm3"], 1.32)
        self.assertIn("h6_bd_g_cm3", result.fields)

    def test_equal_complete_profiles_share_an_id_and_missing_cells_are_filled(self):
        raw = complete_arrays((1, 3))
        raw[(DEPTHS[3], "clay")][0, 1] = 300.0
        masks = {key: np.ones((1, 3), dtype=bool) for key in raw}
        masks[(DEPTHS[5], "soc")][0, 2] = False
        result = normalize_soilgrids_arrays(raw, masks)
        self.assertEqual(len(result.profiles), 2)
        self.assertGreater(result.zone_ids[0, 0], 0)
        self.assertNotEqual(result.zone_ids[0, 0], result.zone_ids[0, 1])
        self.assertGreater(result.zone_ids[0, 2], 0)
        self.assertEqual(result.filled_nodata_cells, 1)
        self.assertTrue(np.all(result.valid_mask))

    def test_maximum_class_count_clusters_similar_complete_profiles(self):
        raw = complete_arrays((2, 3))
        raw[(DEPTHS[0], "sand")] = np.array(
            [[380.0, 390.0, 400.0], [410.0, 420.0, 430.0]]
        )
        result = normalize_soilgrids_arrays(raw, max_classes=2)
        self.assertEqual(result.exact_profile_count, 6)
        self.assertEqual(len(result.profiles), 2)
        self.assertEqual(set(np.unique(result.zone_ids)), {1, 2})
        self.assertEqual(result.filled_nodata_cells, 0)

    def test_all_nodata_gaps_are_filled_including_boundary_corridors(self):
        raw = complete_arrays((5, 5))
        masks = {key: np.ones((5, 5), dtype=bool) for key in raw}
        masks[(DEPTHS[5], "soc")][2, 2] = False
        enclosed = normalize_soilgrids_arrays(raw, masks)
        self.assertEqual(enclosed.filled_nodata_cells, 1)
        self.assertEqual(enclosed.zone_ids[2, 2], 1)
        self.assertTrue(enclosed.valid_mask[2, 2])

        masks[(DEPTHS[5], "soc")][0:3, 2] = False
        boundary_connected = normalize_soilgrids_arrays(raw, masks)
        self.assertEqual(boundary_connected.filled_nodata_cells, 3)
        self.assertTrue(np.all(boundary_connected.zone_ids[0:3, 2] == 1))
        self.assertTrue(np.all(boundary_connected.valid_mask))

    def test_class_count_validation_and_optional_hole_fill(self):
        raw = complete_arrays((3, 3))
        masks = {key: np.ones((3, 3), dtype=bool) for key in raw}
        masks[(DEPTHS[0], "clay")][1, 1] = False
        with self.assertRaisesRegex(ValueError, "at least 1"):
            normalize_soilgrids_arrays(raw, max_classes=0)
        result = normalize_soilgrids_arrays(raw, masks, fill_nodata=False)
        self.assertEqual(result.filled_nodata_cells, 0)
        self.assertEqual(result.zone_ids[1, 1], 0)

    def test_staged_set_must_contain_all_36_coverages(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "raw" / "soil" / "soilgrids"
            directory.mkdir(parents=True)
            for depth in DEPTHS:
                for name in PROPERTIES:
                    (directory / f"{name}_{depth}_mean.tif").touch()
            self.assertEqual(len(find_staged_files(temporary)), 36)
            (directory / "soc_100-200cm_mean.tif").unlink()
            with self.assertRaisesRegex(ValueError, "Incomplete SoilGrids input"):
                find_staged_files(temporary)

    def test_gdal_pipeline_writes_polygon_profiles(self):
        try:
            from osgeo import gdal, ogr, osr
        except ImportError:
            self.skipTest("GDAL Python bindings are not installed")

        gdal.UseExceptions()
        ogr.UseExceptions()
        osr.UseExceptions()
        with tempfile.TemporaryDirectory() as temporary:
            source_dir = Path(temporary) / "raw" / "soil" / "soilgrids"
            source_dir.mkdir(parents=True)
            reference = complete_arrays((1, 2))
            reference[(DEPTHS[2], "clay")][0, 1] = 300.0
            spatial_reference = osr.SpatialReference()
            spatial_reference.ImportFromEPSG(4326)
            driver = gdal.GetDriverByName("GTiff")
            paths = []
            for depth in DEPTHS:
                for name in PROPERTIES:
                    path = source_dir / f"{name}_{depth}_mean.tif"
                    dataset = driver.Create(str(path), 2, 1, 1, gdal.GDT_Int16)
                    dataset.SetGeoTransform((9.0, 0.01, 0.0, 45.0, 0.0, -0.01))
                    dataset.SetProjection(spatial_reference.ExportToWkt())
                    dataset.GetRasterBand(1).WriteArray(reference[(depth, name)])
                    dataset = None
                    paths.append(path)

            clip = BoundingBox(9.005, 44.992, 9.015, 44.998)
            result = normalize_soilgrids_files(paths, temporary, bbox=clip)
            self.assertEqual(result.profile_count, 2)
            self.assertEqual(result.polygon_count, 2)
            self.assertEqual(result.exact_profile_count, 2)
            self.assertEqual(result.filled_nodata_cells, 0)
            database = ogr.Open(str(result.path))
            layer = database.GetLayerByName("soil_profiles")
            self.assertEqual(layer.GetFeatureCount(), 2)
            fields = {
                layer.GetLayerDefn().GetFieldDefn(index).GetName()
                for index in range(layer.GetLayerDefn().GetFieldCount())
            }
            self.assertIn("h1_oc_pct", fields)
            self.assertIn("h6_bd_g_cm3", fields)
            for feature in layer:
                geometry = feature.GetGeometryRef()
                self.assertGreater(geometry.GetArea(), 0)
                west, east, south, north = geometry.GetEnvelope()
                self.assertGreaterEqual(west, clip.west)
                self.assertLessEqual(east, clip.east)
                self.assertGreaterEqual(south, clip.south)
                self.assertLessEqual(north, clip.north)
            database = None
            manifest = json.loads((Path(temporary) / "manifest.json").read_text())
            request = manifest["assets"][0]["request"]
            self.assertEqual(request["classification"]["maximum_classes"], 20)
            self.assertEqual(request["classification"]["output_class_count"], 2)
            self.assertEqual(request["filled_nodata_cells"], 0)
            self.assertEqual(request["clip_aoi"], clip.as_dict())

            merged = normalize_soilgrids_files(paths, temporary, max_classes=1)
            self.assertEqual(merged.profile_count, 1)
            self.assertEqual(merged.polygon_count, 1)


if __name__ == "__main__":
    unittest.main()
