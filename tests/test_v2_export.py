import json
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from IdrAgraGather.core.landuses import (
    CropDefinition, LandUseAllocation, LandUseDefinition, write_configuration,
)
from IdrAgraGather.core.v2_export import (
    aggregate_profile_layers, capillary_rise_parameters, export_v2_workspace,
    usda_texture_class,
)

try:
    import numpy as np
    from osgeo import gdal, ogr, osr
except ImportError:
    np = gdal = ogr = osr = None


class V2SoilAggregationTests(unittest.TestCase):
    def test_legacy_two_layer_aggregation_and_n(self):
        horizons = []
        for top, bottom, ksat, fc in (
            (0, 5, 10.0, 0.30), (5, 15, 20.0, 0.32),
            (15, 30, 30.0, 0.34), (30, 60, 40.0, 0.36),
            (60, 100, 50.0, 0.38), (100, 200, 60.0, 0.40),
        ):
            horizons.append({
                "top_cm": top, "bottom_cm": bottom, "ksat_mm_h": ksat,
                "theta_fc": fc, "theta_wp": 0.15, "theta_res": 0.07,
                "theta_sat": 0.48,
            })
        first, second = aggregate_profile_layers(horizons)
        self.assertAlmostEqual(first["ksat_mm_h"], 13.333333333333334)
        self.assertAlmostEqual(first["theta_fc"], 0.31)
        self.assertAlmostEqual(
            first["v2_brooks_corey_n"],
            math.log((0.2 / 24.0) / first["ksat_mm_h"]) /
            math.log((first["theta_fc"] - 0.07) / (0.48 - 0.07)),
        )
        self.assertGreater(second["ksat_mm_h"], first["ksat_mm_h"])

    def test_export_destination_cannot_replace_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "cannot be the workspace"):
                export_v2_workspace(root, root, overwrite=True)

    def test_usda_classes_collapse_to_the_three_legacy_capillary_classes(self):
        examples = (
            ((90, 5, 5), 1, 101),
            ((40, 40, 20), 4, 102),
            ((30, 35, 35), 8, 103),
        )
        for texture, expected_usda, expected_macro in examples:
            self.assertEqual(usda_texture_class(*texture), expected_usda)
            result = capillary_rise_parameters(
                [{
                    "top_cm": 0, "bottom_cm": 200,
                    "sand_pct": texture[0], "silt_pct": texture[1],
                    "clay_pct": texture[2],
                }],
                1.0,
            )
            self.assertEqual(result["macro_texture_class"], expected_macro)

    def test_capillary_texture_is_selected_below_the_modeled_depth(self):
        result = capillary_rise_parameters(
            [
                {"top_cm": 0, "bottom_cm": 100, "sand_pct": 90, "silt_pct": 5, "clay_pct": 5},
                {"top_cm": 100, "bottom_cm": 200, "sand_pct": 30, "silt_pct": 35, "clay_pct": 35},
            ],
            1.0,
        )
        self.assertEqual(result["macro_texture_class"], 103)
        self.assertEqual(result["a3"], -1.4)
        self.assertEqual(result["b4"], -0.98)


