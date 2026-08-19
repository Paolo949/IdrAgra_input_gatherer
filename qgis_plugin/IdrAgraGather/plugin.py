from pathlib import Path

from qgis.PyQt.QtCore import QDir, QObject, QSettings, pyqtSignal # pyright: ignore[reportAttributeAccessIssue]
from qgis.PyQt.QtWidgets import QAction, QMessageBox # pyright: ignore[reportAttributeAccessIssue]
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsProviderRegistry,
    QgsProviderSublayerDetails,
    QgsRasterLayer,
    QgsTask,
    QgsVectorLayer,
)

from .core.era5_normalize import normalize_era5_files
from .core.eobs_normalize import normalize_eobs_files
from .core.models import BoundingBox, DateWindow
from .core.providers.corine import fetch as fetch_corine
from .core.providers.era5_land import fetch, plan_jobs, write_plan
from .core.providers.eobs import fetch as fetch_eobs
from .core.providers.eobs import plan_jobs as plan_eobs_jobs
from .core.providers.soilgrids import DEPTHS as SOIL_DEPTHS
from .core.providers.soilgrids import PROPERTIES as SOIL_PROPERTIES
from .core.providers.soilgrids import fetch as fetch_soilgrids
from .core.providers.soilgrids import plan_jobs as plan_soilgrids_jobs
from .core.staging import StagingArea
from .dialog import (
    AcquisitionDialog,
    LANDUSE_SOURCE_CORINE,
    SOIL_DEPTHS_TOPSOIL,
    SOIL_SOURCE_SOILGRIDS,
)
from .map_tool import RectangleMapTool


MENU_NAME = "&IdrAgra"
LAYER_GROUP = "IdrAgra gathered inputs"
MESSAGE_LEVEL = getattr(Qgis, "MessageLevel", Qgis)


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
            weather_dir = Path(request["output"]) / "raw" / "weather" / "era5_land"
            weather_outputs = sorted(weather_dir.glob("*.nc"))
            if not weather_outputs:
                raise ValueError(f"No staged ERA5-Land NetCDF files found in {weather_dir}")
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
            weather_dir = Path(request["output"]) / "raw" / "weather" / "eobs"
            expected = plan_eobs_jobs(bbox, window)
            weather_outputs = [weather_dir / job.target_name for job in expected]
            missing = [path for path in weather_outputs if not path.is_file()]
            if missing:
                raise ValueError(
                    "No complete staged E-OBS subset was found for this exact AOI and "
                    "date window. Run Acquire raw or Acquire + transform first."
                )
            eobs_status(f"Found {len(weather_outputs)} staged NetCDF file(s).")

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

    if request.get("soil_source") == SOIL_SOURCE_SOILGRIDS:
        report_status = request.get("status_callback")
        depths = SOIL_DEPTHS[:3] if request.get("soil_depths") == SOIL_DEPTHS_TOPSOIL else SOIL_DEPTHS

        def soil_progress(done, total, _path):
            task.setProgress(max(task.progress(), 96 + 3 * done / total))

        def soil_status(message):
            if report_status is not None:
                report_status("SoilGrids: " + message)

        soil_outputs = fetch_soilgrids(
            request["output"], bbox, depths=depths,
            is_cancelled=task.isCanceled, on_progress=soil_progress,
            on_status=soil_status,
        )
        outputs.extend(soil_outputs)
        load_paths.extend(soil_outputs)

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
        load_paths.extend(landuse_outputs)
    
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
        if path.suffix.lower() in {".gpkg", ".sqlite"}:
            return self._load_vector_sublayers(path)
        if path.suffix.lower() in {".geojson", ".shp"}:
            layer = QgsVectorLayer(str(path), path.stem, "ogr")
            if layer.isValid():
                self._add_layer(layer, path)
                return 1
        if path.suffix.lower() in {".tif", ".tiff", ".vrt", ".asc"}:
            layer = QgsRasterLayer(str(path), path.stem)
            if layer.isValid():
                self._add_layer(layer, path)
                return 1
        return 0

    def _load_vector_sublayers(self, path):
        details = QgsProviderRegistry.instance().querySublayers(str(path))
        options = QgsProviderSublayerDetails.LayerOptions(
            QgsProject.instance().transformContext()
        )
        loaded = 0
        for detail in details:
            layer = detail.toLayer(options)
            if layer is not None and layer.isValid():
                layer.setName(f"{path.stem} — {detail.name()}")
                self._add_layer(layer, path)
                loaded += 1
        return loaded

    def _load_netcdf_sublayers(self, path):
        details = QgsProviderRegistry.instance().querySublayers(str(path))
        options = QgsProviderSublayerDetails.LayerOptions(
            QgsProject.instance().transformContext()
        )
        loaded = 0
        for detail in details:
            layer = detail.toLayer(options)
            if layer is not None and layer.isValid():
                layer.setName(f"{path.stem} — {detail.name()}")
                self._add_layer(layer, path)
                loaded += 1
        if loaded == 0:
            layer = QgsRasterLayer(str(path), path.stem)
            if layer.isValid():
                self._add_layer(layer, path)
                return 1
        return loaded

    @staticmethod
    def _add_layer(layer, path):
        project = QgsProject.instance()
        group = project.layerTreeRoot().findGroup(LAYER_GROUP)
        if group is None:
            group = project.layerTreeRoot().addGroup(LAYER_GROUP)
        target_group = group
        subgroup_name, is_raw = IdrAgraGatherPlugin._subgroup_for_path(path)
        if subgroup_name:
            target_group = group.findGroup(subgroup_name)
            if target_group is None:
                target_group = group.addGroup(subgroup_name)
                target_group.setExpanded(not is_raw)
                if is_raw:
                    target_group.setItemVisibilityChecked(False)
        project.addMapLayer(layer, False)
        target_group.addLayer(layer)

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
