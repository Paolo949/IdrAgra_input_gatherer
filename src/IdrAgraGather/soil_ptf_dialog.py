"""QGIS dialog for deriving soil hydraulic properties with PTFs."""

from pathlib import Path

from qgis.PyQt.QtCore import pyqtSignal  # pyright: ignore[reportAttributeAccessIssue]
from qgis.PyQt.QtWidgets import (  # pyright: ignore[reportAttributeAccessIssue]
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .core.soil_ptf import (
    INPUT_FIELD_INFO,
    OUTPUT_FIELD_INFO,
    rosetta_field_groups,
)


STANDARD_BUTTON = getattr(QDialogButtonBox, "StandardButton", QDialogButtonBox)
BUTTON_ROLE = getattr(QDialogButtonBox, "ButtonRole", QDialogButtonBox)
RESIZE_MODE = getattr(QHeaderView, "ResizeMode", QHeaderView)
EDIT_TRIGGER = getattr(QAbstractItemView, "EditTrigger", QAbstractItemView)
SELECTION_MODE = getattr(QAbstractItemView, "SelectionMode", QAbstractItemView)


class SoilPtfDialog(QDialog):
    runRequested = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Derive IdrAgra soil hydraulic properties")
        self.resize(940, 690)
        self._running = False

        layout = QVBoxLayout(self)
        introduction = QLabel(
            "Apply a documented pedotransfer function to the normalized soil "
            "profiles. Results are stored separately and keyed by soil profile and "
            "horizon. Running a PTF replaces the previous hydraulic result."
        )
        introduction.setWordWrap(True)
        layout.addWidget(introduction)
        layout.addWidget(self._build_workspace_group())
        layout.addWidget(self._build_method_group())

        field_row = QHBoxLayout()
        self.required_table = self._field_panel(field_row, "Required inputs", "#d7eafa")
        self.generated_table = self._field_panel(field_row, "Generated outputs", "#d9f2df")
        self.unused_table = self._field_panel(field_row, "Available but unused", "#e6e6e6")
        layout.addLayout(field_row, 1)

        self.method_note = QLabel()
        self.method_note.setWordWrap(True)
        layout.addWidget(self.method_note)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(115)
        self.log.setPlaceholderText("PTF messages appear here.")
        layout.addWidget(self.log)

        buttons = QDialogButtonBox(STANDARD_BUTTON.Close)
        self.run_button = QPushButton("Run Rosetta")
        buttons.addButton(self.run_button, BUTTON_ROLE.AcceptRole)
        self.run_button.clicked.connect(self._emit_request)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        self.use_bulk_density.toggled.connect(self._update_method_view)
        self._update_method_view()

    def _build_workspace_group(self):
        group = QGroupBox("Workspace")
        layout = QFormLayout(group)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        self.workspace_edit = QLineEdit()
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_workspace)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_status)
        row_layout.addWidget(self.workspace_edit, 1)
        row_layout.addWidget(browse)
        row_layout.addWidget(refresh)
        self.input_status = QLabel("Choose a gathered-input workspace.")
        self.input_status.setWordWrap(True)
        layout.addRow("Working folder", row)
        layout.addRow("Normalized source", self.input_status)
        return group

    def _build_method_group(self):
        group = QGroupBox("Pedotransfer method")
        layout = QFormLayout(group)
        self.method_combo = QComboBox()
        self.method_combo.addItem("Rosetta 3", "rosetta3")
        self.use_bulk_density = QCheckBox("Use bulk density (H3; recommended when density is reliable)")
        self.use_bulk_density.setChecked(True)
        layout.addRow("Method", self.method_combo)
        layout.addRow("Hierarchy", self.use_bulk_density)
        return group

    @staticmethod
    def _field_panel(parent_layout, title, color):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QLabel(title)
        header.setStyleSheet(f"background-color: {color}; border: 1px solid #9a9a9a; font-weight: 600; padding: 5px;")
        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(("Field", "Unit"))
        table.horizontalHeader().setSectionResizeMode(0, RESIZE_MODE.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, RESIZE_MODE.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(EDIT_TRIGGER.NoEditTriggers)
        table.setSelectionMode(SELECTION_MODE.NoSelection)
        layout.addWidget(header)
        layout.addWidget(table, 1)
        parent_layout.addWidget(panel, 1)
        return table

    def _update_method_view(self):
        use_density = self.use_bulk_density.isChecked()
        groups = rosetta_field_groups(use_bulk_density=use_density)
        self._fill_table(self.required_table, groups.required, INPUT_FIELD_INFO)
        self._fill_table(self.generated_table, groups.generated, OUTPUT_FIELD_INFO)
        self._fill_table(self.unused_table, groups.unused, INPUT_FIELD_INFO)
        hierarchy = "H3" if use_density else "H2"
        self.method_note.setText(
            f"Rosetta 3 {hierarchy} directly predicts theta_res, theta_sat, van "
            "Genuchten alpha and n, and Ksat. vg_m is constrained to 1 - 1/n; "
            "theta_fc and theta_wp are evaluated from that mean curve at 33 and "
            "1500 kPa. No coarse-fragment correction is applied in this first version."
        )

    @staticmethod
    def _fill_table(table, fields, metadata):
        table.setRowCount(len(fields))
        for row, name in enumerate(fields):
            label, unit = metadata[name]
            field_item = QTableWidgetItem(f"{name}  —  {label}")
            unit_item = QTableWidgetItem(unit)
            table.setItem(row, 0, field_item)
            table.setItem(row, 1, unit_item)

    def _browse_workspace(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose gathered-input workspace", self.workspace_edit.text().strip())
        if folder:
            self.set_workspace(folder)

    def set_workspace(self, path):
        self.workspace_edit.setText(str(path or ""))
        self.refresh_status()

    def refresh_status(self):
        text = self.workspace_edit.text().strip()
        if not text:
            self.input_status.setText("Choose a gathered-input workspace.")
            return
        source = Path(text) / "soil" / "soil_profiles.gpkg"
        if not source.is_file():
            self.input_status.setText(f"Missing: {source}")
            return
        self.input_status.setText(f"Ready: {source}")

    def _emit_request(self):
        try:
            request = self.request()
        except Exception as exc:
            self.append_log("ERROR: " + str(exc))
            return
        self.runRequested.emit(request)

    def request(self):
        root = Path(self.workspace_edit.text().strip())
        if not root.is_dir():
            raise ValueError("Choose an existing gathered-input workspace.")
        source = root / "soil" / "soil_profiles.gpkg"
        if not source.is_file():
            raise ValueError(f"Normalized soil profiles were not found: {source}")
        return {
            "output": str(root),
            "method": self.method_combo.currentData(),
            "use_bulk_density": self.use_bulk_density.isChecked(),
        }

    def set_running(self, running):
        self._running = bool(running)
        self.run_button.setEnabled(not running)
        self.workspace_edit.setEnabled(not running)
        self.method_combo.setEnabled(not running)
        self.use_bulk_density.setEnabled(not running)

    def append_log(self, message):
        self.log.appendPlainText(str(message))
