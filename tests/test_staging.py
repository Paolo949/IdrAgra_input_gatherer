import json
import tempfile
import unittest
from pathlib import Path

from idragather.manifest import Manifest
from idragather.models import BoundingBox
from idragather.staging import StagingArea, find_staged_files


class StagingTests(unittest.TestCase):
    def test_find_staged_files_filters_manifest_assets_and_checks_their_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            january = root / "raw" / "weather" / "era5_land" / "2024-01.nc"
            february = root / "raw" / "weather" / "era5_land" / "2024-02.nc"
            plan = january.parent / "era5_plan.json"
            eobs = root / "raw" / "weather" / "eobs" / "temperature.nc"
            january.parent.mkdir(parents=True)
            eobs.parent.mkdir(parents=True)
            for path in (february, january, plan):
                path.touch()
            eobs.touch()

            manifest = Manifest(root)
            for path in (february, january, plan):
                manifest.add_asset(
                    path,
                    category="weather",
                    provider="copernicus-cds",
                )
            manifest.add_asset(
                eobs,
                category="weather",
                provider="eobs-knmi",
            )

            paths = find_staged_files(
                root,
                provider="copernicus-cds",
                suffix=".nc",
            )
            self.assertEqual(paths, (january, february))

            january.unlink()
            with self.assertRaisesRegex(ValueError, "recorded.*missing"):
                find_staged_files(
                    root,
                    provider="copernicus-cds",
                    suffix=".nc",
                )

    def test_stage_local_copies_and_records_checksum(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "soil.tif"
            source.write_bytes(b"soil")
            output = root / "project"

            staged = StagingArea(output).stage_local(source, category="soil")
            self.assertEqual(staged.read_bytes(), b"soil")
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["assets"][0]["category"], "soil")
            self.assertEqual(len(manifest["assets"][0]["sha256"]), 64)

    def test_stage_local_does_not_silently_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first" / "weather.csv"
            second = root / "second" / "weather.csv"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            area = StagingArea(root / "project")
            area.stage_local(first, category="weather")
            with self.assertRaises(FileExistsError):
                area.stage_local(second, category="weather")

    def test_write_aoi_creates_loadable_geojson_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = StagingArea(temporary).write_aoi(BoundingBox(8.5, 44.7, 10.2, 46.2))
            geojson = json.loads(output.read_text(encoding="utf-8"))
            ring = geojson["features"][0]["geometry"]["coordinates"][0]
            self.assertEqual(ring[0], ring[-1])
            self.assertEqual(len(ring), 5)


if __name__ == "__main__":
    unittest.main()
