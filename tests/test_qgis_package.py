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
            self.assertIn("IdrAgraGather/cell_dialog.py", names)
            self.assertIn("IdrAgraGather/soil_ptf_dialog.py", names)
            self.assertIn("IdrAgraGather/v2_export_dialog.py", names)
            self.assertIn("IdrAgraGather/core/cells.py", names)
            self.assertIn("IdrAgraGather/core/soil_ptf.py", names)
            self.assertIn("IdrAgraGather/core/v2_export.py", names)
            self.assertIn(
                "IdrAgraGather/core/_rosetta_data/rose3_mod2_0.npz", names
            )
            self.assertIn(
                "IdrAgraGather/core/_rosetta_data/rose3_mod3_0.npz", names
            )
            self.assertIn(
                "IdrAgraGather/core/_rosetta_data/ROSETTA_LICENSE.txt", names
            )
            self.assertIn("IdrAgraGather/core/landuses.py", names)
            self.assertIn("IdrAgraGather/core/providers/era5_land.py", names)
            self.assertIn("IdrAgraGather/core/providers/eobs.py", names)
            self.assertIn("IdrAgraGather/core/providers/soilgrids.py", names)
            self.assertIn("IdrAgraGather/core/providers/corine.py", names)
            self.assertIn("IdrAgraGather/core/providers/copernicus_dem.py", names)
            self.assertIn("IdrAgraGather/core/corine_normalize.py", names)
            self.assertIn("IdrAgraGather/core/topography_normalize.py", names)
            self.assertIn("IdrAgraGather/core/vector_clip.py", names)
            self.assertIn("IdrAgraGather/core/era5_normalize.py", names)
            self.assertIn("IdrAgraGather/core/eobs_normalize.py", names)
            self.assertIn("IdrAgraGather/core/soilgrids_normalize.py", names)
            self.assertIn("qgisMinimumVersion=3.28", metadata)
            self.assertIn("qgisMaximumVersion=4.99", metadata)
            self.assertIn("version=0.16.1", metadata)
            self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))

    def test_qgis4_removed_enum_aliases_are_not_used(self):
        plugin_root = Path(__file__).resolve().parents[1] / "src" / "IdrAgraGather"
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
        plugin_root = Path(__file__).resolve().parents[1] / "src" / "IdrAgraGather"
        dialog = (plugin_root / "dialog.py").read_text(encoding="utf-8")
        plugin = (plugin_root / "plugin.py").read_text(encoding="utf-8")
        cell_dialog = (plugin_root / "cell_dialog.py").read_text(encoding="utf-8")
        ptf_dialog = (plugin_root / "soil_ptf_dialog.py").read_text(encoding="utf-8")
        export_dialog = (plugin_root / "v2_export_dialog.py").read_text(encoding="utf-8")
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
            "topography-acquire",
            "topography-transform",
            "topography-both",
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
        self.assertIn("QgsRendererCategory", plugin)
        self.assertIn("_categories_for_values", plugin)
        self.assertIn("QColor.fromHsv", plugin)
        self.assertIn("_georeference_netcdf_raster", plugin)
        self.assertIn('QgsCoordinateReferenceSystem("EPSG:4326")', plugin)
        self.assertIn('preview_dir = Path(path).parent / ".qgis_previews"', plugin)
        self.assertIn("outputBounds=list(bounds)", plugin)
        self.assertIn('"Replace normalized weather data?"', plugin)
        self.assertIn("_remove_project_layers_for_path", plugin)
        self.assertIn("root.insertGroup(0, LAYER_GROUP)", plugin)
        self.assertIn("root.insertChildNode(0, group)", plugin)
        self.assertIn("group.insertGroup(0, subgroup_name)", plugin)
        self.assertIn("group.insertChildNode(0, target_group)", plugin)
        self.assertIn("target_group.insertLayer(0, layer)", plugin)
        self.assertIn("find_staged_files", plugin)
        self.assertIn("common_date_coverage", plugin)
        self.assertIn("QStackedWidget", dialog)
        self.assertIn("QTabWidget", dialog)
        self.assertIn('"Weather"', dialog)
        self.assertIn('"Soil"', dialog)
        self.assertIn('"Land use"', dialog)
        self.assertIn('"Topography"', dialog)
        self.assertLess(dialog.index("_build_output_group()"), dialog.index("QTabWidget()"))
        self.assertLess(dialog.index("_build_aoi_group()"), dialog.index("QTabWidget()"))
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
        self.assertIn("setCurrentIndex(self.weather_source_selector.findData(WEATHER_SOURCE_EOBS))", dialog)
        self.assertIn("Copernicus DEM", dialog)
        self.assertIn('"normalize_topography"', dialog)
        self.assertIn("normalize_dem_files", plugin)
        self.assertIn("Build IdrAgra simulation cells", plugin)
        self.assertIn("build_simulation_cells", plugin)
        self.assertIn("Crop rotations", cell_dialog)
        self.assertIn("Class allocation", cell_dialog)
        self.assertIn("Import soil_uses.txt", cell_dialog)
        self.assertIn("Only squares fully inside the AOI", cell_dialog)
        self.assertIn("All full squares intersecting the AOI", cell_dialog)
        self.assertIn('"grid_boundary_policy"', cell_dialog)
        self.assertIn("Derive soil hydraulic properties", plugin)
        self.assertIn("Rosetta 3", ptf_dialog)
        self.assertIn("Required inputs", ptf_dialog)
        self.assertIn("Generated outputs", ptf_dialog)
        self.assertIn("Available but unused", ptf_dialog)
        self.assertIn("Export IdrAgra v2 inputs", plugin)
        self.assertIn("static, rain-fed", export_dialog)
        self.assertNotIn("Not generated: phenology series", export_dialog)
        self.assertIn('on_status(f"Writing {relative}...")', (
            plugin_root / "core" / "v2_export.py"
        ).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
