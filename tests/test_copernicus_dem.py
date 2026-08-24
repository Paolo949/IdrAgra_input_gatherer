import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from IdrAgraGather.core.models import BoundingBox
from IdrAgraGather.core.providers.copernicus_dem import (
    MAX_TILE_PIXELS,
    fetch,
    fetch_access_token,
    find_tiles,
    plan_jobs,
)


class FakeResponse(io.BytesIO):
    def __init__(self, payload, content_type="application/json"):
        super().__init__(payload)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class CopernicusDemTests(unittest.TestCase):
    def test_plan_uses_native_resolution_and_tiles_large_aoi(self):
        bbox = BoundingBox(9.0, 45.0, 9.8, 45.2)
        jobs = plan_jobs(bbox, instance="COPERNICUS_30")
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].width, MAX_TILE_PIXELS)
        self.assertLessEqual(jobs[1].width, MAX_TILE_PIXELS)
        self.assertTrue(all(job.height <= MAX_TILE_PIXELS for job in jobs))
        self.assertAlmostEqual(jobs[0].bbox.west, bbox.west)
        self.assertAlmostEqual(jobs[-1].bbox.east, bbox.east)

        payload = jobs[0].request_payload()
        self.assertEqual(payload["input"]["data"][0]["type"], "dem")
        self.assertEqual(
            payload["input"]["data"][0]["dataFilter"]["demInstance"],
            "COPERNICUS_30",
        )
        self.assertFalse(payload["input"]["data"][0]["processing"]["egm"])
        self.assertIn("FLOAT32", payload["evalscript"])

    def test_token_exchange_uses_client_credentials(self):
        observed = {}

        def opener(request, timeout):
            observed["url"] = request.full_url
            observed["body"] = request.data.decode("utf-8")
            observed["timeout"] = timeout
            return FakeResponse(json.dumps({"access_token": "token"}).encode())

        token = fetch_access_token("client", "secret with spaces", opener=opener)
        self.assertEqual(token, "token")
        self.assertEqual(observed["timeout"], 60)
        self.assertIn("grant_type=client_credentials", observed["body"])
        self.assertIn("client_secret=secret+with+spaces", observed["body"])

    def test_fetch_writes_geotiff_tiles_and_manifest(self):
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            return FakeResponse(b"II*\x00fake-tiff", "image/tiff")

        bbox = BoundingBox(9.1, 45.1, 9.11, 45.11)
        with tempfile.TemporaryDirectory() as temporary:
            outputs = fetch(
                temporary,
                bbox,
                access_token="token",
                opener=opener,
            )
            self.assertEqual(len(outputs), 1)
            self.assertEqual(outputs[0].read_bytes(), b"II*\x00fake-tiff")
            request, timeout = calls[0]
            self.assertEqual(timeout, 300)
            self.assertEqual(request.get_header("Authorization"), "Bearer token")
            payload = json.loads(request.data)
            self.assertEqual(payload["input"]["bounds"]["bbox"], [9.1, 45.1, 9.11, 45.11])

            manifest = json.loads(
                (Path(temporary) / "manifest.json").read_text(encoding="utf-8")
            )
            asset = manifest["assets"][0]
            self.assertEqual(asset["provider"], "copernicus-dem-sentinel-hub")
            self.assertEqual(asset["request"]["vertical_reference"], "EGM2008 orthometric height")
            self.assertEqual(find_tiles(temporary, bbox), tuple(outputs))

    def test_missing_credentials_have_an_actionable_error(self):
        with mock.patch.dict(
            "os.environ", {"SH_CLIENT_ID": "", "SH_CLIENT_SECRET": ""}
        ):
            with self.assertRaisesRegex(ValueError, "OAuth client ID and secret"):
                fetch_access_token()


if __name__ == "__main__":
    unittest.main()
