import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from IdrAgraGather.core.models import BoundingBox
from IdrAgraGather.core.providers.corine import build_query_url, fetch


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class CorineTests(unittest.TestCase):
    def test_query_uses_the_aoi_and_requests_geojson(self):
        url = build_query_url(BoundingBox(9.1, 45.1, 9.4, 45.3))
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["geometry"], ["9.1,45.1,9.4,45.3"])
        self.assertEqual(query["inSR"], ["4326"])
        self.assertEqual(query["outSR"], ["4326"])
        self.assertEqual(query["f"], ["geojson"])
        self.assertIn("Code_18", query["outFields"][0])

    def test_fetch_writes_geojson_and_manifest(self):
        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"OBJECTID": 1, "Code_18": "213"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[9.1, 45.1], [9.2, 45.1], [9.1, 45.1]]],
                    },
                }
            ],
        }

        def opener(_request, timeout):
            self.assertEqual(timeout, 180)
            return FakeResponse(json.dumps(payload).encode("utf-8"))

        with tempfile.TemporaryDirectory() as temporary:
            outputs = fetch(
                temporary,
                BoundingBox(9.1, 45.1, 9.4, 45.3),
                opener=opener,
            )
            output = outputs[0]
            saved = json.loads(output.read_text(encoding="utf-8"))
            manifest = json.loads(
                (Path(temporary) / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["features"][0]["properties"]["Code_18"], "213")
            self.assertEqual(manifest["assets"][0]["provider"], "eea-corine-arcgis-rest")


if __name__ == "__main__":
    unittest.main()
