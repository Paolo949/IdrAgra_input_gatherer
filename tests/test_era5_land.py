from datetime import date
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import sys
import types

from idragather.models import BoundingBox, DateWindow
from idragather.providers.era5_land import DATASET, fetch, plan_jobs, write_plan


class FakeClient:
    def __init__(self):
        self.calls = []

    def retrieve(self, dataset, request, target):
        self.calls.append((dataset, request, target))
        with open(target, "wb") as stream:
            stream.write(b"netcdf placeholder")


class Era5LandTests(unittest.TestCase):
    def test_plan_splits_partial_window_by_month(self):
        jobs = plan_jobs(
            BoundingBox(8.5, 44.7, 10.2, 46.2),
            DateWindow(date(2024, 1, 30), date(2024, 2, 2)),
        )
        self.assertEqual([job.target_name for job in jobs], ["2024-01.nc", "2024-02.nc"])
        self.assertEqual(jobs[0].request["day"], ["30", "31"])
        self.assertEqual(jobs[1].request["day"], ["01", "02"])
        self.assertEqual(jobs[0].request["area"], [46.2, 8.5, 44.7, 10.2])

    def test_small_aoi_is_snapped_outward_to_era5_land_grid(self):
        jobs = plan_jobs(
            BoundingBox(
                9.376581165845161,
                46.143438115229365,
                9.437847434940448,
                46.17592818540497,
            ),
            DateWindow(date(2025, 1, 1), date(2025, 1, 1)),
        )
        self.assertEqual(jobs[0].request["area"], [46.2, 9.3, 46.1, 9.5])

    def test_grid_aligned_aoi_is_not_expanded(self):
        jobs = plan_jobs(
            BoundingBox(9.3, 46.1, 9.5, 46.2),
            DateWindow(date(2025, 1, 1), date(2025, 1, 1)),
        )
        self.assertEqual(jobs[0].request["area"], [46.2, 9.3, 46.1, 9.5])

    def test_fetch_records_manifest_and_resumes(self):
        bbox = BoundingBox(8.5, 44.7, 10.2, 46.2)
        window = DateWindow(date(2024, 1, 30), date(2024, 2, 2))
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outputs = fetch(root, bbox, window, client=client)
            self.assertEqual(len(outputs), 2)
            self.assertEqual([call[0] for call in client.calls], [DATASET, DATASET])

            fetch(root, bbox, window, client=client)
            self.assertEqual(len(client.calls), 2)
            manifest = (root / "manifest.json").read_text(encoding="utf-8")
            self.assertIn('"schema_version": 1', manifest)
            self.assertIn('"provider": "copernicus-cds"', manifest)

    def test_default_client_disables_console_output_and_reports_status(self):
        created = {}

        class Client(FakeClient):
            def __init__(self, **kwargs):
                super().__init__()
                created.update(kwargs)

            def retrieve(self, dataset, request, target):
                created["info_callback"]("Request is %s", "running")
                super().retrieve(dataset, request, target)

        messages = []
        fake_cdsapi = types.SimpleNamespace(Client=Client)
        bbox = BoundingBox(9.3, 46.1, 9.5, 46.2)
        window = DateWindow(date(2025, 1, 1), date(2025, 1, 1))
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(sys.modules, {"cdsapi": fake_cdsapi}):
                fetch(temporary, bbox, window, on_status=messages.append)

        self.assertTrue(created["quiet"])
        self.assertFalse(created["progress"])
        self.assertIsNot(created["debug_callback"], created["info_callback"])
        self.assertIn("Request is running", messages)

    def test_plan_can_be_written_without_cds_credentials(self):
        bbox = BoundingBox(8.5, 44.7, 10.2, 46.2)
        window = DateWindow(date(2024, 1, 30), date(2024, 2, 2))
        with tempfile.TemporaryDirectory() as temporary:
            output = write_plan(temporary, bbox, window)
            contents = output.read_text(encoding="utf-8")
            self.assertIn('"dataset": "reanalysis-era5-land"', contents)
            self.assertIn('"target": "2024-02.nc"', contents)


if __name__ == "__main__":
    unittest.main()
