"""QGIS dialog for the first IdrAgra v2 exporter contract."""

from pathlib import Path

from qgis.PyQt.QtCore import pyqtSignal  # pyright: ignore[reportAttributeAccessIssue]
from qgis.PyQt.QtWidgets import (  # pyright: ignore[reportAttributeAccessIssue]
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)


STANDARD_BUTTON = getattr(QDialogButtonBox, "StandardButton", QDialogButtonBox)
BUTTON_ROLE = getattr(QDialogButtonBox, "ButtonRole", QDialogButtonBox)


class V2ExportDialog(QDialog):
    runRequested = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export IdrAgra v2 inputs")
        self.resize(780, 670)
        layout = QVBoxLayout(self)
        introduction = QLabel(
            "Convert a regular-grid cell view, Rosetta soil hydraulics, normalized "
            "weather, and the land-use catalogue to IdrAgra v2 files. This first "
            "contract is static, rain-fed, and has capillary rise disabled."
        )
        introduction.setWordWrap(True)
        layout.addWidget(introduction)
        layout.addWidget(self._paths_group())
        layout.addWidget(self._settings_group())

        scope = QLabel(
            "Generated: domain, static soil use, slope, two-layer soil hydraulic "
            "grids, Curve Number soil group/condition, weather station series and "
            "IDW grids, CropCoef rotation files, provenance, and a v2 parameter "
            "template. A zero-method irrigation parser stub is included for Mode 0."
            "\n\nNot generated: phenology series, operational irrigation/network "
            "inputs, yearly land-use maps, water-table depth, or capillary-rise "
            "parameters. Crop parameter files are copied when their source files "
            "can be resolved."
        )
        scope.setWordWrap(True)
        scope.setStyleSheet("background: #f4f0dc; border: 1px solid #b8ae76; padding: 8px;")
        layout.addWidget(scope)
        self.status = QLabel("Choose a gathered-input workspace.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Exporter messages appear here.")
        layout.addWidget(self.log, 1)
        buttons = QDialogButtonBox(STANDARD_BUTTON.Close)
        self.export_button = QPushButton("Export v2 package")
        buttons.addButton(self.export_button, BUTTON_ROLE.AcceptRole)
        self.export_button.clicked.connect(self._emit_request)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

    def _paths_group(self):
        group = QGroupBox("Paths")
        layout = QFormLayout(group)
        self.workspace_edit, row = self._folder_row(self._browse_workspace)
        layout.addRow("Gathered workspace", row)
        self.destination_edit, row = self._folder_row(self._browse_destination)
        layout.addRow("Export folder", row)
        self.crop_folder_edit, row = self._folder_row(self._browse_crop_folder)
        layout.addRow("Crop parameter folder", row)
        return group

    @staticmethod
    def _folder_row(callback):
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit()
        browse = QPushButton("Browse...")
        browse.clicked.connect(callback)
        row_layout.addWidget(edit, 1)
        row_layout.addWidget(browse)
        return edit, row

    def _settings_group(self):
        group = QGroupBox("v2 model settings")
        layout = QFormLayout(group)
        self.evap_depth = QDoubleSpinBox(); self.evap_depth.setRange(0.01, 2.0); self.evap_depth.setDecimals(2); self.evap_depth.setValue(0.10); self.evap_depth.setSuffix(" m")
        self.root_depth = QDoubleSpinBox(); self.root_depth.setRange(0.01, 4.0); self.root_depth.setDecimals(2); self.root_depth.setValue(0.90); self.root_depth.setSuffix(" m")
        self.neighbors = QSpinBox(); self.neighbors.setRange(1, 10); self.neighbors.setValue(2)
        self.condition = QComboBox()
        self.condition.addItem("Fair (2)", 2); self.condition.addItem("Good (1)", 1); self.condition.addItem("Poor (3)", 3)
        layout.addRow("Evaporative-layer thickness", self.evap_depth)
        layout.addRow("Root-layer thickness", self.root_depth)
        layout.addRow("Nearest weather stations", self.neighbors)
        layout.addRow("Hydrologic condition", self.condition)
        note = QLabel("The two thicknesses are cumulative: defaults export 0-0.10 m and 0.10-1.00 m, matching the legacy IdrAgraTools aggregation.")
        note.setWordWrap(True); layout.addRow(note)
        return group

    def set_workspace(self, path):
        root = Path(path) if path else Path()
        self.workspace_edit.setText(str(path or ""))
        if path and not self.destination_edit.text().strip():
            self.destination_edit.setText(str(root / "exports" / "idragra_v2"))
        self.refresh_status()

    def refresh_status(self):
        root = Path(self.workspace_edit.text().strip())
        expected = (
            root / "cells" / "soil_id.tif", root / "cells" / "landuse_id.tif",
            root / "soil" / "soil_hydraulics.gpkg",
            root / "weather" / "weather_daily_points.gpkg",
        )
        missing = [path.name for path in expected if not path.is_file()]
        self.status.setText(
            "Ready for v2 export." if not missing else "Missing required workspace outputs: " + ", ".join(missing)
        )

    def _choose(self, edit, title):
        folder = QFileDialog.getExistingDirectory(self, title, edit.text().strip() or self.workspace_edit.text().strip())
        if folder: edit.setText(folder)

    def _browse_workspace(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose gathered-input workspace", self.workspace_edit.text().strip())
        if folder: self.set_workspace(folder)

    def _browse_destination(self): self._choose(self.destination_edit, "Choose v2 export folder")
    def _browse_crop_folder(self): self._choose(self.crop_folder_edit, "Choose crop parameter folder")

    def request(self):
        root = Path(self.workspace_edit.text().strip())
        if not root.is_dir(): raise ValueError("Choose an existing gathered-input workspace.")
        destination = Path(self.destination_edit.text().strip())
        if not destination.name: raise ValueError("Choose a v2 export folder.")
        return {
            "workspace": str(root), "destination": str(destination),
            "evap_layer_m": self.evap_depth.value(), "root_layer_m": self.root_depth.value(),
            "weather_neighbors": self.neighbors.value(), "hydrologic_condition": self.condition.currentData(),
            "crop_parameter_folder": self.crop_folder_edit.text().strip() or None,
        }

    def _emit_request(self):
        try: request = self.request()
        except Exception as exc:
            self.append_log("ERROR: " + str(exc)); return
        self.runRequested.emit(request)

    def set_running(self, running):
        self.export_button.setEnabled(not running)
        for widget in (self.workspace_edit, self.destination_edit, self.crop_folder_edit, self.evap_depth, self.root_depth, self.neighbors, self.condition):
            widget.setEnabled(not running)

    def append_log(self, message): self.log.appendPlainText(str(message))
