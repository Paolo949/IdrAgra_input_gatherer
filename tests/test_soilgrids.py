import io
import json
import tempfile
import unittest
from pathlib import Path

from IdrAgraGather.core.models import BoundingBox
from IdrAgraGather.core.providers.soilgrids import (
    DEPTHS,
    PROPERTIES,
    SOILGRIDS_CRS,
    ensure_raster_crs,
    fetch,
    plan_jobs,
)


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
            properties=("clay",),
        )
        self.assertEqual(len(jobs), len(DEPTHS))
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
                temporary, bbox, properties=("sand",), opener=opener
            )
            self.assertEqual(len(outputs), len(DEPTHS))
            self.assertTrue(all(path.suffix == ".tif" for path in outputs))
            manifest = json.loads((Path(temporary) / "manifest.json").read_text())
            self.assertEqual(len(manifest["assets"]), len(DEPTHS))
            self.assertEqual(manifest["assets"][0]["provider"], "isric-soilgrids-wcs")

            fetch(temporary, bbox, properties=("sand",), opener=opener)
            self.assertEqual(len(calls), len(DEPTHS))

    def test_default_download_has_every_ptf_input_at_every_depth(self):
        jobs = plan_jobs(BoundingBox(9.1, 45.1, 9.4, 45.3))
        self.assertEqual(len(jobs), len(PROPERTIES) * len(DEPTHS))
        self.assertEqual({job.property_name for job in jobs}, set(PROPERTIES))
        self.assertIn("cfvo", PROPERTIES)
        self.assertIn("soc", PROPERTIES)

    def test_missing_wcs_crs_is_assigned_as_soilgrids_homolosine(self):
        try:
            from osgeo import gdal, osr
        except ImportError:
            self.skipTest("GDAL Python bindings are not installed")

        gdal.UseExceptions()
        osr.UseExceptions()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "soilgrids.tif"
            dataset = gdal.GetDriverByName("GTiff").Create(
                str(path), 1, 1, 1, gdal.GDT_Int16
            )
            dataset.SetGeoTransform((0.0, 250.0, 0.0, 0.0, 0.0, -250.0))
            dataset = None

            self.assertTrue(ensure_raster_crs(path))
            self.assertFalse(ensure_raster_crs(path))
            dataset = gdal.Open(str(path))
            spatial_reference = dataset.GetSpatialRef().Clone()
            dataset = None
            expected = osr.SpatialReference()
            expected.SetFromUserInput(SOILGRIDS_CRS)
            self.assertTrue(spatial_reference.IsSame(expected))
            self.assertEqual(SOILGRIDS_CRS, "ESRI:54052")


if __name__ == "__main__":
    unittest.main()
