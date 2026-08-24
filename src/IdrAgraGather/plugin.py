import hashlib
import math
import os
from pathlib import Path
import re

from qgis.PyQt.QtCore import QCoreApplication, QDir, QObject, QSettings, pyqtSignal # pyright: ignore[reportAttributeAccessIssue]
from qgis.PyQt.QtGui import QColor # pyright: ignore[reportAttributeAccessIssue]
from qgis.PyQt.QtWidgets import QAction, QMessageBox # pyright: ignore[reportAttributeAccessIssue]
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsColorRampShader,
    QgsFillSymbol,
    QgsGeometry,
    QgsProject,
    QgsProviderRegistry,
    QgsProviderSublayerDetails,
    QgsRasterLayer,
    QgsRasterShader,
    QgsRandomColorRamp,
    QgsSingleBandPseudoColorRenderer,
    QgsSymbol,
    QgsTask,
    QgsVectorLayer,
)
from .core.corine_normalize import normalize_corine_file
from .core.era5_normalize import normalize_era5_files
from .core.eobs_normalize import common_date_coverage, normalize_eobs_files
from .core.models import BoundingBox, DateWindow
from .core.providers.corine import fetch as fetch_corine
from .core.providers.era5_land import fetch, plan_jobs, write_plan
from .core.providers.eobs import fetch as fetch_eobs
from .core.providers.soilgrids import ensure_raster_crs as ensure_soilgrids_raster_crs
from .core.providers.soilgrids import fetch as fetch_soilgrids
from .core.soilgrids_normalize import normalize_soilgrids_files
from .core.staging import StagingArea, find_staged_files
from .dialog import (
    AcquisitionDialog,
    LANDUSE_SOURCE_CORINE,
    SOIL_SOURCE_SOILGRIDS,
)
from .map_tool import RectangleMapTool


MENU_NAME = "&IdrAgra"
LAYER_GROUP = "IdrAgra gathered inputs"
MESSAGE_LEVEL = getattr(Qgis, "MessageLevel", Qgis)
MESSAGE_BUTTON = getattr(QMessageBox, "StandardButton", QMessageBox)
NORMALIZED_WEATHER_NAME = "weather_daily_points.gpkg"
NORMALIZED_SOIL_NAME = "soil_profiles.gpkg"
NORMALIZED_LANDUSE_NAME = "landuse.shp"


class AcquisitionReporter(QObject):
    """Thread-safe bridge from the acquisition worker to the dialog log."""

    status = pyqtSignal(str)


