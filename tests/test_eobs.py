from datetime import date
from pathlib import Path
import tempfile
import unittest

from IdrAgraGather.core.models import BoundingBox, DateWindow
from IdrAgraGather.core.providers.eobs import VARIABLES, fetch, plan_jobs
from IdrAgraGather.core.staging import find_staged_files


class EobsProviderTests(unittest.TestCase):
    def test_jobs_are_one_file_per_variable_and_intersecting_period(self):
        jobs = plan_jobs(
            BoundingBox(9.3, 46.1, 9.5, 46.2),
            DateWindow(date(2010, 12, 30), date(2011, 1, 2)),
            today=date(2026, 8, 19),
        )
        self.assertEqual(len(jobs), len(VARIABLES) * 2)
        self.assertEqual({job.period for job in jobs}, {"1995-2010", "2011-2025"})
        self.assertTrue(all(job.url.endswith(".nc") for job in jobs))
        self.assertTrue(all(job.target_name.endswith(".nc") for job in jobs))

    def test_running_year_uses_provisional_monthly_update_files(self):
        jobs = plan_jobs(
            BoundingBox(9.3, 46.1, 9.5, 46.2),
            DateWindow(date(2026, 1, 1), date(2026, 7, 31)),
            today=date(2026, 8, 19),
        )
        self.assertTrue(all(job.provisional for job in jobs))
        self.assertTrue(all("/months/ens/" in job.url for job in jobs))

    def test_pre_1980_window_is_rejected_because_wind_is_unavailable(self):
        with self.assertRaisesRegex(ValueError, "wind speed starts in 1980"):
            plan_jobs(
                BoundingBox(9.3, 46.1, 9.5, 46.2),
                DateWindow(date(1979, 1, 1), date(1980, 1, 1)),
            )

    def test_fetch_downloads_and_records_all_assets(self):
        calls = []

        def subsetter(job, target, bbox, window, is_cancelled):
            calls.append((job.url, bbox, window, is_cancelled))
            target.write_bytes(b"netcdf placeholder")

        bbox = BoundingBox(9.3, 46.1, 9.5, 46.2)
        window = DateWindow(date(2025, 1, 1), date(2025, 1, 2))
        with tempfile.TemporaryDirectory() as temporary:
            outputs = fetch(temporary, bbox, window, subsetter=subsetter)
            self.assertEqual(len(outputs), len(VARIABLES))
            self.assertEqual(len(calls), len(VARIABLES))
            self.assertTrue(all(path.is_file() for path in outputs))
            manifest = (Path(temporary) / "manifest.json").read_text(encoding="utf-8")
            self.assertIn('"provider": "eobs-knmi"', manifest)
            self.assertIn('"requested_bbox"', manifest)

    def test_staged_files_are_found_from_the_manifest(self):
        def subsetter(_job, target, _bbox, _window, _is_cancelled):
            target.write_bytes(b"netcdf placeholder")

        bbox = BoundingBox(9.3, 46.1, 9.5, 46.2)
        acquired = DateWindow(date(2026, 1, 1), date(2026, 8, 20))
        with tempfile.TemporaryDirectory() as temporary:
            outputs = fetch(temporary, bbox, acquired, subsetter=subsetter)
            staged = find_staged_files(
                temporary,
                provider="eobs-knmi",
                suffix=".nc",
            )
            self.assertEqual(set(staged), set(outputs))


if __name__ == "__main__":
    unittest.main()
