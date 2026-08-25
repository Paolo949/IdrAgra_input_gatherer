import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from IdrAgraGather.core.soil_ptf import (
    LAYERS_LAYER,
    METADATA_LAYER,
    apply_rosetta3_to_workspace,
    predict_rosetta3,
    rosetta_field_groups,
    van_genuchten_theta,
)


class SoilPtfTests(unittest.TestCase):
    def test_rosetta3_h3_matches_upstream_reference_result_and_converts_units(self):
        result = predict_rosetta3([[55.0, 25.0, 20.0, 1.1]])
        self.assertEqual(result.model_code, 3)
        self.assertEqual(
            result.fields,
            ("theta_res", "theta_sat", "vg_alpha", "vg_n", "ksat_mm_h"),
        )
        np.testing.assert_allclose(
            result.values[0],
            (
                0.09130753033144609,
                0.485031958049669,
                0.009748718589139088 / 10.0,
                1.4171964842691995,
                84.7833726853498 * 10.0 / 24.0,
            ),
            rtol=1e-12,
        )
        np.testing.assert_allclose(
            result.standard_deviations[0],
            (
                0.012777797583517441,
                0.013068706563916467,
                0.0022148332082555515 / 10.0,
                0.05767077407189314,
                27.035739850886362 * 10.0 / 24.0,
            ),
            rtol=1e-12,
        )

    def test_rosetta3_h2_and_field_roles_do_not_claim_bulk_density(self):
        result = predict_rosetta3([[20.0, 60.0, 20.0]], use_bulk_density=False)
        self.assertEqual(result.model_code, 2)
        np.testing.assert_allclose(
            result.values[0, :4],
            (0.08994502219206939, 0.4301366480210401, 0.00038032951212636345,
             1.4992978643076722),
            rtol=1e-12,
        )
        fields = rosetta_field_groups(use_bulk_density=False)
        self.assertEqual(fields.required, ("sand_pct", "silt_pct", "clay_pct"))
        self.assertIn("bd_g_cm3", fields.unused)
        self.assertIn("vg_alpha", fields.generated)

    def test_van_genuchten_derived_water_contents_are_ordered(self):
        result = predict_rosetta3([[55.0, 25.0, 20.0, 1.1]])
        theta_res, theta_sat, alpha, n_value, _ksat = result.values[0]
        theta_fc = float(
            van_genuchten_theta(33.0, theta_res, theta_sat, alpha, n_value)
        )
        theta_wp = float(
            van_genuchten_theta(1500.0, theta_res, theta_sat, alpha, n_value)
        )
        self.assertGreater(theta_sat, theta_fc)
        self.assertGreater(theta_fc, theta_wp)
        self.assertGreater(theta_wp, theta_res)
        self.assertAlmostEqual(theta_fc, 0.3194293965913354)
        self.assertAlmostEqual(theta_wp, 0.14009429537639662)

    def test_input_validation_reports_texture_and_density_domains(self):
        with self.assertRaisesRegex(ValueError, "summing to 99-101"):
            predict_rosetta3([[20.0, 20.0, 20.0]], use_bulk_density=False)
        with self.assertRaisesRegex(ValueError, "0.5-2.0"):
            predict_rosetta3([[40.0, 35.0, 25.0, 2.1]])

    def test_workspace_output_replaces_the_previous_ptf_result(self):
        try:
            from osgeo import ogr
        except ImportError:
            self.skipTest("GDAL Python bindings are not installed")

        ogr.UseExceptions()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            soil_dir = root / "soil"
            soil_dir.mkdir()
            source = soil_dir / "soil_profiles.gpkg"
            database = ogr.GetDriverByName("GPKG").CreateDataSource(str(source))
            layer = database.CreateLayer("soil_profiles", None, ogr.wkbPolygon)
            layer.CreateField(ogr.FieldDefn("profile_id", ogr.OFTInteger))
            for horizon, (top, bottom) in enumerate(
                ((0, 5), (5, 15), (15, 30), (30, 60), (60, 100), (100, 200)),
                start=1,
            ):
                for name, field_type in (
                    ("top_cm", ogr.OFTInteger),
                    ("bottom_cm", ogr.OFTInteger),
                    ("sand_pct", ogr.OFTReal),
                    ("silt_pct", ogr.OFTReal),
                    ("clay_pct", ogr.OFTReal),
                    ("skel_pct", ogr.OFTReal),
                    ("oc_pct", ogr.OFTReal),
                    ("bd_g_cm3", ogr.OFTReal),
                ):
                    layer.CreateField(
                        ogr.FieldDefn(f"h{horizon}_{name}", field_type)
                    )
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField("profile_id", 7)
            for horizon, (top, bottom) in enumerate(
                ((0, 5), (5, 15), (15, 30), (30, 60), (60, 100), (100, 200)),
                start=1,
            ):
                for name, value in (
                    ("top_cm", top),
                    ("bottom_cm", bottom),
                    ("sand_pct", 55.0),
                    ("silt_pct", 25.0),
                    ("clay_pct", 20.0),
                    ("skel_pct", 12.0),
                    ("oc_pct", 2.35),
                    ("bd_g_cm3", 1.1),
                ):
                    feature.SetField(f"h{horizon}_{name}", value)
            feature.SetGeometry(ogr.CreateGeometryFromWkt(
                "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"
            ))
            layer.CreateFeature(feature)
            database = None

            first = apply_rosetta3_to_workspace(root)
            second = apply_rosetta3_to_workspace(root, use_bulk_density=False)
            self.assertEqual(first.profile_count, 1)
            self.assertEqual(first.horizon_count, 6)
            self.assertEqual(first.path, second.path)

            database = ogr.Open(str(second.path))
            self.assertEqual(database.GetLayerCount(), 2)
            metadata = database.GetLayerByName(METADATA_LAYER)
            self.assertEqual(metadata.GetFeatureCount(), 1)
            current = next(iter(metadata))
            self.assertEqual(current.GetField("model_code"), 2)
            self.assertEqual(current.GetField("theta_fc_kpa"), 33.0)
            self.assertEqual(current.GetField("theta_wp_kpa"), 1500.0)
            self.assertEqual(current.GetField("vg_constraint"), "m = 1 - 1/n")
            self.assertEqual(current.GetField("cf_correction"), 0)
            self.assertEqual(database.GetLayerByName(LAYERS_LAYER).GetFeatureCount(), 6)
            layers = database.GetLayerByName(LAYERS_LAYER)
            fields = {
                layers.GetLayerDefn().GetFieldDefn(index).GetName()
                for index in range(layers.GetLayerDefn().GetFieldCount())
            }
            for name in (
                "ksat_mm_h", "theta_sat", "theta_fc", "theta_wp", "theta_res",
                "vg_alpha", "vg_n", "vg_m",
            ):
                self.assertIn(name, fields)
            self.assertNotIn("run_id", fields)
            database = None

            manifest = json.loads((root / "manifest.json").read_text())
            request = next(
                item["request"] for item in manifest["assets"]
                if item["path"] == "soil/soil_hydraulics.gpkg"
            )
            self.assertEqual(request["model_hierarchy"], "H2")
            self.assertFalse(request["coarse_fragment_correction"])


if __name__ == "__main__":
    unittest.main()
