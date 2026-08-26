import json
import tempfile
import unittest
from pathlib import Path

from IdrAgraGather.core.manifest import Manifest
from IdrAgraGather.core.models import BoundingBox
from IdrAgraGather.core.staging import StagingArea, find_staged_files


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
        geometry = {
            "type": "Polygon",
            "coordinates": [[[8.5, 44.7], [10.2, 44.7], [10.2, 46.2], [8.5, 46.2], [8.5, 44.7]]],
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = StagingArea(temporary).write_aoi(BoundingBox(8.5, 44.7, 10.2, 46.2), geometry=geometry)
            geojson = json.loads(output.read_text(encoding="utf-8"))
            ring = geojson["features"][0]["geometry"]["coordinates"][0]
            self.assertEqual(ring[0], ring[-1])
            self.assertEqual(len(ring), 5)

    def test_write_aoi_preserves_exact_polygon_separately_from_its_envelope(self):
        geometry = {
            "type": "Polygon",
            "coordinates": [[[9.0, 45.2], [9.8, 45.0], [10.0, 45.8], [9.2, 46.0], [9.0, 45.2]]],
        }
        bbox = BoundingBox(9.0, 45.0, 10.0, 46.0)
        with tempfile.TemporaryDirectory() as temporary:
            output = StagingArea(temporary).write_aoi(bbox, geometry=geometry)
            geojson = json.loads(output.read_text(encoding="utf-8"))
            manifest = Manifest(Path(temporary)).read()
            self.assertEqual(geojson["features"][0]["geometry"], geometry)
            self.assertEqual(geojson["features"][0]["properties"]["geometry_source"], "exact-canvas-polygon")
            self.assertEqual(manifest["aoi"], bbox.as_dict())
            self.assertEqual(manifest["assets"][0]["dataset"], "study-area-polygon")

    def test_write_aoi_rejects_an_open_polygon_ring(self):
        geometry = {
            "type": "Polygon",
            "coordinates": [[[9.0, 45.0], [10.0, 45.0], [10.0, 46.0], [9.0, 46.0]]],
        }
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "exterior ring must be closed"):
                StagingArea(temporary).write_aoi(BoundingBox(9.0, 45.0, 10.0, 46.0), geometry=geometry)


if __name__ == "__main__":
    unittest.main()
