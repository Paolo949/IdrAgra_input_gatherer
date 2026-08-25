import hashlib
import math
import os
from pathlib import Path
import re

from qgis.PyQt.QtCore import QCoreApplication, QDir, QObject, QSettings, pyqtSignal  # pyright: ignore[reportAttributeAccessIssue]
from qgis.PyQt.QtGui import QColor  # pyright: ignore[reportAttributeAccessIssue]
from qgis.PyQt.QtWidgets import QAction, QMessageBox  # pyright: ignore[reportAttributeAccessIssue]
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
    QgsRendererCategory,
    QgsSingleBandPseudoColorRenderer,
    QgsSymbol,
    QgsTask,
    QgsVectorLayer,
)
from .cell_dialog import CellBuilderDialog
from .core.cells import CELLS_NAME, build_simulation_cells
from .core.corine_normalize import normalize_corine_file
from .core.era5_normalize import normalize_era5_files
from .core.eobs_normalize import common_date_coverage, normalize_eobs_files
from .core.models import BoundingBox, DateWindow
from .core.providers.copernicus_dem import fetch as fetch_copernicus_dem
from .core.providers.copernicus_dem import find_tiles as find_copernicus_dem_tiles
from .core.providers.corine import fetch as fetch_corine
from .core.providers.era5_land import fetch, write_plan
from .core.providers.eobs import fetch as fetch_eobs
from .core.providers.soilgrids import ensure_raster_crs as ensure_soilgrids_raster_crs
from .core.providers.soilgrids import fetch as fetch_soilgrids
from .core.soilgrids_normalize import normalize_soilgrids_files
from .core.soil_ptf import OUTPUT_NAME as SOIL_PTF_OUTPUT_NAME
from .core.soil_ptf import apply_rosetta3_to_workspace
from .core.staging import StagingArea, find_staged_files
from .core.topography_normalize import normalize_dem_files
from .dialog import (
    AcquisitionDialog,
    LANDUSE_SOURCE_CORINE,
    SOIL_SOURCE_SOILGRIDS,
    TOPOGRAPHY_SOURCE_COPERNICUS,
)
from .map_tool import RectangleMapTool
from .soil_ptf_dialog import SoilPtfDialog
from .v2_export_dialog import V2ExportDialog
from .core.v2_export import export_v2_workspace


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


# Runs the acquisition and/or normalization workflow for either weather/soil/landuse/topography
def _run_acquisition(task, request):
    bbox = BoundingBox(*request["bbox"])
    window = DateWindow.from_iso(request["start"], request["end"])
    staging = StagingArea(request["output"])
    aoi_path = staging.write_aoi(bbox)
    outputs = [aoi_path]
    load_paths = [aoi_path]
    task.setProgress(5)

    match request["action"]:
        case "weather-acquire" | "weather-transform" | "weather-both":
            new_outputs, new_load_paths = _process_weather(task, request, bbox, window)
        case "soil-acquire" | "soil-transform" | "soil-both":
            new_outputs, new_load_paths = _process_soil(task, request, bbox)
        case "landuse-acquire" | "landuse-transform" | "landuse-both":
            new_outputs, new_load_paths = _process_landuse(task, request, bbox)
        case "topography-acquire" | "topography-transform" | "topography-both":
            new_outputs, new_load_paths = _process_topography(task, request, bbox)
        case unknown_action:
            raise ValueError(f"Unknown acquisition action: {unknown_action}")

    outputs.extend(new_outputs)
    load_paths.extend(new_load_paths)
    if task.isCanceled():
        return {"cancelled": True, "outputs": [str(path) for path in outputs]}

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


# Run version-neutral cell generation in a QGIS background task.
def _run_cell_builder(task, request):
    status_callback = request.get("status_callback")

    def update_status(message):
        if status_callback is not None:
            status_callback(str(message))

    task.setProgress(5)
    result = build_simulation_cells(
        request["output"],
        mode=request["mode"],
        cell_width_m=request["cell_width_m"],
        grid_boundary_policy=request.get("grid_boundary_policy", "inside"),
        elevation_method=request["elevation_method"],
        slope_method=request["slope_method"],
        crops=request["crops"],
        landuses=request["landuses"],
        allocations=request["allocations"],
        on_status=update_status,
    )
    task.setProgress(100)
    return {
        "cancelled": bool(task.isCanceled()),
        "outputs": [str(path) for path in result.paths],
        "load_paths": [str(result.cells_path), *(str(path) for path in result.raster_paths)],
        "cell_count": result.cell_count,
        "warnings": list(result.warnings),
    }