@unittest.skipIf(gdal is None, "GDAL/OGR is provided by QGIS")
class V2ExportIntegrationTests(unittest.TestCase):
    def test_static_rainfed_export_writes_v2_names_units_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            for folder in ("cells", "soil", "weather"):
                (root / folder).mkdir(parents=True)
            srs = osr.SpatialReference(); srs.ImportFromEPSG(32632)
            transform = (500000.0, 250.0, 0.0, 5000500.0, 0.0, -250.0)
            self._raster(root / "cells" / "soil_id.tif", np.array([[1, 1], [1, 1]], dtype=np.int32), transform, srs)
            self._raster(root / "cells" / "landuse_id.tif", np.array([[7, 7], [7, 8]], dtype=np.int32), transform, srs)
            self._raster(root / "cells" / "slope_pct.tif", np.full((2, 2), 2.5, dtype=np.float32), transform, srs)
            self._raster(root / "cells" / "elevation_m_asl.tif", np.full((2, 2), 123.0, dtype=np.float32), transform, srs)
            self._simulation_cells(root / "cells" / "simulation_cells.gpkg", (7, 7, 7, 8))
            crop_source = root / "selected_crop_sources"; crop_source.mkdir(parents=True)
            (crop_source / "maize.tab").write_text("example\n")
            (crop_source / "wheat.tab").write_text("unused\n")
            crops = [
                CropDefinition("maize", "Maize", "maize.tab"),
                CropDefinition("wheat", "Wheat", "wheat.tab"),
            ]
            landuses = [
                LandUseDefinition(7, "Maize", "maize"),
                LandUseDefinition(2, "Wheat", "wheat"),
                LandUseDefinition(8, "Excluded"),
            ]
            write_configuration(
                root / "cells" / "landuse_configuration.json", crops, landuses,
                [LandUseAllocation("x", 7, 100.0)],
            )
            self._hydraulics(root / "soil" / "soil_hydraulics.gpkg")
            self._weather(root / "weather" / "weather_daily_points.gpkg")
            destination = root / "exports" / "idragra_v2"
            destination.mkdir(parents=True)
            (destination / "stale.txt").write_text("replace me\n")
            status_messages = []
            result = export_v2_workspace(
                root, destination, crop_parameter_folder=crop_source,
                overwrite=True, on_status=status_messages.append,
            )
            self.assertEqual(result.station_count, 1)
            self.assertEqual(result.active_landuses, (7,))
            self.assertFalse((destination / "stale.txt").exists())
            expected = (
                "domain.asc", "soiluse.asc", "Ksat_I.asc", "Ksat_II.asc",
                "N_I.asc", "N_II.asc", "ThetaI_FC.asc", "ThetaII_r.asc",
                "hydr_cond.asc", "hydr_group.asc", "meteo_1.asc", "meteo_2.asc",
                "CapRisePar_a3.asc", "CapRisePar_a4.asc", "CapRisePar_b1.asc",
                "CapRisePar_b2.asc", "CapRisePar_b3.asc", "CapRisePar_b4.asc",
            )
            self.assertTrue(all((destination / "geodata" / name).is_file() for name in expected))
            data = self._ascii_values(destination / "geodata" / "Ksat_I.asc")
            self.assertAlmostEqual(data[0][0], 1.0)  # 10 mm/h canonical -> 1 cm/h v2
            domain = self._ascii_values(destination / "geodata" / "domain.asc")
            self.assertEqual(domain[1][1], -9999.0)
            soiluse = self._ascii_values(destination / "geodata" / "soiluse.asc")
            self.assertEqual(soiluse[0][0], 1.0)
            domain_rows = (destination / "geodata" / "domain.asc").read_text().splitlines()[6:]
            self.assertTrue(all("." not in value for row in domain_rows for value in row.split()))
            weights1 = self._ascii_values(destination / "geodata" / "meteo_1.asc")
            weights2 = self._ascii_values(destination / "geodata" / "meteo_2.asc")
            self.assertEqual(weights1[0][0], 1.5)
            self.assertEqual(weights2[0][0], 1.5)
            station = (destination / "meteodata" / "station_001.dat").read_text()
            self.assertIn("T_max   T_min   P_tot   U_max", station)
            soil_uses = (destination / "landuses" / "soil_uses.txt").read_text()
            self.assertIn("1\tmaize.tab\t*\t# Maize", soil_uses)
            self.assertNotIn("wheat.tab", soil_uses)
            self.assertTrue((destination / "landuses" / "crop_parameters" / "maize.tab").is_file())
            self.assertFalse((destination / "landuses" / "crop_parameters" / "wheat.tab").exists())
            self.assertFalse((destination / "geodata" / "rice_soilparam.txt").exists())
            capillary = self._ascii_values(destination / "geodata" / "CapRisePar_a3.asc")
            self.assertEqual(capillary[0][0], -1.3)
            parameters = (destination / "idragra_parameters.txt").read_text()
            self.assertIn("Mode = 0", parameters)
            self.assertIn("SoilUseVarFlag = F", parameters)
            self.assertIn("SoilUsesNum = 1", parameters)
            self.assertIn("SimulatedSoilUses = 1", parameters)
            provenance = json.loads((destination / "export_provenance.json").read_text())
            self.assertEqual(provenance["landuse_id_mapping"], {"7": 1})
            self.assertEqual(provenance["aggregation"]["ksat_export_unit"], "cm/h (canonical mm/h divided by 10)")
            self.assertEqual(provenance["capillary_rise"]["coefficients"]["102"]["a4"], 4.6)
            for relative in (
                "geodata/domain.asc", "geodata/CapRisePar_a3.asc",
                "meteodata/station_001.dat", "landuses/soil_uses.txt",
                "landuses/crop_parameters/maize.tab", "export_provenance.json",
            ):
                self.assertIn(f"Writing {relative}...", status_messages)

    @staticmethod
    def _raster(path, values, transform, srs):
        dtype = gdal.GDT_Int32 if values.dtype.kind in "iu" else gdal.GDT_Float32
        dataset = gdal.GetDriverByName("GTiff").Create(str(path), values.shape[1], values.shape[0], 1, dtype)
        dataset.SetGeoTransform(transform); dataset.SetProjection(srs.ExportToWkt())
        band = dataset.GetRasterBand(1); band.WriteArray(values); band.SetNoDataValue(-9999)
        band = dataset = None

    @staticmethod
    def _simulation_cells(path, landuse_ids):
        database = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
        layer = database.CreateLayer("simulation_cells", None, ogr.wkbNone)
        layer.CreateField(ogr.FieldDefn("landuse_id", ogr.OFTInteger))
        definition = layer.GetLayerDefn()
        for landuse_id in landuse_ids:
            feature = ogr.Feature(definition)
            feature.SetField("landuse_id", landuse_id)
            layer.CreateFeature(feature)
        layer = database = None

    @staticmethod
    def _hydraulics(path):
        database = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
        metadata = database.CreateLayer("soil_hydraulic_metadata", None, ogr.wkbNone)
        metadata.CreateField(ogr.FieldDefn("method", ogr.OFTString))
        feature = ogr.Feature(metadata.GetLayerDefn()); feature.SetField("method", "test"); metadata.CreateFeature(feature)
        layer = database.CreateLayer("soil_hydraulic_layers", None, ogr.wkbNone)
        for name, kind in (
            ("profile_id", ogr.OFTInteger), ("top_cm", ogr.OFTReal), ("bottom_cm", ogr.OFTReal),
            ("ksat_mm_h", ogr.OFTReal), ("theta_fc", ogr.OFTReal), ("theta_wp", ogr.OFTReal),
            ("theta_res", ogr.OFTReal), ("theta_sat", ogr.OFTReal),
            ("sand_pct", ogr.OFTReal), ("silt_pct", ogr.OFTReal),
            ("clay_pct", ogr.OFTReal),
        ): layer.CreateField(ogr.FieldDefn(name, kind))
        for top, bottom in ((0, 5), (5, 15), (15, 30), (30, 60), (60, 100), (100, 200)):
            feature = ogr.Feature(layer.GetLayerDefn())
            for name, value in (("profile_id", 1), ("top_cm", top), ("bottom_cm", bottom), ("ksat_mm_h", 10), ("theta_fc", .30), ("theta_wp", .15), ("theta_res", .07), ("theta_sat", .48), ("sand_pct", 40), ("silt_pct", 40), ("clay_pct", 20)):
                feature.SetField(name, value)
            layer.CreateFeature(feature)
        database = None

    @staticmethod
    def _weather(path):
        database = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
        srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
        layer = database.CreateLayer("weather_daily_points", srs, ogr.wkbPoint)
        layer.CreateField(ogr.FieldDefn("date", ogr.OFTString)); layer.CreateField(ogr.FieldDefn("location_id", ogr.OFTString))
        fields = ("tmax_c", "tmin_c", "rhmax_pct", "rhmin_pct", "wind2m_m_s", "solar_rad_mj_m2_day", "precip_mm")
        for name in fields: layer.CreateField(ogr.FieldDefn(name, ogr.OFTReal))
        start = date(2021, 1, 1)
        for offset in range(2):
            feature = ogr.Feature(layer.GetLayerDefn()); feature.SetField("date", str(start + timedelta(days=offset))); feature.SetField("location_id", "west")
            for name, value in zip(fields, (20, 10, 90, 40, 2, 15, 3)): feature.SetField(name, value)
            geometry = ogr.Geometry(ogr.wkbPoint); geometry.AddPoint_2D(9.0, 45.15); feature.SetGeometry(geometry); layer.CreateFeature(feature)
        database = None

    @staticmethod
    def _ascii_values(path):
        lines = path.read_text().splitlines()[6:]
        return [[float(value) for value in line.split()] for line in lines]


if __name__ == "__main__":
    unittest.main()
