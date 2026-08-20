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
            self.assertIn("IdrAgraGather/core/providers/eobs.py", names)
            self.assertIn("IdrAgraGather/core/providers/soilgrids.py", names)
            self.assertIn("IdrAgraGather/core/providers/corine.py", names)
            self.assertIn("IdrAgraGather/core/corine_normalize.py", names)
            self.assertIn("IdrAgraGather/core/vector_clip.py", names)
            self.assertIn("IdrAgraGather/core/era5_normalize.py", names)
            self.assertIn("IdrAgraGather/core/eobs_normalize.py", names)
            self.assertIn("IdrAgraGather/core/soilgrids_normalize.py", names)
            self.assertIn("qgisMinimumVersion=3.28", metadata)
            self.assertIn("qgisMaximumVersion=4.99", metadata)
            self.assertIn("version=0.10.0", metadata)
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
        self.assertNotIn("QkeyEvent", source)
        self.assertIn("QKeyEvent", source)

    def test_workspace_has_scoped_actions_and_collapsed_raw_groups(self):
        plugin_root = Path(__file__).resolve().parents[1] / "qgis_plugin" / "IdrAgraGather"
        dialog = (plugin_root / "dialog.py").read_text(encoding="utf-8")
        plugin = (plugin_root / "plugin.py").read_text(encoding="utf-8")
        for action in (
            "weather-acquire",
            "weather-transform",
            "weather-both",
            "soil-acquire",
            "soil-transform",
            "soil-both",
            "landuse-acquire",
            "landuse-transform",
            "landuse-both",
            "topography-stage",
        ):
            self.assertIn(action, dialog)
        self.assertIn('return f"{parts[index + 1]}_raw", True', plugin)
        self.assertIn("target_group.setExpanded(not is_raw)", plugin)
        self.assertIn("target_group.setItemVisibilityChecked(True)", plugin)
        self.assertIn("isinstance(layer, QgsRasterLayer)", plugin)
        self.assertIn("QgsSingleBandPseudoColorRenderer", plugin)
        self.assertIn("QgsCategorizedSymbolRenderer", plugin)
        self.assertIn('_style_normalized_soil_layer(layer, path)', plugin)
        self.assertIn('QgsCategorizedSymbolRenderer("profile_id", categories)', plugin)
        self.assertIn("QgsCategorizedSymbolRenderer.createCategories", plugin)
        self.assertIn("QgsRandomColorRamp", plugin)
        self.assertIn("_georeference_netcdf_raster", plugin)
        self.assertIn('QgsCoordinateReferenceSystem("EPSG:4326")', plugin)
        self.assertIn('preview_dir = Path(path).parent / ".qgis_previews"', plugin)
        self.assertIn("outputBounds=list(bounds)", plugin)
        self.assertIn('"Replace normalized weather data?"', plugin)
        self.assertIn("_remove_project_layers_for_path", plugin)
        self.assertIn("root.insertGroup(0, LAYER_GROUP)", plugin)
        self.assertIn("root.insertChildNode(0, group)", plugin)
        self.assertIn("find_staged_eobs_files", plugin)
        self.assertIn("common_date_coverage", plugin)
        self.assertIn("QStackedWidget", dialog)
        self.assertIn("Add outputs from completed actions to QGIS", dialog)
        self.assertIn("Refresh detected files", dialog)
        self.assertIn("ISRIC SoilGrids", dialog)
        self.assertNotIn("Topsoil only", dialog)
        self.assertIn("all six SoilGrids horizons", dialog)
        self.assertIn("Maximum soil classes", dialog)
        self.assertIn('"soil_max_classes"', dialog)
        self.assertIn('max_classes=request.get("soil_max_classes", 20)', plugin)
        self.assertIn("normalize_soilgrids_files", plugin)
        self.assertIn("ensure_soilgrids_raster_crs(path)", plugin)
        self.assertIn("CORINE Land Cover 2018", dialog)
        self.assertIn("normalize_corine_file", plugin)
        self.assertIn('QgsCategorizedSymbolRenderer("landuse", categories)', plugin)
        self.assertIn('{".gpkg", ".sqlite", ".shp"}', plugin)
        self.assertIn("self._style_normalized_landuse_layer(layer, path)", plugin)
        self.assertIn('"normalize_landuse"', dialog)
        self.assertIn("WEATHER_SOURCE_EOBS", dialog)


if __name__ == "__main__":
    unittest.main()
