import json
import tempfile
import unittest
from pathlib import Path

from idragather.staging import StagingArea
from idragather.models import BoundingBox


class StagingTests(unittest.TestCase):
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
