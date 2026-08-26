import json
import tempfile
import unittest
from pathlib import Path

from IdrAgraGather.core.corine_normalize import (
    CORINE_CATEGORIES,
    corine_category,
    normalize_corine_file,
)
from IdrAgraGather.core.models import BoundingBox
from IdrAgraGather.core.staging import StagingArea


class CorineNormalizeTests(unittest.TestCase):
    def test_all_44_level_three_codes_have_readable_categories(self):
        self.assertEqual(len(CORINE_CATEGORIES), 44)
        self.assertEqual(corine_category("213"), "Rice fields")
        self.assertEqual(corine_category(311), "Broad-leaved forest")
        self.assertEqual(corine_category(523.0), "Sea and ocean")
        self.assertTrue(all(label and not label.isdigit() for label in CORINE_CATEGORIES.values()))

    def test_invalid_and_unknown_codes_are_rejected(self):
        for code in (None, True, "", "213.5", "999"):
            with self.subTest(code=code), self.assertRaisesRegex(
                ValueError, "CORINE class code"
            ):
                corine_category(code)

    def test_gdal_pipeline_writes_only_landuse_attributes(self):
        try:
            from osgeo import ogr
        except ImportError:
            self.skipTest("GDAL Python bindings are not installed")

        ogr.UseExceptions()
        collection = {
            "type": "FeatureCollection",
            "name": "clc2018",
            "crs": {
                "type": "name",
                "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
            },
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "OBJECTID": 1,
                        "Code_18": "213",
                        "Remark": "remove me",
                        "Area_Ha": 1.0,
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[9.0, 45.0], [9.1, 45.0], [9.1, 45.1], [9.0, 45.0]]
                        ],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {
                        "OBJECTID": 2,
                        "Code_18": "311",
                        "Remark": "remove me too",
                        "Area_Ha": 2.0,
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[9.2, 45.0], [9.3, 45.0], [9.3, 45.1], [9.2, 45.0]]
                        ],
                    },
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "raw" / "landuse" / "corine" / "clc2018.geojson"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps(collection), encoding="utf-8")
            clip = BoundingBox(9.05, 45.01, 9.25, 45.08)
            StagingArea(temporary).write_aoi(
                clip,
                geometry={
                    "type": "Polygon",
                    "coordinates": [[[9.05, 45.01], [9.25, 45.01], [9.25, 45.08], [9.05, 45.08], [9.05, 45.01]]],
                },
            )
            result = normalize_corine_file(source, temporary, bbox=clip)

            self.assertEqual(result.path, Path(temporary) / "landuse" / "landuse.shp")
            self.assertEqual(result.polygon_count, 2)
            self.assertEqual(result.category_count, 2)
            database = ogr.Open(str(result.path))
            layer = database.GetLayer(0)
            fields = [
                layer.GetLayerDefn().GetFieldDefn(index).GetName()
                for index in range(layer.GetLayerDefn().GetFieldCount())
            ]
            self.assertEqual(fields, ["landuse"])
            self.assertEqual(
                {feature.GetField("landuse") for feature in layer},
                {"Rice fields", "Broad-leaved forest"},
            )
            layer.ResetReading()
            for feature in layer:
                west, east, south, north = feature.GetGeometryRef().GetEnvelope()
                self.assertGreaterEqual(west, clip.west)
                self.assertLessEqual(east, clip.east)
                self.assertGreaterEqual(south, clip.south)
                self.assertLessEqual(north, clip.north)
            database = None

            manifest = json.loads((Path(temporary) / "manifest.json").read_text())
            normalized = next(
                asset for asset in manifest["assets"] if asset["path"] == "landuse/landuse.shp"
            )
            self.assertFalse(normalized["request"]["numeric_code_retained"])
            self.assertEqual(normalized["request"]["output_fields"], ["landuse"])
            self.assertEqual(normalized["request"]["clip_aoi"], clip.as_dict())

if __name__ == "__main__":
    unittest.main()
