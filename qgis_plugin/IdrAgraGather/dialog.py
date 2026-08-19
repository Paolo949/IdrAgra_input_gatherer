from pathlib import Path

from qgis.PyQt.QtCore import QDate, Qt, pyqtSignal # pyright: ignore[reportAttributeAccessIssue]
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog,       # pyright: ignore[reportAttributeAccessIssue]
                                 QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,           # pyright: ignore[reportAttributeAccessIssue]
                                 QPlainTextEdit, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget)# pyright: ignore[reportAttributeAccessIssue]


# PyQt6 scopes enums which PyQt5 also exposed directly on their classes.
WINDOW_TYPE = getattr(Qt, "WindowType", Qt)
STANDARD_BUTTON = getattr(QDialogButtonBox, "StandardButton", QDialogButtonBox)
TEXT_INTERACTION_FLAG = getattr(Qt, "TextInteractionFlag", Qt)

WEATHER_SOURCE_ERA5 = "era5"
WEATHER_SOURCE_EOBS = "eobs"
WEATHER_SOURCE_LOCAL = "local"

SOIL_SOURCE_SOILGRIDS = "soilgrids"
SOIL_SOURCE_LOCAL = "local"

SOIL_DEPTHS_ALL = "all"
SOIL_DEPTHS_TOPSOIL = "topsoil"


class FilePicker(QWidget):
    def __init__(self, file_filter="All files (*.*)", parent=None):
        super().__init__(parent)
        self.file_filter = file_filter
        self.line_edit = QLineEdit()
        self.button = QPushButton("Browse…")
        self.button.clicked.connect(self._browse)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.line_edit, 1)
        layout.addWidget(self.button)

    def path(self):
        return self.line_edit.text().strip()

    def set_path(self, path):
        self.line_edit.setText(str(path or ""))

    def _browse(self):
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Choose input file",
            self.path(),
            self.file_filter,
        )
        if filename:
            self.set_path(filename)


