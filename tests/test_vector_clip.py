import json
import tempfile
import unittest
from pathlib import Path

from IdrAgraGather.core.models import BoundingBox
from IdrAgraGather.core.staging import StagingArea
from IdrAgraGather.core.vector_clip import aoi_geometry


class VectorClipTests(unittest.TestCase):
    def test_saved_polygon_is_used_instead_of_reconstructed_envelope(self):
        try:
            from osgeo import ogr, osr
        except ImportError:
            self.skipTest("GDAL/OGR is provided by QGIS")

        bbox = BoundingBox(9.0, 45.0, 10.0, 46.0)
        geometry = {
            "type": "Polygon",
            "coordinates": [[[9.0, 45.5], [9.5, 45.0], [10.0, 45.5], [9.5, 46.0], [9.0, 45.5]]],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = StagingArea(temporary).write_aoi(bbox, geometry=geometry)
            exact = aoi_geometry(path, None, ogr, osr)
            self.assertAlmostEqual(exact.GetArea(), 0.5)

    def test_missing_saved_polygon_is_rejected(self):
        try:
            from osgeo import ogr, osr
        except ImportError:
            self.skipTest("GDAL/OGR is provided by QGIS")

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "no saved AOI polygon"):
                aoi_geometry(Path(temporary) / "aoi.geojson", None, ogr, osr)

    def test_legacy_unmarked_aoi_is_rejected(self):
        try:
            from osgeo import ogr, osr
        except ImportError:
            self.skipTest("GDAL/OGR is provided by QGIS")

        collection = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[9.0, 45.0], [10.0, 45.0], [10.0, 46.0], [9.0, 46.0], [9.0, 45.0]]],
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "aoi.geojson"
            path.write_text(json.dumps(collection), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "predates exact polygon support"):
                aoi_geometry(path, None, ogr, osr)


if __name__ == "__main__":
    unittest.main()