def _run_acquisition(task, request):
    bbox = BoundingBox(*request["bbox"])
    window = DateWindow.from_iso(request["start"], request["end"])
    staging = StagingArea(request["output"])
    aoi_path = staging.write_aoi(bbox)
    outputs = [aoi_path]
    load_paths = [aoi_path]
    task.setProgress(5)

    source = request["weather_source"]
    if source == "era5-plan":
        outputs.append(write_plan(request["output"], bbox, window))
        task.setProgress(80)
    elif source in {"era5-download", "era5-normalize"}:
        report_status = request.get("status_callback")

        def update_progress(done, total, _path):
            task.setProgress(5 + 80 * done / total)

        def update_status(message):
            if report_status is not None:
                report_status("CDS: " + message)
            lowered = message.lower()
            if "download" in lowered:
                task.setProgress(max(task.progress(), 65))
            elif "running" in lowered:
                task.setProgress(max(task.progress(), 25))
            elif "accepted" in lowered or "queue" in lowered or "submitting" in lowered:
                task.setProgress(max(task.progress(), 10))

        if source == "era5-download":
            weather_outputs = fetch(
                    request["output"],
                    bbox,
                    window,
                    is_cancelled=task.isCanceled,
                    on_progress=update_progress,
                    on_status=update_status,
            )
        else:
            weather_outputs = find_staged_files(
                request["output"],
                provider="copernicus-cds",
                suffix=".nc",
            )
            if report_status is not None:
                report_status(
                    f"Normalize: found {len(weather_outputs)} staged NetCDF file(s)."
                )
        outputs.extend(weather_outputs)
        if request.get("normalize_weather", True) and not task.isCanceled():
            task.setProgress(max(task.progress(), 88))

            def normalization_status(message):
                if report_status is not None:
                    report_status("Normalize: " + message)

            normalized = normalize_era5_files(
                weather_outputs,
                request["output"],
                window,
                timezone_name=request.get("timezone", "Europe/Rome"),
                on_status=normalization_status,
            )
            outputs.append(normalized.path)
            load_paths.append(normalized.path)
            if report_status is not None:
                for warning in normalized.warnings:
                    report_status("WARNING: " + warning)
            task.setProgress(96)
        else:
            load_paths.extend(weather_outputs)
    elif source in {"eobs-download", "eobs-normalize"}:
        report_status = request.get("status_callback")

        def eobs_progress(done, total, _path):
            task.setProgress(5 + 80 * done / total)

        def eobs_status(message):
            if report_status is not None:
                report_status("E-OBS: " + message)

        if source == "eobs-download":
            weather_outputs = fetch_eobs(
                request["output"],
                bbox,
                window,
                is_cancelled=task.isCanceled,
                on_progress=eobs_progress,
                on_status=eobs_status,
            )
        else:
            weather_outputs = find_staged_files(
                request["output"],
                provider="eobs-knmi",
                suffix=".nc",
            )
            eobs_status(f"Found {len(weather_outputs)} staged NetCDF file(s).")

        # Raw-only acquisition has no normalization result in which to report
        # truncated provisional coverage, so surface it explicitly here.
        if not task.isCanceled() and not request.get("normalize_weather", True):
            coverage = common_date_coverage(weather_outputs)
            if coverage.start > window.start or coverage.end < window.end:
                eobs_status(
                    "WARNING: The staged provisional files cover "
                    f"{coverage.start} to {coverage.end}, not the full requested "
                    f"interval {window.start} to {window.end}. Normalization will "
                    "use the common available dates."
                )

        outputs.extend(weather_outputs)
        if request.get("normalize_weather", True) and not task.isCanceled():
            task.setProgress(max(task.progress(), 88))

            def eobs_normalization_status(message):
                if report_status is not None:
                    report_status("Normalize: " + message)

            normalized = normalize_eobs_files(
                weather_outputs,
                request["output"],
                bbox,
                window,
                on_status=eobs_normalization_status,
            )
            outputs.append(normalized.path)
            load_paths.append(normalized.path)
            if report_status is not None:
                for warning in normalized.warnings:
                    report_status("WARNING: " + warning)
            task.setProgress(96)
        else:
            load_paths.extend(weather_outputs)
    if task.isCanceled():
        return {"cancelled": True, "outputs": [str(path) for path in outputs]}

    soil_outputs = []
    if request.get("soil_source") == SOIL_SOURCE_SOILGRIDS:
        report_status = request.get("status_callback")

        def soil_progress(done, total, _path):
            task.setProgress(max(task.progress(), 96 + 3 * done / total))

        def soil_status(message):
            if report_status is not None:
                report_status("SoilGrids: " + message)

        soil_outputs = fetch_soilgrids(
            request["output"], bbox,
            is_cancelled=task.isCanceled, on_progress=soil_progress,
            on_status=soil_status,
        )
        outputs.extend(soil_outputs)
        if not request.get("normalize_soil"):
            load_paths.extend(soil_outputs)

    if task.isCanceled():
        return {"cancelled": True, "outputs": [str(path) for path in outputs]}

    if request.get("normalize_soil") and not task.isCanceled():
        report_status = request.get("status_callback")
        if not soil_outputs:
            soil_outputs = list(
                find_staged_files(
                    request["output"],
                    provider="isric-soilgrids-wcs",
                    suffix=".tif",
                )
            )
        task.setProgress(max(task.progress(), 96))

        def soil_normalization_status(message):
            if report_status is not None:
                report_status("Normalize soil: " + message)

        normalized_soil = normalize_soilgrids_files(
            soil_outputs,
            request["output"],
            bbox=bbox,
            max_classes=request.get("soil_max_classes", 20),
            on_status=soil_normalization_status,
        )
        outputs.append(normalized_soil.path)
        load_paths.append(normalized_soil.path)
        if report_status is not None:
            report_status(
                "Normalize soil: wrote "
                f"{normalized_soil.polygon_count} polygon(s) for "
                f"{normalized_soil.profile_count} class(es), condensed from "
                f"{normalized_soil.exact_profile_count} exact profile(s); filled "
                f"{normalized_soil.filled_nodata_cells} NoData cell(s) to cover the AOI."
            )
        task.setProgress(99)

    landuse_outputs = []
    if request.get("landuse_source") == LANDUSE_SOURCE_CORINE:
        report_status = request.get("status_callback")

        def landuse_progress(done, total, _path):
            task.setProgress(max(task.progress(), 95 + 4 * done / total))

        def landuse_status(message):
            if report_status is not None:
                report_status("CORINE: " + message)

        landuse_outputs = fetch_corine(
            request["output"],
            bbox,
            is_cancelled=task.isCanceled,
            on_progress=landuse_progress,
            on_status=landuse_status,
        )
        outputs.extend(landuse_outputs)
        if not request.get("normalize_landuse"):
            load_paths.extend(landuse_outputs)

    if task.isCanceled():
        return {"cancelled": True, "outputs": [str(path) for path in outputs]}

    if request.get("normalize_landuse"):
        report_status = request.get("status_callback")
        if not landuse_outputs:
            landuse_outputs = list(
                find_staged_files(
                    request["output"],
                    provider="eea-corine-arcgis-rest",
                    suffix=".geojson",
                )
            )

        def landuse_normalization_status(message):
            if report_status is not None:
                report_status("Normalize land use: " + message)

        normalized_landuse = normalize_corine_file(
            landuse_outputs[0],
            request["output"],
            bbox=bbox,
            on_status=landuse_normalization_status,
        )
        outputs.append(normalized_landuse.path)
        load_paths.append(normalized_landuse.path)
        task.setProgress(99)
    
    for category, source_path in request["local_files"].items():
        if source_path:
            staged_path = staging.stage_local(source_path, category=category)
            outputs.append(staged_path)
            load_paths.append(staged_path)
    manifest_path = Path(request["output"]) / "manifest.json"
    if manifest_path.exists():
        outputs.append(manifest_path)
    task.setProgress(100)
    return {
        "cancelled": False,
        "load_results": request["load_results"],
        "outputs": [str(path) for path in outputs],
        "load_paths": [str(path) for path in load_paths],
    }


class IdrAgraGatherPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.action = None
        self.dialog = None
        self.map_tool = RectangleMapTool(self.canvas)
        self.map_tool.rectangleCreated.connect(self._rectangle_created)
        self.map_tool.cancelled.connect(self._drawing_cancelled)
        self.task = None
        self.reporter = None

    def initGui(self):
        self.action = QAction("Gather IdrAgra inputs…", self.iface.mainWindow())
        self.action.setToolTip("Draw an area and gather raw IdrAgra inputs")
        self.action.triggered.connect(self.show_dialog)
        self.iface.addPluginToMenu(MENU_NAME, self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.action is not None:
            self.iface.removePluginMenu(MENU_NAME, self.action)
            self.iface.removeToolBarIcon(self.action)
        if self.canvas.mapTool() is self.map_tool:
            self.iface.actionPan().trigger()
        self.map_tool.clear()
        if self.dialog is not None:
            self.dialog.close()

    def show_dialog(self):
        if self.dialog is None:
            self.dialog = AcquisitionDialog(self.iface.mainWindow())
            self.dialog.drawRequested.connect(self._start_drawing)
            self.dialog.canvasExtentRequested.connect(self._use_canvas_extent)
            self.dialog.runRequested.connect(self._run)
            self.dialog.finished.connect(self._dialog_closed)
            settings = QSettings()
            default_output = QgsProject.instance().homePath() or QDir.homePath()
            self.dialog.set_output_folder(
                settings.value("IdrAgraGather/output", default_output, type=str)
            )
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _start_drawing(self):
        self.dialog.hide()
        self.iface.messageBar().pushMessage(
            "IdrAgra Input Gatherer",
            "Drag a rectangle on the map; press Esc to cancel.",
            level=MESSAGE_LEVEL.Info,
            duration=5,
        )
        self.canvas.setMapTool(self.map_tool)

    def _rectangle_created(self, rectangle):
        try:
            self._set_canvas_rectangle(rectangle)
        finally:
            self.iface.actionPan().trigger()
            self.show_dialog()

    def _drawing_cancelled(self):
        self.iface.actionPan().trigger()
        self.show_dialog()

    def _use_canvas_extent(self):
        self._set_canvas_rectangle(self.canvas.extent())

    def _set_canvas_rectangle(self, rectangle):
        source_crs = self.canvas.mapSettings().destinationCrs()
        target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        geometry = QgsGeometry.fromRect(rectangle)
        if source_crs != target_crs:
            transform = QgsCoordinateTransform(
                source_crs,
                target_crs,
                QgsProject.instance().transformContext(),
            )
            geometry.transform(transform)
        bbox = geometry.boundingBox()
        self.dialog.set_bbox(bbox.xMinimum(), bbox.yMinimum(), bbox.xMaximum(), bbox.yMaximum())
        self.dialog.append_log("Study area selected in EPSG:4326.")

    def _run(self, action):
        if self.task is not None:
            self._show_error("An acquisition task is already running.")
            return
        try:
            request = self.dialog.request(action)
        except Exception as exc:
            self._show_error(str(exc))
            return

        if request.get("normalize_weather"):
            normalized_path = Path(request["output"]) / "weather" / NORMALIZED_WEATHER_NAME
            if normalized_path.exists():
                answer = QMessageBox.question(
                    self.iface.mainWindow(),
                    "Replace normalized weather data?",
                    "A normalized weather dataset already exists:\n\n"
                    f"{normalized_path}\n\n"
                    "Replace it with the result of this transformation?",
                    MESSAGE_BUTTON.Yes | MESSAGE_BUTTON.No,
                    MESSAGE_BUTTON.No,
                )
                if answer != MESSAGE_BUTTON.Yes:
                    self.dialog.append_log("Transformation cancelled; existing normalized data kept.")
                    return
                removed = self._remove_project_layers_for_path(normalized_path)
                if removed:
                    self.dialog.append_log(
                        f"Removed {removed} loaded normalized layer(s) before overwrite."
                    )
                    # Let QGIS dispose its providers before the worker attempts to
                    # replace the GeoPackage on Windows.
                    QCoreApplication.processEvents()

        if request.get("normalize_soil"):
            normalized_path = Path(request["output"]) / "soil" / NORMALIZED_SOIL_NAME
            if normalized_path.exists():
                answer = QMessageBox.question(
                    self.iface.mainWindow(),
                    "Replace normalized soil data?",
                    "A normalized soil dataset already exists:\n\n"
                    f"{normalized_path}\n\n"
                    "Replace it with the result of this transformation?",
                    MESSAGE_BUTTON.Yes | MESSAGE_BUTTON.No,
                    MESSAGE_BUTTON.No,
                )
                if answer != MESSAGE_BUTTON.Yes:
                    self.dialog.append_log("Transformation cancelled; existing normalized soil data kept.")
                    return
                removed = self._remove_project_layers_for_path(normalized_path)
                if removed:
                    self.dialog.append_log(
                        f"Removed {removed} loaded normalized soil layer(s) before overwrite."
                    )
                    QCoreApplication.processEvents()

        if request.get("normalize_landuse"):
            normalized_path = Path(request["output"]) / "landuse" / NORMALIZED_LANDUSE_NAME
            if normalized_path.exists():
                answer = QMessageBox.question(
                    self.iface.mainWindow(),
                    "Replace normalized land-use data?",
                    "A normalized land-use dataset already exists:\n\n"
                    f"{normalized_path}\n\n"
                    "Replace it with the result of this transformation?",
                    MESSAGE_BUTTON.Yes | MESSAGE_BUTTON.No,
                    MESSAGE_BUTTON.No,
                )
                if answer != MESSAGE_BUTTON.Yes:
                    self.dialog.append_log(
                        "Transformation cancelled; existing normalized land-use data kept."
                    )
                    return
                removed = self._remove_project_layers_for_path(normalized_path)
                if removed:
                    self.dialog.append_log(
                        f"Removed {removed} loaded normalized land-use layer(s) before overwrite."
                    )
                    QCoreApplication.processEvents()

        QSettings().setValue("IdrAgraGather/output", request["output"])
        self.dialog.set_running(True)
        self.dialog.append_log(f"Task started: {action}.")
        self.reporter = AcquisitionReporter(self.iface.mainWindow())
        self.reporter.status.connect(self._append_task_status)
        request["status_callback"] = self.reporter.status.emit
        self.task = QgsTask.fromFunction(
            f"IdrAgra: {action}",
            _run_acquisition,
            on_finished=self._task_finished,
            request=request,
        )
        QgsApplication.taskManager().addTask(self.task)

    def _task_finished(self, exception, result=None):
        self.task = None
        if self.reporter is not None:
            self.reporter.deleteLater()
            self.reporter = None
        if self.dialog is not None:
            self.dialog.set_running(False)
            self.dialog.refresh_status()
        if exception is not None:
            message = str(exception)
            if self.dialog is not None:
                self.dialog.append_log("ERROR: " + message)
            self._show_error(message)
            return
        if not result or result.get("cancelled"):
            if self.dialog is not None:
                self.dialog.append_log("Acquisition cancelled; completed files were kept.")
            return

        outputs = [Path(path) for path in result["outputs"]]
        if self.dialog is not None:
            self.dialog.append_log(f"Completed: {len(outputs)} output file(s).")
        loaded = 0
        if result.get("load_results"):
            for path in (Path(path) for path in result.get("load_paths", [])):
                loaded += self._load_spatial_file(path)
            if self.dialog is not None:
                self.dialog.append_log(f"Loaded {loaded} QGIS layer(s).")
        self.iface.messageBar().pushMessage(
            "IdrAgra Input Gatherer",
            "Input gathering completed.",
            level=MESSAGE_LEVEL.Success,
            duration=6,
        )

    def _append_task_status(self, message):
        if self.dialog is not None:
            self.dialog.append_log(str(message))

    def _load_spatial_file(self, path):
        if path.suffix.lower() == ".nc":
            return self._load_netcdf_sublayers(path)
        if path.suffix.lower() in {".gpkg", ".sqlite", ".shp"}:
            return self._load_vector_sublayers(path)
        if path.suffix.lower() == ".geojson":
            if path.stem.lower() == "aoi":
                self._remove_project_layers_for_path(path)
            layer = QgsVectorLayer(str(path), path.stem, "ogr")
            if layer.isValid():
                self._style_aoi_layer(layer, path)
                self._style_normalized_landuse_layer(layer, path)
                self._add_layer(layer, path)
                return 1
        if path.suffix.lower() in {".tif", ".tiff", ".vrt", ".asc"}:
            parts = {part.lower() for part in path.parts}
            if {"raw", "soil", "soilgrids"}.issubset(parts):
                ensure_soilgrids_raster_crs(path)
            layer = QgsRasterLayer(str(path), path.stem)
            if layer.isValid():
                self._style_raster_layer(layer)
                self._add_layer(layer, path)
                return 1
        return 0

    def _load_vector_sublayers(self, path):
        # Forward slashes avoid provider-specific interpretation of backslashes
        # in Windows paths, particularly for Shapefiles with spaces in a parent
        # directory name.
        vector_uri = Path(path).resolve().as_posix()
        details = QgsProviderRegistry.instance().querySublayers(vector_uri)
        options = QgsProviderSublayerDetails.LayerOptions(
            QgsProject.instance().transformContext()
        )
        loaded = 0
        for detail in details:
            layer = detail.toLayer(options)
            if layer is not None and layer.isValid():
                layer.setName(f"{path.stem} — {detail.name()}")
                self._style_normalized_soil_layer(layer, path)
                self._style_normalized_landuse_layer(layer, path)
                self._add_layer(layer, path)
                loaded += 1
        # Some older provider builds do not advertise a Shapefile as a
        # sublayer even though OGR can open it normally.
        if loaded == 0 and Path(path).suffix.lower() == ".shp":
            layer = QgsVectorLayer(vector_uri, Path(path).stem, "ogr")
            if layer.isValid():
                self._style_normalized_landuse_layer(layer, path)
                self._add_layer(layer, path)
                return 1
            if self.dialog is not None:
                provider_error = layer.error().summary() or "OGR returned no details"
                self.dialog.append_log(
                    f"WARNING: QGIS could not load vector output {path}: {provider_error}"
                )
        return loaded

    @staticmethod
    def _style_normalized_soil_layer(layer, path):
        """Categorize normalized soil profiles using QGIS random colors."""
        if Path(path).name.lower() != NORMALIZED_SOIL_NAME:
            return
        field_index = layer.fields().indexFromName("profile_id")
        if field_index < 0:
            return
        values = sorted(layer.uniqueValues(field_index), key=lambda value: int(value))
        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        if symbol is None:
            return
        categories = QgsCategorizedSymbolRenderer.createCategories(
            values, symbol, QgsRandomColorRamp()
        )
        layer.setRenderer(QgsCategorizedSymbolRenderer("profile_id", categories))
        layer.triggerRepaint()

    @staticmethod
    def _style_normalized_landuse_layer(layer, path):
        """Categorize normalized CORINE polygons by their readable class."""
        if Path(path).name.lower() != NORMALIZED_LANDUSE_NAME:
            return
        field_index = layer.fields().indexFromName("landuse")
        if field_index < 0:
            return
        values = sorted(layer.uniqueValues(field_index), key=str)
        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        if symbol is None:
            return
        categories = QgsCategorizedSymbolRenderer.createCategories(
            values, symbol, QgsRandomColorRamp()
        )
        layer.setRenderer(QgsCategorizedSymbolRenderer("landuse", categories))
        layer.triggerRepaint()

    def _load_netcdf_sublayers(self, path):
        details = QgsProviderRegistry.instance().querySublayers(str(path))
        options = QgsProviderSublayerDetails.LayerOptions(
            QgsProject.instance().transformContext()
        )
        loaded = 0
        for detail in details:
            layer = detail.toLayer(options)
            # GDAL, MDAL and OGR may all advertise the same NetCDF. Loading each
            # representation creates a raster, mesh and table for one variable.
            # Retain the raster and render time band 1 explicitly.
            if isinstance(layer, QgsRasterLayer) and layer.isValid():
                layer = self._georeference_netcdf_raster(layer, path, detail.name())
                self._style_raster_layer(layer)
                layer.setName(f"{path.stem} — {detail.name()}")
                self._add_layer(layer, path)
                loaded += 1
        if loaded == 0:
            layer = QgsRasterLayer(str(path), path.stem)
            if layer.isValid():
                layer = self._georeference_netcdf_raster(layer, path, path.stem)
                self._style_raster_layer(layer)
                self._add_layer(layer, path)
                return 1
        return loaded

    def _georeference_netcdf_raster(self, layer, path, variable_name):
        """Assign or repair WGS 84 georeferencing from NetCDF coordinate axes."""
        axes = self._netcdf_geographic_axes(path)
        if axes is None:
            return layer
        longitude, latitude = axes
        bounds = self._coordinate_bounds(longitude, latitude)
        if bounds is None:
            return layer

        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if self._extent_matches_bounds(layer.extent(), bounds):
            # setCrs labels the existing, correct lon/lat geotransform. QGIS then
            # handles reprojection to the project's CRS automatically.
            layer.setCrs(wgs84)
            return layer

        repaired = self._create_georeferenced_preview(
            layer, path, variable_name, bounds, wgs84
        )
        if repaired is not None:
            if self.dialog is not None:
                self.dialog.append_log(
                    f"Display: repaired missing NetCDF georeferencing for {path.name}."
                )
            return repaired

        # The coordinate axes prove that the source data are geographic. This is
        # still more useful than triggering QGIS's ambiguous-CRS prompt if an old
        # GDAL build cannot create the VRT fallback.
        layer.setCrs(wgs84)
        return layer

    @staticmethod
    def _netcdf_geographic_axes(path):
        try:
            from osgeo import gdal

            dataset = gdal.OpenEx(str(path), gdal.OF_MULTIDIM_RASTER)
            if dataset is None:
                return None
            root = dataset.GetRootGroup()
            latitude_array = root.OpenMDArray("latitude")
            longitude_array = root.OpenMDArray("longitude")
            if latitude_array is None or longitude_array is None:
                return None
            latitude = [float(value) for value in latitude_array.ReadAsArray().flat]
            longitude = [float(value) for value in longitude_array.ReadAsArray().flat]
            if not latitude or not longitude:
                return None
            if not all(math.isfinite(value) for value in latitude + longitude):
                return None
            if min(latitude) < -90.0 or max(latitude) > 90.0:
                return None
            if min(longitude) < -180.0 or max(longitude) > 360.0:
                return None
            return longitude, latitude
        except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
            return None

    @staticmethod
    def _coordinate_bounds(longitude, latitude):
        def axis_bounds(values):
            ordered = sorted(set(values))
            if len(ordered) == 1:
                # Both current weather providers use a 0.1 degree grid.
                spacing = 0.1
            else:
                intervals = [
                    right - left for left, right in zip(ordered, ordered[1:])
                ]
                spacing = sum(intervals) / len(intervals)
                tolerance = max(abs(spacing) * 0.01, 1e-8)
                if any(abs(interval - spacing) > tolerance for interval in intervals):
                    return None
            half_cell = abs(spacing) / 2.0
            return ordered[0] - half_cell, ordered[-1] + half_cell

        x_bounds = axis_bounds(longitude)
        y_bounds = axis_bounds(latitude)
        if x_bounds is None or y_bounds is None:
            return None
        return x_bounds[0], y_bounds[1], x_bounds[1], y_bounds[0]

    @staticmethod
    def _extent_matches_bounds(extent, bounds):
        west, north, east, south = bounds
        tolerance = max(east - west, north - south, 1.0) * 1e-5
        actual = (
            extent.xMinimum(),
            extent.yMaximum(),
            extent.xMaximum(),
            extent.yMinimum(),
        )
        return all(
            abs(found - expected) <= tolerance
            for found, expected in zip(actual, (west, north, east, south))
        )

    @staticmethod
    def _create_georeferenced_preview(layer, path, variable_name, bounds, crs):
        try:
            from osgeo import gdal

            source = gdal.Open(layer.source(), gdal.GA_ReadOnly)
            if source is None:
                return None
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(variable_name)).strip("_")
            signature = hashlib.sha256(layer.source().encode("utf-8")).hexdigest()[:10]
            preview_dir = Path(path).parent / ".qgis_previews"
            preview_dir.mkdir(exist_ok=True)
            preview_path = preview_dir / f"{Path(path).stem}_{safe_name}_{signature}.vrt"
            options = gdal.TranslateOptions(
                format="VRT",
                outputSRS=crs.toWkt(),
                outputBounds=list(bounds),
            )
            result = gdal.Translate(str(preview_path), source, options=options)
            if result is None:
                return None
            result = None
            repaired = QgsRasterLayer(str(preview_path), layer.name())
            if not repaired.isValid():
                return None
            repaired.setCrs(crs)
            return repaired
        except (ImportError, RuntimeError, TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _style_raster_layer(layer):
        """Give raw numeric rasters a visible, deterministic single-band style."""
        if layer.bandCount() < 1:
            return
        provider = layer.dataProvider()
        stats = provider.bandStatistics(1)
        minimum = float(stats.minimumValue)
        maximum = float(stats.maximumValue)
        if not (math.isfinite(minimum) and math.isfinite(maximum)):
            return
        if maximum <= minimum:
            maximum = minimum + 1.0

        stops = (
            (0.00, "#440154"),
            (0.25, "#3b528b"),
            (0.50, "#21918c"),
            (0.75, "#5ec962"),
            (1.00, "#fde725"),
        )
        span = maximum - minimum
        items = [
            QgsColorRampShader.ColorRampItem(
                minimum + fraction * span,
                QColor(color),
                f"{minimum + fraction * span:g}",
            )
            for fraction, color in stops
        ]
        color_shader = QgsColorRampShader(minimum, maximum)
        legacy_type = getattr(QgsColorRampShader, "Type", None)
        ramp_type = getattr(legacy_type, "Interpolated", None)
        if ramp_type is None:
            ramp_type = Qgis.ShaderInterpolationMethod.Linear
        color_shader.setColorRampType(ramp_type)
        color_shader.setColorRampItemList(items)
        raster_shader = QgsRasterShader()
        raster_shader.setRasterShaderFunction(color_shader)
        layer.setRenderer(QgsSingleBandPseudoColorRenderer(provider, 1, raster_shader))
        layer.triggerRepaint()

    @staticmethod
    def _style_aoi_layer(layer, path):
        """Keep the AOI useful as a boundary without hiding acquired rasters."""
        if Path(path).stem.lower() != "aoi":
            return
        symbol = QgsFillSymbol.createSimple(
            {
                "color": "0,0,0,0",
                "outline_color": "35,139,69,255",
                "outline_width": "0.8",
            }
        )
        layer.renderer().setSymbol(symbol)
        layer.triggerRepaint()

    @staticmethod
    def _add_layer(layer, path):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.findGroup(LAYER_GROUP)
        if group is None:
            group = root.insertGroup(0, LAYER_GROUP)
        elif root.children() and root.children()[0] is not group:
            # Keep gathered inputs above basemaps and pre-existing project layers.
            group.parent().takeChild(group)
            root.insertChildNode(0, group)
        target_group = group
        subgroup_name, is_raw = IdrAgraGatherPlugin._subgroup_for_path(path)
        if subgroup_name:
            target_group = group.findGroup(subgroup_name)
            if target_group is None:
                target_group = group.addGroup(subgroup_name)
                target_group.setExpanded(not is_raw)
            # Keep raw groups collapsed, but visible, so acquisition results
            # appear on the map without another manual toggle. This also repairs
            # a group left unchecked by an earlier plugin version.
            target_group.setItemVisibilityChecked(True)
        project.addMapLayer(layer, False)
        target_group.addLayer(layer)

    @staticmethod
    def _remove_project_layers_for_path(path):
        """Remove loaded layers backed by *path* and return the number removed."""
        target = os.path.normcase(os.path.abspath(str(path)))
        layer_ids = []
        for layer_id, layer in QgsProject.instance().mapLayers().items():
            # OGR sources append options such as ``|layername=...``.
            source_path = str(layer.source()).split("|", 1)[0]
            if os.path.normcase(os.path.abspath(source_path)) == target:
                layer_ids.append(layer_id)
        if layer_ids:
            QgsProject.instance().removeMapLayers(layer_ids)
        return len(layer_ids)

    @staticmethod
    def _subgroup_for_path(path):
        parts = [part.lower() for part in Path(path).parts]
        if "raw" in parts:
            index = parts.index("raw")
            if index + 1 < len(parts):
                return f"{parts[index + 1]}_raw", True
        parent = Path(path).parent.name.lower()
        if parent in {"weather", "soil", "landuse", "topography"}:
            return parent, False
        return None, False

    def _show_error(self, message):
        QMessageBox.critical(self.iface.mainWindow(), "IdrAgra Input Gatherer", message)

    def _dialog_closed(self):
        if self.canvas.mapTool() is self.map_tool:
            self.iface.actionPan().trigger()