class AcquisitionDialog(QDialog):
    drawRequested = pyqtSignal()
    canvasExtentRequested = pyqtSignal()
    runRequested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent) # inherits the parent's window behaviour if specified

        # Creates the dialog window
        self._bbox = None
        self.setWindowTitle("IdrAgra input gatherer")
        self.resize(760, 780)
        self.setWindowFlags(self.windowFlags() | WINDOW_TYPE.WindowMinMaxButtonsHint)

        layout = QVBoxLayout(self)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        form = QWidget()
        form_layout = QVBoxLayout(form)

        # Introductory text at the top of the dialog
        intro = QLabel(
            "Acquire raw input data (weather, soil, landuse, topography) and transform it into reviewable IdrAgra-like datasets. "
            "Supports both local files and online sources accessible through web APIs."
        )
        intro.setWordWrap(True) # allows the intro text to wrap over multiple lines
        form_layout.addWidget(intro)

        # Creates each group of widgets
        form_layout.addWidget(self._build_aoi_group())
        form_layout.addWidget(self._build_output_group())
        form_layout.addWidget(self._build_weather_group())
        form_layout.addWidget(self._build_soil_group())
        form_layout.addWidget(self._build_other_group())
        form_layout.addStretch()
        scroll_area.setWidget(form)
        layout.addWidget(scroll_area, 1)

        # Creates the log area at the bottom of the dialog
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Acquisition messages appear here.")
        layout.addWidget(self.log, 1)

        # Adds the "close" button at the
        buttons = QDialogButtonBox(STANDARD_BUTTON.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        # Sets the refresh policy of the "status" labels for specific user actions
        self.weather_source_selector.currentIndexChanged.connect(self._update_weather_file_state)
        self.soil_source_selector.currentIndexChanged.connect(self._update_soil_file_state)
        self.output_folder.editingFinished.connect(self.refresh_status)
        self._update_weather_file_state()
        self._update_soil_file_state()

    def _build_aoi_group(self):
        group = QGroupBox("1. Study area")
        layout = QGridLayout(group)
        self.aoi_text = QLineEdit()
        self.aoi_text.setReadOnly(True)
        self.aoi_text.setPlaceholderText("No bounding box selected")
        draw_button = QPushButton("Draw box on map")
        extent_button = QPushButton("Use canvas extent")
        draw_button.clicked.connect(self.drawRequested)
        extent_button.clicked.connect(self.canvasExtentRequested)
        layout.addWidget(QLabel("EPSG:4326 bbox"), 0, 0)
        layout.addWidget(self.aoi_text, 0, 1, 1, 2)
        layout.addWidget(draw_button, 1, 1)
        layout.addWidget(extent_button, 1, 2)
        return group

    def _build_weather_group(self):
        group = QGroupBox("Weather")
        layout = QGridLayout(group)
        self.weather_status = QLabel("Raw: not found\nNormalized: not found")
        self.weather_status.setStyleSheet("font-weight: bold;")
        self.weather_status.setWordWrap(True)
        self.weather_status.setTextInteractionFlags(TEXT_INTERACTION_FLAG.TextSelectableByMouse)
        self.weather_source_selector = QComboBox()
        self.weather_source_selector.addItem("ERA5-Land", WEATHER_SOURCE_ERA5)
        self.weather_source_selector.addItem("E-OBS", WEATHER_SOURCE_EOBS)
        self.weather_source_selector.addItem("Local file", WEATHER_SOURCE_LOCAL)
        self.weather_file = FilePicker("Weather data (*.nc *.csv *.txt);;All files (*.*)")
        today = QDate.currentDate()
        self.start_date = QDateEdit(QDate(today.year(), 1, 1))
        self.end_date = QDateEdit(today)
        for widget in (self.start_date, self.end_date):
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd")
        self.timezone_combo = QComboBox()
        self.timezone_combo.addItem("Europe/Rome (legal time included)", "Europe/Rome")
        self.timezone_combo.addItem("UTC", "UTC")
        self.weather_options = QStackedWidget()
        era5_page = QWidget()
        era5_layout = QFormLayout(era5_page)
        period_row = QWidget()
        period_layout = QHBoxLayout(period_row)
        period_layout.setContentsMargins(0, 0, 0, 0)
        period_layout.addWidget(self.start_date)
        period_layout.addWidget(self.end_date)
        era5_layout.addRow("Period", period_row)
        era5_layout.addRow("Daily timezone", self.timezone_combo)
        eobs_page = QWidget()
        eobs_layout = QVBoxLayout(eobs_page)
        eobs_note = QLabel("E-OBS acquisition is planned but not implemented yet.")
        eobs_note.setWordWrap(True)
        eobs_layout.addWidget(eobs_note)
        local_page = QWidget()
        local_layout = QFormLayout(local_page)
        local_layout.addRow("Input file", self.weather_file)
        self.weather_options.addWidget(era5_page)
        self.weather_options.addWidget(eobs_page)
        self.weather_options.addWidget(local_page)
        self.weather_acquire_button = QPushButton("Acquire raw")
        self.weather_transform_button = QPushButton("Transform existing")
        self.weather_both_button = QPushButton("Acquire + transform")
        self.weather_acquire_button.clicked.connect(lambda: self.runRequested.emit("weather-acquire"))
        self.weather_transform_button.clicked.connect(lambda: self.runRequested.emit("weather-transform"))
        self.weather_both_button.clicked.connect(lambda: self.runRequested.emit("weather-both"))
        layout.addWidget(QLabel("Status"), 0, 0)
        layout.addWidget(self.weather_status, 0, 1, 1, 3)
        layout.addWidget(QLabel("Source"), 1, 0)
        layout.addWidget(self.weather_source_selector, 1, 1, 1, 3)
        layout.addWidget(self.weather_options, 2, 1, 1, 3)
        layout.addWidget(self.weather_acquire_button, 3, 1)
        layout.addWidget(self.weather_transform_button, 3, 2)
        layout.addWidget(self.weather_both_button, 3, 3)
        return group

    def _build_soil_group(self):
        group = QGroupBox("Soil")
        layout = QGridLayout(group)
        self.soil_status = QLabel("Raw: not found\nNormalized: not implemented")
        self.soil_status.setStyleSheet("font-weight: bold;")
        self.soil_status.setWordWrap(True)
        self.soil_status.setTextInteractionFlags(TEXT_INTERACTION_FLAG.TextSelectableByMouse)
        self.soil_source_selector = QComboBox()
        self.soil_source_selector.addItem("ISRIC SoilGrids", SOIL_SOURCE_SOILGRIDS)
        self.soil_source_selector.addItem("Local file", SOIL_SOURCE_LOCAL)
        self.soil_depths = QComboBox()
        self.soil_depths.addItem("All six standard depths (0–200 cm)", SOIL_DEPTHS_ALL)
        self.soil_depths.addItem("Topsoil only (0–30 cm)", SOIL_DEPTHS_TOPSOIL)
        self.soil_file = FilePicker("Soil data (*.gpkg *.shp *.tif *.tiff);;All files (*.*)")
        self.soil_options = QStackedWidget()
        soilgrids_page = QWidget()
        soilgrids_layout = QFormLayout(soilgrids_page)
        soilgrids_layout.addRow("Depths", self.soil_depths)
        local_soil_page = QWidget()
        local_soil_layout = QFormLayout(local_soil_page)
        local_soil_layout.addRow("Input file", self.soil_file)
        self.soil_options.addWidget(soilgrids_page)
        self.soil_options.addWidget(local_soil_page)
        self.soil_acquire_button = QPushButton("Acquire raw")
        self.soil_transform_button = QPushButton("Transform existing")
        self.soil_both_button = QPushButton("Acquire + transform")
        self.soil_acquire_button.clicked.connect(lambda: self.runRequested.emit("soil-acquire"))
        for button in (self.soil_transform_button, self.soil_both_button):
            button.setEnabled(False)
            button.setToolTip("Soil unit conversion and PTF transformation are the next milestone.")
        layout.addWidget(QLabel("Status"), 0, 0)
        layout.addWidget(self.soil_status, 0, 1, 1, 3)
        layout.addWidget(QLabel("Source"), 1, 0)
        layout.addWidget(self.soil_source_selector, 1, 1, 1, 3)
        layout.addWidget(self.soil_options, 2, 1, 1, 3)
        layout.addWidget(self.soil_acquire_button, 3, 1)
        layout.addWidget(self.soil_transform_button, 3, 2)
        layout.addWidget(self.soil_both_button, 3, 3)
        note = QLabel("Transform will later apply unit conversion and a selected PTF.")
        note.setWordWrap(True)
        layout.addWidget(note, 4, 1, 1, 3)
        return group

    def _build_other_group(self):
        group = QGroupBox("Other local inputs — stage only")
        layout = QFormLayout(group)
        self.landuse_file = FilePicker("Land-use data (*.gpkg *.shp *.tif *.tiff);;All files (*.*)")
        self.topography_file = FilePicker("Elevation data (*.tif *.tiff *.vrt);;All files (*.*)")
        layout.addRow("Land use", self.landuse_file)
        layout.addRow("DEM", self.topography_file)
        self.other_stage_button = QPushButton("Stage selected files")
        self.other_stage_button.clicked.connect(
            lambda: self.runRequested.emit("other-stage")
        )
        layout.addRow(self.other_stage_button)
        return group

    def _build_output_group(self):
        group = QGroupBox("Workspace")
        layout = QGridLayout(group)
        self.output_folder = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_output)
        refresh = QPushButton("Refresh detected files")
        refresh.clicked.connect(self.refresh_status)
        self.load_results = QCheckBox("Add outputs from completed actions to QGIS")
        self.load_results.setChecked(True)
        self.load_results.setToolTip(
            "Adds only the outputs returned by the action that just completed. "
            "It does not scan and load every file already in the workspace."
        )
        layout.addWidget(QLabel("Working folder"), 0, 0)
        layout.addWidget(self.output_folder, 0, 1)
        layout.addWidget(browse, 0, 2)
        layout.addWidget(refresh, 1, 1)
        layout.addWidget(self.load_results, 2, 1, 1, 2)
        return group

    def set_bbox(self, west, south, east, north):
        self._bbox = [float(west), float(south), float(east), float(north)]
        self.aoi_text.setText(
            f"{west:.6f}, {south:.6f}, {east:.6f}, {north:.6f}"
        )

    def set_output_folder(self, path):
        if path:
            self.output_folder.setText(str(path))
            self.refresh_status()

    def request(self, action):
        if self._bbox is None:
            raise ValueError("Draw a study-area box or use the current canvas extent.")
        output = self.output_folder.text().strip()
        if not output:
            raise ValueError("Choose a working output folder.")
        if self.start_date.date() > self.end_date.date():
            raise ValueError("The start date is after the end date.")

        selected_weather = self.weather_source_selector.currentData()
        weather_source = None
        normalize_weather = False
        if action == "weather-acquire":
            weather_source = "era5-download" if selected_weather == WEATHER_SOURCE_ERA5 else selected_weather
        elif action == "weather-transform" and selected_weather == WEATHER_SOURCE_ERA5:
            weather_source = "era5-normalize"
            normalize_weather = True
        elif action == "weather-both":
            weather_source = "era5-download" if selected_weather == WEATHER_SOURCE_ERA5 else selected_weather
            normalize_weather = selected_weather == WEATHER_SOURCE_ERA5
        if weather_source == WEATHER_SOURCE_LOCAL and not self.weather_file.path():
            raise ValueError("Choose a local weather file or another weather source.")
        soil_source = self.soil_source_selector.currentData() if action == "soil-acquire" else None
        if soil_source == SOIL_SOURCE_LOCAL and not self.soil_file.path():
            raise ValueError("Choose a local soil file or another soil source.")
        local_files = {
            "soil": self.soil_file.path() if soil_source == SOIL_SOURCE_LOCAL else "",
            "landuse": self.landuse_file.path() if action == "other-stage" else "",
            "topography": self.topography_file.path() if action == "other-stage" else "",
        }
        if weather_source == WEATHER_SOURCE_LOCAL:
            local_files["weather"] = self.weather_file.path()
        for category, path in local_files.items():
            if path and not Path(path).is_file():
                raise ValueError(f"The {category} input file does not exist: {path}")

        return {
            "bbox": list(self._bbox),
            "start": self.start_date.date().toString("yyyy-MM-dd"),
            "end": self.end_date.date().toString("yyyy-MM-dd"),
            "weather_source": weather_source,
            "normalize_weather": normalize_weather,
            "timezone": self.timezone_combo.currentData(),
            "soil_source": soil_source,
            "soil_depths": self.soil_depths.currentData(),
            "local_files": local_files,
            "output": output,
            "load_results": bool(self.load_results.isChecked()),
            "action": action,
        }

    def append_log(self, message):
        self.log.appendPlainText(str(message))

    def set_running(self, running):
        self.weather_source_selector.setEnabled(not running)
        self.soil_source_selector.setEnabled(not running)
        for button in (
            self.weather_acquire_button,
            self.weather_transform_button,
            self.weather_both_button,
            self.soil_acquire_button,
            self.other_stage_button,
        ):
            button.setEnabled(not running)
        if not running:
            self._update_weather_file_state()

    def _browse_output(self):
        directory = QFileDialog.getExistingDirectory(
            self,
            "Choose working folder",
            self.output_folder.text().strip(),
        )
        if directory:
            self.output_folder.setText(directory)
            self.refresh_status()

    def _update_weather_file_state(self):
        source = self.weather_source_selector.currentData()
        self.weather_options.setCurrentIndex(self.weather_source_selector.currentIndex())
        is_era5 = source == WEATHER_SOURCE_ERA5
        can_acquire = source in {WEATHER_SOURCE_ERA5, WEATHER_SOURCE_LOCAL}
        self.weather_acquire_button.setEnabled(can_acquire)
        self.weather_transform_button.setEnabled(is_era5)
        self.weather_both_button.setEnabled(is_era5)
        self.refresh_status()

    def _update_soil_file_state(self):
        source = self.soil_source_selector.currentData()
        self.soil_options.setCurrentIndex(self.soil_source_selector.currentIndex())
        self.soil_acquire_button.setEnabled(source in {SOIL_SOURCE_SOILGRIDS, SOIL_SOURCE_LOCAL})
        self.refresh_status()

    def refresh_status(self):
        root = Path(self.output_folder.text().strip())
        weather_provider = {
            WEATHER_SOURCE_ERA5: "era5_land",
            WEATHER_SOURCE_EOBS: "eobs",
            WEATHER_SOURCE_LOCAL: "local",
        }[self.weather_source_selector.currentData()]
        weather_raw = root / "raw" / "weather" / weather_provider
        weather_files = [path for path in weather_raw.iterdir() if path.is_file()] if weather_raw.is_dir() else []
        weather_count = len(weather_files)
        weather_normalized = root / "weather" / "weather_daily_points.gpkg"
        if weather_count == 1:
            raw_weather_text = f"Raw: {weather_files[0]}"
        elif weather_count:
            raw_weather_text = f"Raw: {weather_count} files in {weather_raw}"
        else:
            raw_weather_text = f"Raw: not found in {weather_raw}"
        normalized_weather_text = (
            f"Normalized: {weather_normalized}"
            if weather_normalized.is_file()
            else f"Normalized: not found at {weather_normalized}"
        )
        self.weather_status.setText(raw_weather_text + "\n" + normalized_weather_text)

        soil_provider = {
            SOIL_SOURCE_SOILGRIDS: "soilgrids",
            SOIL_SOURCE_LOCAL: "local",
        }[self.soil_source_selector.currentData()]
        soil_raw = root / "raw" / "soil" / soil_provider
        soil_files = [path for path in soil_raw.iterdir() if path.is_file()] if soil_raw.is_dir() else []
        soil_count = len(soil_files)
        if soil_count == 1:
            raw_soil_text = f"Raw: {soil_files[0]}"
        elif soil_count:
            raw_soil_text = f"Raw: {soil_count} files in {soil_raw}"
        else:
            raw_soil_text = f"Raw: not found in {soil_raw}"
        self.soil_status.setText(raw_soil_text + "\nNormalized: not implemented")
