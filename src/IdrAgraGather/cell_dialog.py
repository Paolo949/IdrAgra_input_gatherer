"""QGIS dialog for configuring and building a simulation-cell view."""

from pathlib import Path

from qgis.PyQt.QtCore import Qt, pyqtSignal  # pyright: ignore[reportAttributeAccessIssue]
from qgis.PyQt.QtWidgets import (  # pyright: ignore[reportAttributeAccessIssue]
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsVectorLayer

from .core.landuses import (
    DEFAULT_CROPS,
    DEFAULT_LANDUSES,
    CropDefinition,
    LandUseAllocation,
    LandUseDefinition,
    parse_idragra_landuses,
    read_configuration,
    validate_allocations,
    validate_catalog,
)


STANDARD_BUTTON = getattr(QDialogButtonBox, "StandardButton", QDialogButtonBox)
SELECTION_BEHAVIOR = getattr(QAbstractItemView, "SelectionBehavior", QAbstractItemView)
RESIZE_MODE = getattr(QHeaderView, "ResizeMode", QHeaderView)
ITEM_FLAG = getattr(Qt, "ItemFlag", Qt)
MESSAGE_BUTTON = getattr(QMessageBox, "StandardButton", QMessageBox)


class CellBuilderDialog(QDialog):
    runRequested = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Build IdrAgra simulation cells")
        self.resize(920, 780)
        self._running = False

        layout = QVBoxLayout(self)
        introduction = QLabel(
            "Combine normalized soil, land use and topography into a version-neutral "
            "cell view. Crop files define reusable parameters; land uses define "
            "ordered annual rotations of zero, one or two crops."
        )
        introduction.setWordWrap(True)
        layout.addWidget(introduction)
        layout.addWidget(self._build_workspace_group())
        layout.addWidget(self._build_method_group())

        tabs = QTabWidget()
        tabs.addTab(self._scrollable(self._build_catalog_page()), "Crop rotations")
        tabs.addTab(self._scrollable(self._build_allocations_page()), "Class allocation")
        layout.addWidget(tabs, 1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(120)
        self.log.setPlaceholderText("Cell-builder messages appear here.")
        layout.addWidget(self.log)

        buttons = QDialogButtonBox(STANDARD_BUTTON.Close)
        self.build_button = QPushButton("Build cell view")
        buttons.addButton(self.build_button, getattr(QDialogButtonBox, "ButtonRole", QDialogButtonBox).AcceptRole)
        self.build_button.clicked.connect(self._emit_request)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self._set_catalog(DEFAULT_CROPS, DEFAULT_LANDUSES)
        self.mode_combo.currentIndexChanged.connect(self._update_mode)
        self._update_mode()

    @staticmethod
    def _scrollable(content):
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(content)
        return area

    def _build_workspace_group(self):
        group = QGroupBox("Workspace")
        layout = QFormLayout(group)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        self.workspace_edit = QLineEdit()
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_workspace)
        load = QPushButton("Load inputs and classes")
        load.clicked.connect(self.load_workspace)
        row_layout.addWidget(self.workspace_edit, 1)
        row_layout.addWidget(browse)
        row_layout.addWidget(load)
        self.input_status = QLabel("Choose a gathered-input workspace.")
        self.input_status.setWordWrap(True)
        layout.addRow("Working folder", row)
        layout.addRow("Detected inputs", self.input_status)
        return group

    def _build_method_group(self):
        group = QGroupBox("Cell geometry and topography")
        layout = QFormLayout(group)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Regular grid", "grid")
        self.mode_combo.addItem("Contiguous soil / land-use polygons", "vector")
        self.cell_width = QDoubleSpinBox()
        self.cell_width.setRange(1.0, 100000.0)
        self.cell_width.setDecimals(1)
        self.cell_width.setValue(250.0)
        self.cell_width.setSuffix(" m")
        self.grid_boundary = QComboBox()
        self.grid_boundary.addItem("Only squares fully inside the AOI", "inside")
        self.grid_boundary.addItem("All full squares intersecting the AOI", "intersect")
        self.elevation_method = QComboBox()
        self.elevation_method.addItem("Median", "median")
        self.elevation_method.addItem("Dominant 1 m band", "dominant")
        self.elevation_method.addItem("Mean", "mean")
        self.elevation_method.addItem("Cell centroid", "centroid")
        self.slope_method = QComboBox()
        self.slope_method.addItem("Dominant 1% band (IdrAgraTools-like)", "dominant")
        self.slope_method.addItem("Median", "median")
        self.slope_method.addItem("Mean", "mean")
        self.slope_method.addItem("Cell centroid", "centroid")
        self.vector_note = QLabel(
            "Vector mode dissolves contiguous soil/source-land-use combinations and "
            "samples topography bilinearly at each centroid. Percentage allocations "
            "use whole contiguous regions in this first version."
        )
        self.vector_note.setWordWrap(True)
        self.grid_note = QLabel(
            "Grid cells are always complete, equal-sized squares. Intersecting mode "
            "allows the simulated grid footprint to extend beyond the AOI; soil and "
            "land-use dominance still use only the overlapping portion."
        )
        self.grid_note.setWordWrap(True)
        layout.addRow("Mode", self.mode_combo)
        layout.addRow("Grid cell width", self.cell_width)
        layout.addRow("AOI boundary", self.grid_boundary)
        layout.addRow("Elevation", self.elevation_method)
        layout.addRow("Slope", self.slope_method)
        layout.addRow(self.grid_note)
        layout.addRow(self.vector_note)
        return group

    def _build_catalog_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        defaults = QPushButton("Restore starter catalogue")
        defaults.clicked.connect(lambda: self._set_catalog(DEFAULT_CROPS, DEFAULT_LANDUSES))
        import_button = QPushButton("Import soil_uses.txt...")
        import_button.clicked.connect(self._import_landuses)
        toolbar.addWidget(defaults)
        toolbar.addWidget(import_button)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        crop_group = QGroupBox("Crop parameter files")
        crop_layout = QVBoxLayout(crop_group)
        self.crop_table = QTableWidget(0, 3)
        self.crop_table.setHorizontalHeaderLabels(("Crop key", "Name", "Parameter file"))
        self.crop_table.horizontalHeader().setSectionResizeMode(RESIZE_MODE.Stretch)
        self.crop_table.setSelectionBehavior(SELECTION_BEHAVIOR.SelectRows)
        crop_layout.addWidget(self.crop_table)
        crop_buttons = QHBoxLayout()
        add_crop = QPushButton("Add crop")
        remove_crop = QPushButton("Remove selected")
        add_crop.clicked.connect(self._add_crop)
        remove_crop.clicked.connect(lambda: self._remove_selected(self.crop_table))
        crop_buttons.addWidget(add_crop)
        crop_buttons.addWidget(remove_crop)
        crop_buttons.addStretch()
        crop_layout.addLayout(crop_buttons)
        layout.addWidget(crop_group)

        landuse_group = QGroupBox("IdrAgra land uses / annual rotations")
        landuse_layout = QVBoxLayout(landuse_group)
        self.landuse_table = QTableWidget(0, 4)
        self.landuse_table.setHorizontalHeaderLabels(("ID", "Name", "Crop 1", "Crop 2"))
        self.landuse_table.horizontalHeader().setSectionResizeMode(RESIZE_MODE.Stretch)
        self.landuse_table.setSelectionBehavior(SELECTION_BEHAVIOR.SelectRows)
        landuse_layout.addWidget(self.landuse_table)
        landuse_buttons = QHBoxLayout()
        add_landuse = QPushButton("Add land use")
        remove_landuse = QPushButton("Remove selected")
        add_landuse.clicked.connect(self._add_landuse)
        remove_landuse.clicked.connect(lambda: self._remove_selected(self.landuse_table))
        landuse_buttons.addWidget(add_landuse)
        landuse_buttons.addWidget(remove_landuse)
        landuse_buttons.addStretch()
        landuse_layout.addLayout(landuse_buttons)
        layout.addWidget(landuse_group)
        return page

    def _build_allocations_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "Assign every normalized source class to one or more IdrAgra land uses. "
            "Rows for the same source class must total exactly 100%."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.allocation_table = QTableWidget(0, 3)
        self.allocation_table.setHorizontalHeaderLabels(("Normalized source class", "IdrAgra land-use ID", "Share (%)"))
        self.allocation_table.horizontalHeader().setSectionResizeMode(0, RESIZE_MODE.Stretch)
        self.allocation_table.horizontalHeader().setSectionResizeMode(1, RESIZE_MODE.ResizeToContents)
        self.allocation_table.horizontalHeader().setSectionResizeMode(2, RESIZE_MODE.ResizeToContents)
        self.allocation_table.setSelectionBehavior(SELECTION_BEHAVIOR.SelectRows)
        layout.addWidget(self.allocation_table)
        toolbar = QHBoxLayout()
        split = QPushButton("Split selected allocation")
        remove = QPushButton("Remove selected")
        refresh = QPushButton("Refresh land-use choices")
        split.clicked.connect(self._split_allocation)
        remove.clicked.connect(lambda: self._remove_selected(self.allocation_table))
        refresh.clicked.connect(self._refresh_allocation_choices)
        toolbar.addWidget(split)
        toolbar.addWidget(remove)
        toolbar.addWidget(refresh)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        return page

    def set_workspace(self, path):
        self.workspace_edit.setText(str(path or ""))
        if path:
            self.load_workspace()

    def set_running(self, running):
        self._running = bool(running)
        self.build_button.setEnabled(not running)
        self.workspace_edit.setEnabled(not running)

    def _emit_request(self):
        try:
            request = self.request()
        except Exception as exc:
            QMessageBox.warning(self, "Cannot build cell view", str(exc))
            return
        self.runRequested.emit(request)

    def request(self):
        root = Path(self.workspace_edit.text().strip())
        if not root.is_dir():
            raise ValueError("Choose an existing gathered-input workspace.")
        crops, landuses = self._catalog()
        allocations = self._allocations()
        validate_catalog(crops, landuses)
        validate_allocations(allocations, landuses)
        return {
            "output": str(root),
            "mode": self.mode_combo.currentData(),
            "cell_width_m": float(self.cell_width.value()),
            "grid_boundary_policy": self.grid_boundary.currentData(),
            "elevation_method": self.elevation_method.currentData(),
            "slope_method": self.slope_method.currentData(),
            "crops": crops,
            "landuses": landuses,
            "allocations": allocations,
        }

    def _browse_workspace(self):
        directory = QFileDialog.getExistingDirectory(self, "Choose gathered-input workspace", self.workspace_edit.text().strip())
        if directory:
            self.workspace_edit.setText(directory)
            self.load_workspace()

    def load_workspace(self):
        root = Path(self.workspace_edit.text().strip())
        required = (
            root / "soil" / "soil_profiles.gpkg",
            root / "landuse" / "landuse.shp",
            root / "topography" / "elevation_m_asl.tif",
            root / "topography" / "slope_pct.tif",
        )
        missing = [path for path in required if not path.is_file()]
        if missing:
            self.input_status.setText("Missing: " + ", ".join(str(path.relative_to(root)) for path in missing))
            return
        self.input_status.setText("Ready: normalized soil, land use, elevation and slope found.")
        configuration = root / "cells" / "landuse_configuration.json"
        saved_allocations = []
        if configuration.is_file():
            try:
                crops, landuses, saved_allocations = read_configuration(configuration)
                self._set_catalog(crops, landuses)
                self.append_log(f"Loaded saved configuration from {configuration}.")
            except Exception as exc:
                self.append_log(f"WARNING: could not load saved configuration: {exc}")
        layer = QgsVectorLayer(str(required[1]), "normalized land use", "ogr")
        index = layer.fields().indexFromName("landuse") if layer.isValid() else -1
        if index < 0:
            self.append_log("ERROR: normalized land-use layer has no 'landuse' field.")
            return
        classes = sorted(str(value) for value in layer.uniqueValues(index))
        if saved_allocations:
            current = [item for item in saved_allocations if item.source_class in classes]
            configured = {item.source_class for item in current}
            current.extend(LandUseAllocation(source, -1, 100.0) for source in classes if source not in configured)
            self._set_allocations(current, allow_unselected=True)
        else:
            self._set_allocations(
                [LandUseAllocation(source, -1, 100.0) for source in classes],
                allow_unselected=True,
            )
        self.append_log(f"Detected {len(classes)} normalized land-use class(es).")

    def _update_mode(self):
        is_grid = self.mode_combo.currentData() == "grid"
        self.cell_width.setEnabled(is_grid)
        self.grid_boundary.setEnabled(is_grid)
        self.elevation_method.setEnabled(is_grid)
        self.slope_method.setEnabled(is_grid)
        self.grid_note.setVisible(is_grid)
        self.vector_note.setVisible(not is_grid)

    def _set_allocations(self, allocations, *, allow_unselected=False):
        self.allocation_table.setRowCount(0)
        for allocation in allocations:
            self._insert_allocation_row(
                allocation.source_class,
                allocation.landuse_id if allocation.landuse_id > 0 else None,
                allocation.share_pct,
            )

    def _allocations(self):
        allocations = []
        for row in range(self.allocation_table.rowCount()):
            source = self._item_text(self.allocation_table, row, 0)
            combo = self.allocation_table.cellWidget(row, 1)
            landuse_id = combo.currentData() if combo is not None else None
            share = self.allocation_table.cellWidget(row, 2)
            if landuse_id is None:
                raise ValueError(f"Choose an IdrAgra land use for {source!r}.")
            allocations.append(LandUseAllocation(source, int(landuse_id), float(share.value())))
        return allocations

    def _split_allocation(self):
        row = self.allocation_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Split allocation", "Select an allocation row first.")
            return
        source = self._item_text(self.allocation_table, row, 0)
        share_widget = self.allocation_table.cellWidget(row, 2)
        old_share = float(share_widget.value())
        first_share = old_share / 2.0
        share_widget.setValue(first_share)
        self._insert_allocation_row(source, None, old_share - first_share, row=row + 1)

    def _insert_allocation_row(self, source, landuse_id=None, share=100.0, *, row=None):
        if row is None:
            row = self.allocation_table.rowCount()
        self.allocation_table.insertRow(row)
        source_item = QTableWidgetItem(str(source))
        source_item.setFlags(source_item.flags() & ~ITEM_FLAG.ItemIsEditable)
        self.allocation_table.setItem(row, 0, source_item)
        combo = self._landuse_combo(landuse_id)
        self.allocation_table.setCellWidget(row, 1, combo)
        share_widget = QDoubleSpinBox()
        share_widget.setRange(0.01, 100.0)
        share_widget.setDecimals(2)
        share_widget.setValue(float(share))
        self.allocation_table.setCellWidget(row, 2, share_widget)

    def _add_crop(self):
        row = self.crop_table.rowCount()
        self.crop_table.insertRow(row)
        for column, value in enumerate((f"crop_{row + 1}", "New crop", "crop.tab")):
            self.crop_table.setItem(row, column, QTableWidgetItem(value))

    def _add_landuse(self):
        existing = []
        for row in range(self.landuse_table.rowCount()):
            try:
                existing.append(int(self._item_text(self.landuse_table, row, 0)))
            except ValueError:
                pass
        new_id = max(existing, default=0) + 1
        row = self.landuse_table.rowCount()
        self.landuse_table.insertRow(row)
        for column, value in enumerate((str(new_id), "New land use", "", "")):
            self.landuse_table.setItem(row, column, QTableWidgetItem(value))

    @staticmethod
    def _remove_selected(table):
        rows = sorted({index.row() for index in table.selectedIndexes()}, reverse=True)
        for row in rows:
            table.removeRow(row)

    def _import_landuses(self):
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Import IdrAgra rotation table",
            self.workspace_edit.text().strip(),
            "IdrAgra land uses (soil_uses.txt *.txt);;Text files (*.txt);;All files (*.*)",
        )
        if not filename:
            return
        try:
            crops, landuses = parse_idragra_landuses(filename)
        except Exception as exc:
            QMessageBox.warning(self, "Could not import land uses", str(exc))
            return
        if self.crop_table.rowCount() or self.landuse_table.rowCount():
            answer = QMessageBox.question(
                self,
                "Replace crop-rotation catalogue?",
                "Replace the current crop and land-use catalogue with the imported table?",
                MESSAGE_BUTTON.Yes | MESSAGE_BUTTON.No,
                MESSAGE_BUTTON.No,
            )
            if answer != MESSAGE_BUTTON.Yes:
                return
        self._set_catalog(crops, landuses)
        self.append_log(f"Imported {len(landuses)} land use(s) from {filename}.")

    def append_log(self, message):
        self.log.appendPlainText(str(message))

    def _set_catalog(self, crops, landuses):
        self.crop_table.setRowCount(0)
        for crop in crops:
            row = self.crop_table.rowCount()
            self.crop_table.insertRow(row)
            for column, value in enumerate((crop.crop_id, crop.name, crop.parameter_file)):
                self.crop_table.setItem(row, column, QTableWidgetItem(str(value)))
        self.landuse_table.setRowCount(0)
        for item in landuses:
            row = self.landuse_table.rowCount()
            self.landuse_table.insertRow(row)
            values = (item.landuse_id, item.name, item.crop1_id or "", item.crop2_id or "")
            for column, value in enumerate(values):
                self.landuse_table.setItem(row, column, QTableWidgetItem(str(value)))
        if hasattr(self, "allocation_table"):
            self._refresh_allocation_choices()

    def _refresh_allocation_choices(self):
        if not hasattr(self, "allocation_table"):
            return
        for row in range(self.allocation_table.rowCount()):
            old = self.allocation_table.cellWidget(row, 1)
            selected = old.currentData() if old is not None else None
            self.allocation_table.setCellWidget(row, 1, self._landuse_combo(selected))

    def _landuse_combo(self, selected=None):
        combo = QComboBox()
        combo.addItem("Choose...", None)
        try:
            _, landuses = self._catalog()
        except Exception:
            landuses = []
        for item in landuses:
            combo.addItem(f"{item.landuse_id} - {item.name}", item.landuse_id)
            if selected == item.landuse_id:
                combo.setCurrentIndex(combo.count() - 1)
        return combo

    def _catalog(self):
        crops = []
        for row in range(self.crop_table.rowCount()):
            values = [self._item_text(self.crop_table, row, column) for column in range(3)]
            crops.append(CropDefinition(*values))
        landuses = []
        for row in range(self.landuse_table.rowCount()):
            values = [self._item_text(self.landuse_table, row, column) for column in range(4)]
            try:
                landuse_id = int(values[0])
            except ValueError as exc:
                raise ValueError(f"Invalid land-use ID in catalogue row {row + 1}.") from exc
            landuses.append(LandUseDefinition(landuse_id, values[1], values[2] or None, values[3] or None))
        return crops, landuses

    @staticmethod
    def _item_text(table, row, column):
        item = table.item(row, column)
        return item.text().strip() if item is not None else ""