# Run a soil PTF in a QGIS background task.
def _run_soil_ptf(task, request):
    status_callback = request.get("status_callback")

    def update_status(message):
        if status_callback is not None:
            status_callback(str(message))

    if request.get("method") != "rosetta3":
        raise ValueError(f"Unsupported soil PTF: {request.get('method')}")
    task.setProgress(5)
    result = apply_rosetta3_to_workspace(
        request["output"],
        use_bulk_density=bool(request.get("use_bulk_density", True)),
        on_status=update_status,
    )
    task.setProgress(100)
    return {
        "cancelled": bool(task.isCanceled()),
        "path": str(result.path),
        "profile_count": result.profile_count,
        "horizon_count": result.horizon_count,
        "model_code": result.model_code,
        "warnings": list(result.warnings),
    }


# Run the IdrAgra v2 exporter in a QGIS background task.
def _run_v2_export(task, request):
    status_callback = request.get("status_callback")
    task.setProgress(5)
    result = export_v2_workspace(
        request["workspace"],
        request["destination"],
        evap_layer_m=request["evap_layer_m"],
        root_layer_m=request["root_layer_m"],
        weather_neighbors=request["weather_neighbors"],
        hydrologic_condition=request["hydrologic_condition"],
        crop_parameter_folder=request.get("crop_parameter_folder"),
        overwrite=bool(request.get("overwrite")),
        on_status=status_callback,
    )
    task.setProgress(100)
    return {
        "cancelled": bool(task.isCanceled()),
        "output_path": str(result.output_path),
        "file_count": result.file_count,
        "station_count": result.station_count,
        "active_landuses": list(result.active_landuses),
        "start": result.start.isoformat(),
        "end": result.end.isoformat(),
        "warnings": list(result.warnings),
    }


# Select and run the requested weather provider workflow.
def _process_weather(task, request, bbox, window):
    source = request.get("weather_source")
    if source is None:
        return [], []
    if source == "era5-plan":
        task.setProgress(80)
        return [write_plan(request["output"], bbox, window)], []
    if source in {"era5-download", "era5-normalize"}:
        return _process_era5_weather(task, request, bbox, window)
    if source in {"eobs-download", "eobs-normalize"}:
        return _process_eobs_weather(task, request, bbox, window)
    return [], []


# Fetch or locate ERA5-Land files, then normalize them when requested.
def _process_era5_weather(task, request, bbox, window):
    def update_progress(done, total, _path):
        task.setProgress(5 + 80 * done / total)

    def update_status(message):
        _report_status(request, "CDS: ", message)
        lowered = message.lower()
        if "download" in lowered:
            task.setProgress(max(task.progress(), 65))
        elif "running" in lowered:
            task.setProgress(max(task.progress(), 25))
        elif "accepted" in lowered or "queue" in lowered or "submitting" in lowered:
            task.setProgress(max(task.progress(), 10))

    if request["weather_source"] == "era5-download":
        raw_paths = fetch(
            request["output"],
            bbox,
            window,
            is_cancelled=task.isCanceled,
            on_progress=update_progress,
            on_status=update_status,
        )
    else:
        raw_paths = find_staged_files(
            request["output"],
            provider="copernicus-cds",
            suffix=".nc",
        )
        _report_status(
            request,
            "Normalize: ",
            f"found {len(raw_paths)} staged NetCDF file(s).",
        )

    outputs = list(raw_paths)
    if not request.get("normalize_weather", True) or task.isCanceled():
        return outputs, list(raw_paths)

    task.setProgress(max(task.progress(), 88))
    normalized = normalize_era5_files(
        raw_paths,
        request["output"],
        window,
        timezone_name=request.get("timezone", "Europe/Rome"),
        on_status=lambda message: _report_status(request, "Normalize: ", message),
    )
    outputs.append(normalized.path)
    for warning in normalized.warnings:
        _report_status(request, "WARNING: ", warning)
    task.setProgress(96)
    return outputs, [normalized.path]


# Fetch or locate E-OBS files, then normalize them when requested.
def _process_eobs_weather(task, request, bbox, window):
    def update_progress(done, total, _path):
        task.setProgress(5 + 80 * done / total)

    def update_status(message):
        _report_status(request, "E-OBS: ", message)

    if request["weather_source"] == "eobs-download":
        raw_paths = fetch_eobs(
            request["output"],
            bbox,
            window,
            is_cancelled=task.isCanceled,
            on_progress=update_progress,
            on_status=update_status,
        )
    else:
        raw_paths = find_staged_files(
            request["output"],
            provider="eobs-knmi",
            suffix=".nc",
        )
        update_status(f"Found {len(raw_paths)} staged NetCDF file(s).")

    outputs = list(raw_paths)
    if not request.get("normalize_weather", True) and not task.isCanceled():
        coverage = common_date_coverage(raw_paths)
        if coverage.start > window.start or coverage.end < window.end:
            update_status(
                "WARNING: The staged provisional files cover "
                f"{coverage.start} to {coverage.end}, not the full requested "
                f"interval {window.start} to {window.end}. Normalization will "
                "use the common available dates."
            )

    if not request.get("normalize_weather", True) or task.isCanceled():
        return outputs, list(raw_paths)

    task.setProgress(max(task.progress(), 88))
    normalized = normalize_eobs_files(
        raw_paths,
        request["output"],
        bbox,
        window,
        on_status=lambda message: _report_status(request, "Normalize: ", message),
    )
    outputs.append(normalized.path)
    for warning in normalized.warnings:
        _report_status(request, "WARNING: ", warning)
    task.setProgress(96)
    return outputs, [normalized.path]


