import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from tools.build_qgis_plugin import build


class QgisPackageTests(unittest.TestCase):
    def test_plugin_zip_contains_metadata_entrypoint_and_core(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = build(Path(temporary) / "plugin.zip")
            with ZipFile(output) as archive:
                names = set(archive.namelist())
                metadata = archive.read("IdrAgraGather/metadata.txt").decode("utf-8")
            roots = {name.split("/", 1)[0] for name in names}
            self.assertEqual(roots, {"IdrAgraGather"})
            self.assertIn("IdrAgraGather/__init__.py", names)
            self.assertIn("IdrAgraGather/plugin.py", names)
            self.assertIn("IdrAgraGather/core/providers/era5_land.py", names)
            self.assertIn("IdrAgraGather/core/providers/soilgrids.py", names)
            self.assertIn("IdrAgraGather/core/era5_normalize.py", names)
            self.assertIn("qgisMinimumVersion=3.28", metadata)
            self.assertIn("qgisMaximumVersion=4.99", metadata)
            self.assertIn("version=0.9.0", metadata)
            self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))

    def test_qgis4_removed_enum_aliases_are_not_used(self):
        plugin_root = Path(__file__).resolve().parents[1] / "qgis_plugin" / "IdrAgraGather"
        source = "\n".join(
            path.read_text(encoding="utf-8") for path in plugin_root.glob("*.py")
        )
        for removed_alias in (
            "Qt.CrossCursor",
            "Qt.Key_Escape",
            "Qt.WindowMinMaxButtonsHint",
            "QDialogButtonBox.Close",
            "QDialogButtonBox.ActionRole",
            "QDialogButtonBox.AcceptRole",
            "Qgis.Info",
            "Qgis.Success",
        ):
            self.assertNotIn(removed_alias, source)

    def test_workspace_has_scoped_actions_and_collapsed_raw_groups(self):
        plugin_root = Path(__file__).resolve().parents[1] / "qgis_plugin" / "IdrAgraGather"
        dialog = (plugin_root / "dialog.py").read_text(encoding="utf-8")
        plugin = (plugin_root / "plugin.py").read_text(encoding="utf-8")
        for action in ("weather-acquire", "weather-transform", "weather-both", "soil-acquire"):
            self.assertIn(action, dialog)
        self.assertIn('return f"{parts[index + 1]}_raw", True', plugin)
        self.assertIn("target_group.setExpanded(not is_raw)", plugin)
        self.assertIn("QStackedWidget", dialog)
        self.assertIn("Add outputs from completed actions to QGIS", dialog)
        self.assertIn("Refresh detected files", dialog)
        self.assertIn("ISRIC SoilGrids", dialog)
        self.assertIn('self.weather_combo.addItem("E-OBS", "eobs")', dialog)


if __name__ == "__main__":
    unittest.main()
