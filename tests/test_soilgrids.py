import io
import json
import tempfile
import unittest
from pathlib import Path

from idragather.models import BoundingBox
from idragather.providers.soilgrids import DEPTHS, fetch, plan_jobs


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"II*\x00fake geotiff", content_type="image/tiff"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class SoilGridsTests(unittest.TestCase):
    def test_plan_uses_mean_wcs_coverages_and_geographic_subset(self):
        jobs = plan_jobs(
            BoundingBox(9.1, 45.1, 9.4, 45.3),
            properties=("clay",), depths=("0-5cm",),
        )
        self.assertEqual(jobs[0].coverage_id, "clay_0-5cm_mean")
        self.assertIn("REQUEST=GetCoverage", jobs[0].url)
        self.assertIn("SUBSETTINGCRS=", jobs[0].url)
        self.assertIn("X%289.1%2C9.4%29", jobs[0].url)

    def test_fetch_stages_tiffs_records_manifest_and_resumes(self):
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, timeout))
            return FakeResponse()

        with tempfile.TemporaryDirectory() as temporary:
            bbox = BoundingBox(9.1, 45.1, 9.4, 45.3)
            outputs = fetch(
                temporary, bbox, properties=("sand",), depths=DEPTHS[:2], opener=opener
            )
            self.assertEqual(len(outputs), 2)
            self.assertTrue(all(path.suffix == ".tif" for path in outputs))
            manifest = json.loads((Path(temporary) / "manifest.json").read_text())
            self.assertEqual(len(manifest["assets"]), 2)
            self.assertEqual(manifest["assets"][0]["provider"], "isric-soilgrids-wcs")

            fetch(temporary, bbox, properties=("sand",), depths=DEPTHS[:2], opener=opener)
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