# Fetch or locate SoilGrids rasters, then normalize them when requested.
def _process_soil(task, request, bbox):
    raw_paths = []
    if request.get("soil_source") == SOIL_SOURCE_SOILGRIDS:
        raw_paths = fetch_soilgrids(
            request["output"],
            bbox,
            is_cancelled=task.isCanceled,
            on_progress=lambda done, total, _path: task.setProgress(max(task.progress(), 96 + 3 * done / total)),
            on_status=lambda message: _report_status(request, "SoilGrids: ", message),
        )

    outputs = list(raw_paths)
    if not request.get("normalize_soil") or task.isCanceled():
        return outputs, list(raw_paths)
    if not raw_paths:
        raw_paths = list(
            find_staged_files(
                request["output"],
                provider="isric-soilgrids-wcs",
                suffix=".tif",
            )
        )

    task.setProgress(max(task.progress(), 96))
    normalized = normalize_soilgrids_files(
        raw_paths,
        request["output"],
        bbox=bbox,
        max_classes=request.get("soil_max_classes", 20),
        on_status=lambda message: _report_status(request, "Normalize soil: ", message),
    )
    outputs.append(normalized.path)
    _report_status(
        request,
        "Normalize soil: ",
        f"wrote {normalized.polygon_count} polygon(s) for "
        f"{normalized.profile_count} class(es), condensed from "
        f"{normalized.exact_profile_count} exact profile(s); filled "
        f"{normalized.filled_nodata_cells} NoData cell(s) to cover the AOI.",
    )
    task.setProgress(99)
    return outputs, [normalized.path]


# Fetch or locate CORINE polygons, then normalize them when requested.
def _process_landuse(task, request, bbox):
    raw_paths = []
    if request.get("landuse_source") == LANDUSE_SOURCE_CORINE:
        raw_paths = fetch_corine(
            request["output"],
            bbox,
            is_cancelled=task.isCanceled,
            on_progress=lambda done, total, _path: task.setProgress(max(task.progress(), 95 + 4 * done / total)),
            on_status=lambda message: _report_status(request, "CORINE: ", message),
        )

    outputs = list(raw_paths)
    if not request.get("normalize_landuse") or task.isCanceled():
        return outputs, list(raw_paths)
    if not raw_paths:
        raw_paths = list(
            find_staged_files(
                request["output"],
                provider="eea-corine-arcgis-rest",
                suffix=".geojson",
            )
        )

    normalized = normalize_corine_file(
        raw_paths[0],
        request["output"],
        bbox=bbox,
        on_status=lambda message: _report_status(request, "Normalize land use: ", message),
    )
    outputs.append(normalized.path)
    task.setProgress(99)
    return outputs, [normalized.path]


# Fetch or locate Copernicus DEM tiles, then create metric elevation and slope rasters.
def _process_topography(task, request, bbox):
    raw_paths = []
    instance = request.get("dem_instance", "COPERNICUS_30")
    if request.get("topography_source") == TOPOGRAPHY_SOURCE_COPERNICUS:
        raw_paths = fetch_copernicus_dem(
            request["output"],
            bbox,
            instance=instance,
            client_id=request.get("copernicus_client_id"),
            client_secret=request.get("copernicus_client_secret"),
            is_cancelled=task.isCanceled,
            on_progress=lambda done, total, _path: task.setProgress(max(task.progress(), 5 + 80 * done / total)),
            on_status=lambda message: _report_status(request, "Copernicus DEM: ", message),
        )

    outputs = list(raw_paths)
    if not request.get("normalize_topography") or task.isCanceled():
        return outputs, list(raw_paths)
    if not raw_paths:
        raw_paths = list(
            find_copernicus_dem_tiles(
                request["output"],
                bbox,
                instance=instance,
            )
        )
        _report_status(
            request,
            "Normalize topography: ",
            f"found {len(raw_paths)} staged DEM tile(s).",
        )

    task.setProgress(max(task.progress(), 88))
    normalized = normalize_dem_files(
        raw_paths,
        request["output"],
        bbox=bbox,
        resolution_m=request.get("topography_resolution_m", 30),
        dem_instance=instance,
        on_status=lambda message: _report_status(request, "Normalize topography: ", message),
    )
    outputs.extend(normalized.paths)
    task.setProgress(99)
    return outputs, list(normalized.paths)


# Forward a worker message to the dialog when a status callback is available.
def _report_status(request, prefix, message):
    callback = request.get("status_callback")
    if callback is not None:
        callback(prefix + message)


class IdrAgraGatherPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.action = None
        self.cell_action = None
        self.ptf_action = None
        self.v2_export_action = None
        self.dialog = None
        self.cell_dialog = None
        self.ptf_dialog = None
        self.v2_export_dialog = None
        self.map_tool = RectangleMapTool(self.canvas)
        self.map_tool.rectangleCreated.connect(self._rectangle_created)
        self.map_tool.cancelled.connect(self._drawing_cancelled)
        self.task = None
        self.reporter = None

    def initGui(self):
        action = QAction("Gather IdrAgra inputs…", self.iface.mainWindow())
        action.setToolTip("Draw an area and gather raw IdrAgra inputs")
        action.triggered.connect(self.show_dialog)
        self.iface.addPluginToMenu(MENU_NAME, action)
        self.iface.addToolBarIcon(action)
        self.action = action

        cell_action = QAction("Build IdrAgra simulation cells...", self.iface.mainWindow())
        cell_action.setToolTip("Combine normalized inputs into an IdrAgra cell view")
        cell_action.triggered.connect(self.show_cell_dialog)
        self.iface.addPluginToMenu(MENU_NAME, cell_action)
        self.iface.addToolBarIcon(cell_action)
        self.cell_action = cell_action

        ptf_action = QAction("Derive soil hydraulic properties...", self.iface.mainWindow())
        ptf_action.setToolTip("Apply a pedotransfer function to normalized soil profiles")
        ptf_action.triggered.connect(self.show_soil_ptf_dialog)
        self.iface.addPluginToMenu(MENU_NAME, ptf_action)
        self.iface.addToolBarIcon(ptf_action)
        self.ptf_action = ptf_action

        export_action = QAction("Export IdrAgra v2 inputs...", self.iface.mainWindow())
        export_action.setToolTip("Convert the regular-grid workspace to IdrAgra v2 files")
        export_action.triggered.connect(self.show_v2_export_dialog)
        self.iface.addPluginToMenu(MENU_NAME, export_action)
        self.iface.addToolBarIcon(export_action)
        self.v2_export_action = export_action

    def unload(self):
        action = self.action
        if action is not None:
            self.iface.removePluginMenu(MENU_NAME, action)
            self.iface.removeToolBarIcon(action)
        cell_action = self.cell_action
        if cell_action is not None:
            self.iface.removePluginMenu(MENU_NAME, cell_action)
            self.iface.removeToolBarIcon(cell_action)
        ptf_action = self.ptf_action
        if ptf_action is not None:
            self.iface.removePluginMenu(MENU_NAME, ptf_action)
            self.iface.removeToolBarIcon(ptf_action)
        export_action = self.v2_export_action
        if export_action is not None:
            self.iface.removePluginMenu(MENU_NAME, export_action)
            self.iface.removeToolBarIcon(export_action)
        if self.canvas.mapTool() is self.map_tool:
            self.iface.actionPan().trigger()
        self.map_tool.clear()
        dialog = self.dialog
        if dialog is not None:
            dialog.close()
        cell_dialog = self.cell_dialog
        if cell_dialog is not None:
            cell_dialog.close()
        ptf_dialog = self.ptf_dialog
        if ptf_dialog is not None:
            ptf_dialog.close()
        export_dialog = self.v2_export_dialog
        if export_dialog is not None:
            export_dialog.close()

    def show_cell_dialog(self):
        dialog = self.cell_dialog
        if dialog is None:
            dialog = CellBuilderDialog(self.iface.mainWindow())
            dialog.runRequested.connect(self._run_cells)
            settings = QSettings()
            default_output = _qgis_project().homePath() or QDir.homePath()
            dialog.set_workspace(settings.value("IdrAgraGather/output", default_output, type=str))
            self.cell_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def show_soil_ptf_dialog(self):
        dialog = self.ptf_dialog
        if dialog is None:
            dialog = SoilPtfDialog(self.iface.mainWindow())
            dialog.runRequested.connect(self._run_ptf)
            settings = QSettings()
            default_output = _qgis_project().homePath() or QDir.homePath()
            dialog.set_workspace(settings.value("IdrAgraGather/output", default_output, type=str))
            self.ptf_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def show_v2_export_dialog(self):
        dialog = self.v2_export_dialog
        if dialog is None:
            dialog = V2ExportDialog(self.iface.mainWindow())
            dialog.runRequested.connect(self._run_v2_export)
            settings = QSettings()
            default_output = _qgis_project().homePath() or QDir.homePath()
            dialog.set_workspace(settings.value("IdrAgraGather/output", default_output, type=str))
            self.v2_export_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _run_v2_export(self, request):
        if self.task is not None:
            self._show_error("Another IdrAgra task is already running.")
            return
        dialog = self.v2_export_dialog
        if dialog is None:
            return
        destination = Path(request["destination"])
        if destination.exists() and destination.is_dir() and any(destination.iterdir()):
            answer = QMessageBox.question(
                self.iface.mainWindow(),
                "Replace existing v2 export?",
                f"The export folder is not empty:\n{destination}\n\nReplace its contents?",
                MESSAGE_BUTTON.Yes | MESSAGE_BUTTON.No,
                MESSAGE_BUTTON.No,
            )
            if answer != MESSAGE_BUTTON.Yes:
                dialog.append_log("Export cancelled; existing files kept.")
                return
            request["overwrite"] = True
        QSettings().setValue("IdrAgraGather/output", request["workspace"])
        dialog.set_running(True)
        dialog.append_log("IdrAgra v2 export started.")
        reporter = AcquisitionReporter(self.iface.mainWindow())
        reporter.status.connect(dialog.append_log)
        request["status_callback"] = reporter.status.emit
        self.reporter = reporter
        worker_task = QgsTask.fromFunction(
            "IdrAgra: export v2 inputs",
            _run_v2_export,
            on_finished=self._v2_export_finished,
            request=request,
        )
        self.task = worker_task
        _qgis_task_manager().addTask(worker_task)

    def _v2_export_finished(self, exception, result=None):
        self.task = None
        reporter = self.reporter
        if reporter is not None:
            reporter.deleteLater()
            self.reporter = None
        dialog = self.v2_export_dialog
        if dialog is not None:
            dialog.set_running(False)
        if exception is not None:
            message = str(exception)
            if dialog is not None:
                dialog.append_log("ERROR: " + message)
            self._show_error(message)
            return
        if not result or result.get("cancelled"):
            if dialog is not None:
                dialog.append_log("v2 export cancelled.")
            return
        if dialog is not None:
            dialog.append_log(
                f"Completed: {result['file_count']} file(s), "
                f"{result['station_count']} weather station(s), "
                f"{result['start']} to {result['end']}."
            )
            for warning in result.get("warnings", []):
                dialog.append_log("NOTE: " + warning)
        self.iface.messageBar().pushMessage(
            "IdrAgra Input Gatherer",
            f"IdrAgra v2 package written to {result['output_path']}",
            level=MESSAGE_LEVEL.Success,
            duration=8,
        )

    def _run_ptf(self, request):
        if self.task is not None:
            self._show_error("Another IdrAgra task is already running.")
            return
        dialog = self.ptf_dialog
        if dialog is None:
            return
        output = Path(request["output"]) / "soil" / SOIL_PTF_OUTPUT_NAME
        if output.exists():
            removed = self._remove_project_layers_for_path(output)
            if removed:
                dialog.append_log(f"Removed {removed} loaded hydraulic table(s) before replacement.")
                QCoreApplication.processEvents()

        QSettings().setValue("IdrAgraGather/output", request["output"])
        dialog.set_running(True)
        hierarchy = "H3" if request.get("use_bulk_density", True) else "H2"
        dialog.append_log(f"Rosetta 3 {hierarchy} run started.")
        reporter = AcquisitionReporter(self.iface.mainWindow())
        reporter.status.connect(dialog.append_log)
        request["status_callback"] = reporter.status.emit
        self.reporter = reporter
        worker_task = QgsTask.fromFunction(
            f"IdrAgra: Rosetta 3 {hierarchy}",
            _run_soil_ptf,
            on_finished=self._ptf_task_finished,
            request=request,
        )
        self.task = worker_task
        _qgis_task_manager().addTask(worker_task)

    def _ptf_task_finished(self, exception, result=None):
        self.task = None
        reporter = self.reporter
        if reporter is not None:
            reporter.deleteLater()
            self.reporter = None
        dialog = self.ptf_dialog
        if dialog is not None:
            dialog.set_running(False)
            dialog.refresh_status()
        if exception is not None:
            message = str(exception)
            if dialog is not None:
                dialog.append_log("ERROR: " + message)
            self._show_error(message)
            return
        if not result or result.get("cancelled"):
            if dialog is not None:
                dialog.append_log("Soil PTF run cancelled.")
            return
        loaded = self._load_spatial_file(Path(result["path"]))
        if dialog is not None:
            dialog.append_log(
                f"Completed: {result['profile_count']} profile(s), "
                f"{result['horizon_count']} horizon(s); loaded {loaded} table(s)."
            )
            for warning in result.get("warnings", []):
                dialog.append_log("NOTE: " + warning)
        self.iface.messageBar().pushMessage(
            "IdrAgra Input Gatherer",
            "Rosetta soil hydraulic properties are ready.",
            level=MESSAGE_LEVEL.Success,
            duration=6,
        )

    def _run_cells(self, request):
        if self.task is not None:
            self._show_error("Another IdrAgra task is already running.")
            return
        dialog = self.cell_dialog
        if dialog is None:
            return
        output_dir = Path(request["output"]) / "cells"
        existing = [
            path
            for path in (
                output_dir / CELLS_NAME,
                output_dir / "soil_id.tif",
                output_dir / "landuse_id.tif",
                output_dir / "elevation_m_asl.tif",
                output_dir / "slope_pct.tif",
            )
            if path.exists()
        ]
        if existing:
            answer = QMessageBox.question(
                self.iface.mainWindow(),
                "Replace existing cell view?",
                "A cell view already exists in this workspace. Replace its spatial outputs with the new configuration?",
                MESSAGE_BUTTON.Yes | MESSAGE_BUTTON.No,
                MESSAGE_BUTTON.No,
            )
            if answer != MESSAGE_BUTTON.Yes:
                dialog.append_log("Cell generation cancelled; existing outputs kept.")
                return
            for path in existing:
                self._remove_project_layers_for_path(path)
            QCoreApplication.processEvents()

        QSettings().setValue("IdrAgraGather/output", request["output"])
        dialog.set_running(True)
        dialog.append_log(f"Cell generation started in {request['mode']} mode.")
        reporter = AcquisitionReporter(self.iface.mainWindow())
        reporter.status.connect(dialog.append_log)
        request["status_callback"] = reporter.status.emit
        self.reporter = reporter
        worker_task = QgsTask.fromFunction(
            "IdrAgra: build simulation cells",
            _run_cell_builder,
            on_finished=self._cell_task_finished,
            request=request,
        )
        self.task = worker_task
        _qgis_task_manager().addTask(worker_task)

    def _cell_task_finished(self, exception, result=None):
        self.task = None
        reporter = self.reporter
        if reporter is not None:
            reporter.deleteLater()
            self.reporter = None
        dialog = self.cell_dialog
        if dialog is not None:
            dialog.set_running(False)
        if exception is not None:
            message = str(exception)
            if dialog is not None:
                dialog.append_log("ERROR: " + message)
            self._show_error(message)
            return
        if not result or result.get("cancelled"):
            if dialog is not None:
                dialog.append_log("Cell generation cancelled.")
            return
        loaded = sum(self._load_spatial_file(Path(path)) for path in result.get("load_paths", []))
        if dialog is not None:
            dialog.append_log(f"Completed: {result['cell_count']} cell(s); loaded {loaded} layer(s).")
            for warning in result.get("warnings", []):
                dialog.append_log("WARNING: " + warning)
        self.iface.messageBar().pushMessage(
            "IdrAgra Input Gatherer",
            f"Simulation cell view ready: {result['cell_count']} cell(s).",
            level=MESSAGE_LEVEL.Success,
            duration=6,
        )

    def _start_drawing(self):
        dialog = self.dialog
        if dialog is None:
            return
        dialog.hide()
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

    def show_dialog(self):
        dialog = self.dialog
        if dialog is None:
            dialog = AcquisitionDialog(self.iface.mainWindow())
            dialog.drawRequested.connect(self._start_drawing)
            dialog.canvasExtentRequested.connect(self._use_canvas_extent)
            dialog.runRequested.connect(self._run)
            dialog.finished.connect(self._dialog_closed)
            settings = QSettings()
            default_output = _qgis_project().homePath() or QDir.homePath()
            dialog.set_output_folder(settings.value("IdrAgraGather/output", default_output, type=str))
            self.dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

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
                _qgis_project().transformContext(),
            )
            geometry.transform(transform)
        bbox = geometry.boundingBox()
        dialog = self.dialog
        if dialog is not None:
            dialog.set_bbox(bbox.xMinimum(), bbox.yMinimum(), bbox.xMaximum(), bbox.yMaximum())
            dialog.append_log("Study area selected in EPSG:4326.")

    def _run(self, action):
        if self.task is not None:
            self._show_error("An acquisition task is already running.")
            return
        dialog = self.dialog
        if dialog is None:
            return
        try:
            request = dialog.request(action)
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
                    dialog.append_log("Transformation cancelled; existing normalized data kept.")
                    return
                removed = self._remove_project_layers_for_path(normalized_path)
                if removed:
                    dialog.append_log(f"Removed {removed} loaded normalized layer(s) before overwrite.")
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
                    dialog.append_log("Transformation cancelled; existing normalized soil data kept.")
                    return
                removed = self._remove_project_layers_for_path(normalized_path)
                if removed:
                    dialog.append_log(f"Removed {removed} loaded normalized soil layer(s) before overwrite.")
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
                    dialog.append_log("Transformation cancelled; existing normalized land-use data kept.")
                    return
                removed = self._remove_project_layers_for_path(normalized_path)
                if removed:
                    dialog.append_log(f"Removed {removed} loaded normalized land-use layer(s) before overwrite.")
                    QCoreApplication.processEvents()

        QSettings().setValue("IdrAgraGather/output", request["output"])
        dialog.set_running(True)
        dialog.append_log(f"Task started: {action}.")
        reporter = AcquisitionReporter(self.iface.mainWindow())
        reporter.status.connect(self._append_task_status)
        request["status_callback"] = reporter.status.emit
        self.reporter = reporter
        worker_task = QgsTask.fromFunction(
            f"IdrAgra: {action}",
            _run_acquisition,
            on_finished=self._task_finished,
            request=request,
        )
        self.task = worker_task
        _qgis_task_manager().addTask(worker_task)

    def _task_finished(self, exception, result=None):
        self.task = None
        reporter = self.reporter
        if reporter is not None:
            reporter.deleteLater()
            self.reporter = None
        dialog = self.dialog
        if dialog is not None:
            dialog.set_running(False)
            dialog.refresh_status()
        if exception is not None:
            message = str(exception)
            if dialog is not None:
                dialog.append_log("ERROR: " + message)
            self._show_error(message)
            return
        if not result or result.get("cancelled"):
            if dialog is not None:
                dialog.append_log("Acquisition cancelled; completed files were kept.")
            return

        outputs = [Path(path) for path in result["outputs"]]
        if dialog is not None:
            dialog.append_log(f"Completed: {len(outputs)} output file(s).")
        loaded = 0
        if result.get("load_results"):
            for path in (Path(path) for path in result.get("load_paths", [])):
                loaded += self._load_spatial_file(path)
            if dialog is not None:
                dialog.append_log(f"Loaded {loaded} QGIS layer(s).")
        self.iface.messageBar().pushMessage(
            "IdrAgra Input Gatherer",
            "Input gathering completed.",
            level=MESSAGE_LEVEL.Success,
            duration=6,
        )

    def _append_task_status(self, message):
        dialog = self.dialog
        if dialog is not None:
            dialog.append_log(str(message))

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
        details = _qgis_provider_registry().querySublayers(vector_uri)
        options = QgsProviderSublayerDetails.LayerOptions(_qgis_project().transformContext())
        loaded = 0
        for detail in details:
            layer = detail.toLayer(options)
            if layer is not None and layer.isValid():
                layer.setName(f"{path.stem} — {detail.name()}")
                self._style_normalized_soil_layer(layer, path)
                self._style_normalized_landuse_layer(layer, path)
                self._style_simulation_cells_layer(layer, path)
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
            dialog = self.dialog
            if dialog is not None:
                provider_error = layer.error().summary() or "OGR returned no details"
                dialog.append_log(f"WARNING: QGIS could not load vector output {path}: {provider_error}")
        return loaded

    # Categorize normalized soil profiles using stable colors.
    @staticmethod
    def _style_normalized_soil_layer(layer, path):
        if Path(path).name.lower() != NORMALIZED_SOIL_NAME:
            return
        field_index = layer.fields().indexFromName("profile_id")
        if field_index < 0:
            return
        values = sorted(layer.uniqueValues(field_index), key=lambda value: int(value))
        categories = IdrAgraGatherPlugin._categories_for_values(layer, values)
        if not categories:
            return
        layer.setRenderer(QgsCategorizedSymbolRenderer("profile_id", categories))
        layer.triggerRepaint()

    # Categorize normalized CORINE polygons by their readable class.
    @staticmethod
    def _style_normalized_landuse_layer(layer, path):
        if Path(path).name.lower() != NORMALIZED_LANDUSE_NAME:
            return
        field_index = layer.fields().indexFromName("landuse")
        if field_index < 0:
            return
        values = sorted(layer.uniqueValues(field_index), key=str)
        categories = IdrAgraGatherPlugin._categories_for_values(layer, values)
        if not categories:
            return
        layer.setRenderer(QgsCategorizedSymbolRenderer("landuse", categories))
        layer.triggerRepaint()

    # Categorize canonical cells by their allocated IdrAgra land-use ID.
    @staticmethod
    def _style_simulation_cells_layer(layer, path):
        if Path(path).name.lower() != CELLS_NAME:
            return
        field_index = layer.fields().indexFromName("landuse_id")
        if field_index < 0:
            return
        values = sorted(layer.uniqueValues(field_index), key=lambda value: int(value))
        categories = IdrAgraGatherPlugin._categories_for_values(layer, values)
        if not categories:
            return
        layer.setRenderer(QgsCategorizedSymbolRenderer("landuse_id", categories))
        layer.triggerRepaint()

    # Build QGIS 3/4-compatible categories with deterministic colors.
    @staticmethod
    def _categories_for_values(layer, values):
        base_symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        if base_symbol is None:
            return []
        categories = []
        for value in values:
            digest = hashlib.sha256(str(value).encode("utf-8")).digest()
            hue = int.from_bytes(digest[:2], "big") % 360
            saturation = 150 + digest[2] % 71
            brightness = 185 + digest[3] % 51
            symbol = base_symbol.clone()
            symbol.setColor(QColor.fromHsv(hue, saturation, brightness))
            categories.append(QgsRendererCategory(value, symbol, str(value)))
        return categories

    def _load_netcdf_sublayers(self, path):
        details = _qgis_provider_registry().querySublayers(str(path))
        options = QgsProviderSublayerDetails.LayerOptions(_qgis_project().transformContext())
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

    # Assign or repair WGS 84 georeferencing from NetCDF coordinate axes.
    def _georeference_netcdf_raster(self, layer, path, variable_name):
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

        repaired = self._create_georeferenced_preview(layer, path, variable_name, bounds, wgs84)
        if repaired is not None:
            dialog = self.dialog
            if dialog is not None:
                dialog.append_log(f"Display: repaired missing NetCDF georeferencing for {path.name}.")
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
                intervals = [right - left for left, right in zip(ordered, ordered[1:])]
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
        return all(abs(found - expected) <= tolerance for found, expected in zip(actual, (west, north, east, south)))

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

    # Give raw numeric rasters a visible, deterministic single-band style.
    @staticmethod
    def _style_raster_layer(layer):
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

    # Keep the AOI useful as a boundary without hiding acquired rasters.
    @staticmethod
    def _style_aoi_layer(layer, path):
        if Path(path).stem.lower() != "aoi":
            return
        symbol = QgsFillSymbol.createSimple(
            {
                "color": "0,0,0,0",
                "outline_color": "35,139,69,255",
                "outline_width": "0.8",
            }
        )
        renderer = layer.renderer()
        if renderer is None:
            return
        renderer.setSymbol(symbol)
        layer.triggerRepaint()

    @staticmethod
    def _add_layer(layer, path):
        project = _qgis_project()
        root = project.layerTreeRoot()
        if root is None:
            raise RuntimeError("The QGIS project has no layer tree.")
        group = root.findGroup(LAYER_GROUP)
        if group is None:
            group = root.insertGroup(0, LAYER_GROUP)
            if group is None:
                raise RuntimeError("QGIS could not create the input layer group.")
        elif root.children() and root.children()[0] is not group:
            # Keep gathered inputs above basemaps and pre-existing project layers.
            parent = group.parent()
            if parent is not None:
                parent.takeChild(group)
            root.insertChildNode(0, group)
        target_group = group
        subgroup_name, is_raw = IdrAgraGatherPlugin._subgroup_for_path(path)
        if subgroup_name:
            target_group = group.findGroup(subgroup_name)
            if target_group is None:
                target_group = group.insertGroup(0, subgroup_name)
                if target_group is None:
                    raise RuntimeError("QGIS could not create an input subgroup.")
                target_group.setExpanded(not is_raw)
            elif group.children() and group.children()[0] is not target_group:
                parent = target_group.parent()
                if parent is not None:
                    parent.takeChild(target_group)
                group.insertChildNode(0, target_group)
            # Keep raw groups collapsed, but visible, so acquisition results
            # appear on the map without another manual toggle. This also repairs
            # a group left unchecked by an earlier plugin version.
            target_group.setItemVisibilityChecked(True)
        project.addMapLayer(layer, False)
        target_group.insertLayer(0, layer)

    # Remove loaded layers backed by *path* and return the number removed.
    @staticmethod
    def _remove_project_layers_for_path(path):
        target = os.path.normcase(os.path.abspath(str(path)))
        project = _qgis_project()
        layer_ids = []
        for layer_id, layer in project.mapLayers().items():
            # OGR sources append options such as ``|layername=...``.
            source_path = str(layer.source()).split("|", 1)[0]
            if os.path.normcase(os.path.abspath(source_path)) == target:
                layer_ids.append(layer_id)
        if layer_ids:
            project.removeMapLayers(layer_ids)
        return len(layer_ids)

    @staticmethod
    def _subgroup_for_path(path):
        parts = [part.lower() for part in Path(path).parts]
        if "raw" in parts:
            index = parts.index("raw")
            if index + 1 < len(parts):
                return f"{parts[index + 1]}_raw", True
        parent = Path(path).parent.name.lower()
        if parent in {"weather", "soil", "landuse", "topography", "cells"}:
            return parent, False
        return None, False

    def _show_error(self, message):
        QMessageBox.critical(self.iface.mainWindow(), "IdrAgra Input Gatherer", message)

    def _dialog_closed(self):
        if self.canvas.mapTool() is self.map_tool:
            self.iface.actionPan().trigger()


# Return the active QGIS project or fail clearly during incomplete initialization.
def _qgis_project():
    project = QgsProject.instance()
    if project is None:
        raise RuntimeError("No active QGIS project is available.")
    return project


# Return the QGIS provider registry or fail clearly during incomplete initialization.
def _qgis_provider_registry():
    registry = QgsProviderRegistry.instance()
    if registry is None:
        raise RuntimeError("The QGIS provider registry is unavailable.")
    return registry


# Return the QGIS task manager or fail clearly during incomplete initialization.
def _qgis_task_manager():
    manager = QgsApplication.taskManager()
    if manager is None:
        raise RuntimeError("The QGIS task manager is unavailable.")
    return manager
