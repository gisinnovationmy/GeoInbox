"""
GeoInbox - Versioning Dialog

Three-band layout for comparing and merging datasets:
- Top: Source Selection (live QGIS layer picker) + Comparison Setup
- Middle: Side-by-side Client/Host feature tables
- Bottom: Actions & Details (manual match, commit, undo/redo)
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter, QWidget,
    QLabel, QPushButton, QGroupBox, QTableWidget, QTableWidgetItem,
    QComboBox, QSizePolicy, QCheckBox, QHeaderView, QFrame, QSpinBox
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QTimer

from typing import Optional

try:
    from qgis.core import (
        QgsProject, QgsVectorLayer, QgsMapLayer,
        QgsLayerTree, QgsLayerTreeLayer
    )
    from qgis.utils import iface
    HAS_QGIS = True
except ImportError:
    HAS_QGIS = False


class VersioningDialog(QDialog):
    """
    Three-band dialog for data versioning and merging.

    Source combos stay in sync with the QGIS project at all times:
    layers added, removed, reordered, project opened or cleared are
    all handled via QgsProject signals that are connected on show and
    disconnected on close.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._client_layer: Optional[QgsVectorLayer] = None
        self._host_layer: Optional[QgsVectorLayer] = None
        self._selected_client_fid: Optional[str] = None
        self._selected_host_fid: Optional[str] = None
        self._project_signals_connected = False
        self._diff_summary = None

        self._setup_ui()

    # ------------------------------------------------------------------ #
    #  UI setup                                                            #
    # ------------------------------------------------------------------ #

    def _setup_ui(self):
        self.setWindowTitle("GeoInbox — Data Versioning")
        self.setMinimumSize(1000, 700)

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(8)

        main_layout.addWidget(self._create_source_selection())
        main_layout.addWidget(self._create_comparison_setup())
        main_layout.addWidget(self._create_datasets_section(), stretch=1)
        main_layout.addWidget(self._create_actions_section())

        close_layout = QHBoxLayout()
        close_layout.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        close_btn.clicked.connect(self.reject)
        close_layout.addWidget(close_btn)
        main_layout.addLayout(close_layout)

    # ------------------------------------------------------------------ #
    #  TOP: Source Selection                                               #
    # ------------------------------------------------------------------ #

    def _create_source_selection(self) -> QGroupBox:
        """
        Source selection panel.  Two combo boxes list the current QGIS
        project vector layers.  A 'Load File…' fallback is provided for
        datasets that are not yet loaded into the project.
        """
        group = QGroupBox("📁 Source Selection")
        info = QLabel("Client = field edits (incoming).  Host = main database (target).")
        info.setStyleSheet("color: #666; font-size: 11px;")

        layout = QVBoxLayout(group)
        layout.addWidget(info)

        row = QHBoxLayout()

        # --- Client side ---
        client_col = QVBoxLayout()
        client_col.addWidget(QLabel("Client Dataset (Incoming)"))

        client_row = QHBoxLayout()
        self._client_combo = QComboBox()
        self._client_combo.setMinimumWidth(220)
        self._client_combo.currentIndexChanged.connect(self._on_client_layer_changed)
        client_row.addWidget(self._client_combo)

        client_file_btn = QPushButton("Load File…")
        client_file_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        client_file_btn.clicked.connect(self._load_client_file)
        client_row.addWidget(client_file_btn)

        client_col.addLayout(client_row)

        self._client_source_label = QLabel("No dataset loaded")
        self._client_source_label.setStyleSheet("color: #888; font-size: 11px;")
        client_col.addWidget(self._client_source_label)

        row.addLayout(client_col)
        row.addSpacing(20)

        # --- Host side ---
        host_col = QVBoxLayout()
        host_col.addWidget(QLabel("Host Dataset (Target)"))

        host_row = QHBoxLayout()
        self._host_combo = QComboBox()
        self._host_combo.setMinimumWidth(220)
        self._host_combo.currentIndexChanged.connect(self._on_host_layer_changed)
        host_row.addWidget(self._host_combo)

        host_file_btn = QPushButton("Load File…")
        host_file_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        host_file_btn.clicked.connect(self._load_host_file)
        host_row.addWidget(host_file_btn)

        host_col.addLayout(host_row)

        self._host_source_label = QLabel("No dataset loaded")
        self._host_source_label.setStyleSheet("color: #888; font-size: 11px;")
        host_col.addWidget(self._host_source_label)

        row.addLayout(host_col)
        row.addStretch()

        layout.addLayout(row)
        return group

    # ------------------------------------------------------------------ #
    #  TOP: Comparison Setup                                               #
    # ------------------------------------------------------------------ #

    def _create_comparison_setup(self) -> QGroupBox:
        group = QGroupBox("⚙ Match and Settings")
        layout = QVBoxLayout(group)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Match Mode:"))
        self._match_mode_combo = QComboBox()
        self._match_mode_combo.addItem("Smart Match (ID + Geometry)", "smart")
        self._match_mode_combo.addItem("ID Only", "id_only")
        self._match_mode_combo.addItem("Geometry Only", "geom_only")
        self._match_mode_combo.addItem("Manual Match", "manual")
        self._match_mode_combo.setMinimumWidth(180)
        row1.addWidget(self._match_mode_combo)

        row1.addSpacing(20)
        row1.addWidget(QLabel("ID Field:"))
        self._id_field_combo = QComboBox()
        self._id_field_combo.addItem("(auto-detect)")
        self._id_field_combo.setMinimumWidth(120)
        row1.addWidget(self._id_field_combo)

        pick_btn = QPushButton("Pick…")
        pick_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        pick_btn.clicked.connect(self._pick_id_field)
        row1.addWidget(pick_btn)

        row1.addStretch()
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Geometry Tolerance:"))
        self._tolerance_spin = QSpinBox()
        self._tolerance_spin.setRange(1, 10000)
        self._tolerance_spin.setValue(10)
        self._tolerance_spin.setSuffix(" m")
        self._tolerance_spin.setFixedWidth(80)
        row2.addWidget(self._tolerance_spin)

        row2.addSpacing(20)
        row2.addWidget(QLabel("Attribute Threshold:"))
        self._threshold_spin = QSpinBox()
        self._threshold_spin.setRange(0, 100)
        self._threshold_spin.setValue(75)
        self._threshold_spin.setSuffix(" %")
        self._threshold_spin.setFixedWidth(80)
        row2.addWidget(self._threshold_spin)

        row2.addSpacing(20)
        self._compare_geom_check = QCheckBox("Compare Geometry")
        self._compare_geom_check.setChecked(True)
        row2.addWidget(self._compare_geom_check)

        self._compare_attr_check = QCheckBox("Compare Attributes")
        self._compare_attr_check.setChecked(True)
        row2.addWidget(self._compare_attr_check)

        row2.addStretch()

        self._compare_btn = QPushButton("Compare Datasets")
        self._compare_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._compare_btn.clicked.connect(self._compare_datasets)
        row2.addWidget(self._compare_btn)

        layout.addLayout(row2)
        return group

    # ------------------------------------------------------------------ #
    #  MIDDLE: Feature tables                                              #
    # ------------------------------------------------------------------ #

    def _create_datasets_section(self) -> QGroupBox:
        group = QGroupBox("📋 Comparison Results")
        layout = QVBoxLayout(group)

        # Summary stats row
        stats_row = QHBoxLayout()
        self._stat_labels: dict = {}
        for key in ("Total", "Matched", "Added", "Deleted", "Geom Diff", "Attr Diff"):
            stats_row.addWidget(QLabel(f"{key}:"))
            lbl = QLabel("0")
            lbl.setStyleSheet("font-weight: bold;")
            self._stat_labels[key] = lbl
            stats_row.addWidget(lbl)
            stats_row.addSpacing(8)
        stats_row.addStretch()
        layout.addLayout(stats_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._client_panel, self._client_table = self._create_feature_table("Client Results")
        splitter.addWidget(self._client_panel)

        self._host_panel, self._host_table = self._create_feature_table("Host Results")
        splitter.addWidget(self._host_panel)

        self._client_table.itemSelectionChanged.connect(self._on_client_selection_changed)
        self._host_table.itemSelectionChanged.connect(self._on_host_selection_changed)

        splitter.setSizes([500, 500])
        layout.addWidget(splitter)
        return group

    def _create_feature_table(self, title: str) -> tuple:
        panel = QGroupBox(title)
        layout = QVBoxLayout(panel)

        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["FID", "Match", "Diff", "Attr", "Status"])
        for col in range(5):
            table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents if col < 4
                else QHeaderView.ResizeMode.Stretch
            )
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        layout.addWidget(table)
        return panel, table

    # ------------------------------------------------------------------ #
    #  BOTTOM: Actions                                                     #
    # ------------------------------------------------------------------ #

    def _create_actions_section(self) -> QGroupBox:
        group = QGroupBox("Actions & Details")
        layout = QVBoxLayout(group)

        # Selected feature info
        detail_row = QHBoxLayout()
        detail_row.addWidget(QLabel("Selected Feature:"))
        self._selected_feature_label = QLabel("None")
        self._selected_feature_label.setStyleSheet("font-weight: bold;")
        detail_row.addWidget(self._selected_feature_label)

        detail_row.addSpacing(20)
        self._view_on_map_btn = QPushButton("🔍 View on Map")
        self._view_on_map_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._view_on_map_btn.clicked.connect(self._view_on_map)
        self._view_on_map_btn.setEnabled(False)
        detail_row.addWidget(self._view_on_map_btn)
        detail_row.addStretch()
        layout.addLayout(detail_row)

        layout.addWidget(self._separator())

        # Manual match tools
        match_row = QHBoxLayout()
        match_row.addWidget(QLabel("Manual Match / Replace:"))

        self._link_selection_btn = QPushButton("🔗 Link Selection")
        self._link_selection_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._link_selection_btn.clicked.connect(self._link_selection)
        self._link_selection_btn.setEnabled(False)
        match_row.addWidget(self._link_selection_btn)

        self._replace_geom_btn = QPushButton("📐 Replace Geometry")
        self._replace_geom_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._replace_geom_btn.clicked.connect(self._replace_geometry)
        self._replace_geom_btn.setEnabled(False)
        match_row.addWidget(self._replace_geom_btn)

        self._replace_attr_btn = QPushButton("📝 Replace Attributes")
        self._replace_attr_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._replace_attr_btn.clicked.connect(self._replace_attributes)
        self._replace_attr_btn.setEnabled(False)
        match_row.addWidget(self._replace_attr_btn)

        match_row.addStretch()
        layout.addLayout(match_row)

        # Per-feature actions
        single_row = QHBoxLayout()
        single_row.addWidget(QLabel("Single Feature:"))

        self._commit_feature_btn = QPushButton("✓ Commit This Feature")
        self._commit_feature_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._commit_feature_btn.clicked.connect(self._commit_single_feature)
        self._commit_feature_btn.setEnabled(False)
        single_row.addWidget(self._commit_feature_btn)

        self._skip_feature_btn = QPushButton("⏭ Skip")
        self._skip_feature_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._skip_feature_btn.clicked.connect(self._skip_feature)
        self._skip_feature_btn.setEnabled(False)
        single_row.addWidget(self._skip_feature_btn)

        single_row.addStretch()
        layout.addLayout(single_row)

        layout.addWidget(self._separator())

        # Global actions
        global_row = QHBoxLayout()

        self._show_diff_layers_btn = QPushButton("Show Diff Layers")
        self._show_diff_layers_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._show_diff_layers_btn.setToolTip("Add all 9 diff layers to the QGIS map")
        self._show_diff_layers_btn.clicked.connect(self._show_diff_layers)
        global_row.addWidget(self._show_diff_layers_btn)

        self._show_geom_diff_btn = QPushButton("Show Geometry Diff")
        self._show_geom_diff_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._show_geom_diff_btn.clicked.connect(self._show_geometry_diff)
        global_row.addWidget(self._show_geom_diff_btn)

        self._show_attr_diff_btn = QPushButton("Show Attribute Diff")
        self._show_attr_diff_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._show_attr_diff_btn.clicked.connect(self._show_attribute_diff)
        global_row.addWidget(self._show_attr_diff_btn)

        global_row.addStretch()

        self._commit_all_btn = QPushButton("Commit Changes")
        self._commit_all_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._commit_all_btn.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; font-weight: bold; }"
        )
        self._commit_all_btn.clicked.connect(self._commit_changes)
        global_row.addWidget(self._commit_all_btn)

        self._undo_btn = QPushButton("Undo")
        self._undo_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._undo_btn.clicked.connect(self._undo)
        global_row.addWidget(self._undo_btn)

        self._redo_btn = QPushButton("Redo")
        self._redo_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._redo_btn.clicked.connect(self._redo)
        global_row.addWidget(self._redo_btn)

        layout.addLayout(global_row)
        return group

    def _separator(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        return sep

    # ------------------------------------------------------------------ #
    #  QGIS project live sync                                             #
    # ------------------------------------------------------------------ #

    def _connect_project_signals(self):
        """
        Wire QGIS project signals so the layer combos stay in sync with:
          - layers added / removed from the project
          - project opened / cleared
          - layer drag-drop reorder in the legend
          - individual layer renames
        """
        if not HAS_QGIS or self._project_signals_connected:
            return
        proj = QgsProject.instance()

        # Core project signals (always available in QGIS 3.x)
        proj.layersAdded.connect(self._on_layers_added)
        proj.layersRemoved.connect(self._on_layers_removed)
        proj.cleared.connect(self._on_project_cleared)

        # readProject(QDomDocument) fires after the layer tree is fully built.
        # Connect it; also try projectRead() (no-arg, added in QGIS 3.2) as backup.
        try:
            proj.readProject.connect(self._on_project_read)
        except AttributeError:
            pass
        try:
            proj.projectRead.connect(self._on_project_read)
        except AttributeError:
            pass

        # Layer-tree structure signals — cover drag-drop reorder and group changes.
        # addedChildren / removedChildren pass arguments; connect a no-arg lambda
        # so there is no parameter mismatch (which silently breaks in PyQt5).
        root = proj.layerTreeRoot()
        for sig_name in ('layerOrderChanged',):
            try:
                getattr(root, sig_name).connect(self._refresh_layer_combos)
            except AttributeError:
                pass
        for sig_name in ('addedChildren', 'removedChildren'):
            try:
                getattr(root, sig_name).connect(
                    lambda *_: self._refresh_layer_combos()
                )
            except AttributeError:
                pass

        # Connect to each existing layer's nameChanged so renames propagate
        for layer in proj.mapLayers().values():
            self._connect_layer_rename(layer)

        self._project_signals_connected = True

    def _connect_layer_rename(self, layer):
        """Connect a single layer's nameChanged signal to refresh the combos."""
        try:
            layer.nameChanged.connect(self._refresh_layer_combos)
        except (AttributeError, RuntimeError):
            pass

    def _disconnect_project_signals(self):
        """Disconnect all project signals to prevent leaks after close."""
        if not HAS_QGIS or not self._project_signals_connected:
            return
        proj = QgsProject.instance()

        for signal, slot in (
            (proj.layersAdded, self._on_layers_added),
            (proj.layersRemoved, self._on_layers_removed),
            (proj.cleared, self._on_project_cleared),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

        for sig_name in ('readProject', 'projectRead'):
            try:
                getattr(proj, sig_name).disconnect(self._on_project_read)
            except (AttributeError, RuntimeError, TypeError):
                pass

        # Tree signals: layerOrderChanged was connected to the named method;
        # addedChildren / removedChildren used anonymous lambdas that cannot be
        # individually disconnected — disconnect ALL slots from those signals
        # (safe here because only this dialog connects to them).
        root = proj.layerTreeRoot()
        try:
            root.layerOrderChanged.disconnect(self._refresh_layer_combos)
        except (AttributeError, RuntimeError, TypeError):
            pass
        for sig_name in ('addedChildren', 'removedChildren'):
            try:
                getattr(root, sig_name).disconnect()
            except (AttributeError, RuntimeError, TypeError):
                pass

        # Disconnect per-layer rename signals
        for layer in proj.mapLayers().values():
            try:
                layer.nameChanged.disconnect(self._refresh_layer_combos)
            except (AttributeError, RuntimeError, TypeError):
                pass

        self._project_signals_connected = False

    def _on_layers_added(self, layers):
        """Connect rename signals for newly added layers, then refresh.

        Deferred via QTimer so the layer tree has time to be built from the
        project XML before we walk it — layersAdded fires before the tree nodes
        exist when a project is being loaded.
        """
        for layer in layers:
            self._connect_layer_rename(layer)
        QTimer.singleShot(0, self._refresh_layer_combos)

    def _on_layers_removed(self, layer_ids):
        """Refresh and clear a panel if its layer was removed."""
        self._refresh_layer_combos()
        if self._client_layer and self._client_layer.id() in layer_ids:
            self._client_layer = None
            self._client_source_label.setText("Layer was removed from project")
            self._client_source_label.setStyleSheet("color: #E65100; font-size: 11px;")
        if self._host_layer and self._host_layer.id() in layer_ids:
            self._host_layer = None
            self._host_source_label.setText("Layer was removed from project")
            self._host_source_label.setStyleSheet("color: #E65100; font-size: 11px;")

    def _on_project_cleared(self):
        """Reset both panels when the project is closed / cleared."""
        self._client_layer = None
        self._host_layer = None
        self._refresh_layer_combos()
        self._client_source_label.setText("No dataset loaded")
        self._client_source_label.setStyleSheet("color: #888; font-size: 11px;")
        self._host_source_label.setText("No dataset loaded")
        self._host_source_label.setStyleSheet("color: #888; font-size: 11px;")
        self._client_table.setRowCount(0)
        self._host_table.setRowCount(0)

    def _on_project_read(self, doc=None):
        """Refresh layer list when a new project is loaded.

        Uses a timer to run after any remaining synchronous project-load
        work completes, guaranteeing the layer tree is fully populated.
        """
        QTimer.singleShot(0, self._refresh_layer_combos)

    def _refresh_layer_combos(self):
        """
        Rebuild both source combos to match the current project layers
        in legend order (top → bottom), showing only vector layers.
        Restores the previously selected layer if it still exists.
        """
        if not HAS_QGIS:
            return

        # Remember current selections by layer id
        prev_client_id = self._client_layer.id() if self._client_layer else None
        prev_host_id = self._host_layer.id() if self._host_layer else None

        # Get vector layers in legend order
        layers = self._get_ordered_vector_layers()

        for combo, prev_id in (
            (self._client_combo, prev_client_id),
            (self._host_combo, prev_host_id),
        ):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— select layer —", None)
            restore_idx = 0
            for i, layer in enumerate(layers, start=1):
                combo.addItem(layer.name(), layer.id())
                if layer.id() == prev_id:
                    restore_idx = i
            combo.setCurrentIndex(restore_idx)
            combo.blockSignals(False)

        # Re-resolve layer objects in case ids are still valid
        self._client_layer = self._layer_from_combo(self._client_combo)
        self._host_layer = self._layer_from_combo(self._host_combo)

    def _get_ordered_vector_layers(self):
        """
        Return vector layers in canvas display order.
        Uses iface.mapCanvas().layers() as primary (reliable, ordered).
        Falls back to QgsProject.mapLayers() registry if iface is unavailable.
        """
        if not HAS_QGIS:
            return []

        # Primary: canvas layer order (top → bottom, all visible + hidden layers
        # via layerTreeRoot custom order)
        try:
            if iface is not None:
                return [l for l in iface.mapCanvas().layers()
                        if isinstance(l, QgsVectorLayer)]
        except Exception as exc:
            from ..utils.security import log_debug_exception
            log_debug_exception("Failed to read map canvas layers", exc)

        # Fallback: unordered registry
        return [l for l in QgsProject.instance().mapLayers().values()
                if isinstance(l, QgsVectorLayer)]

    def _layer_from_combo(self, combo: QComboBox) -> Optional['QgsVectorLayer']:
        layer_id = combo.currentData()
        if not layer_id or not HAS_QGIS:
            return None
        return QgsProject.instance().mapLayer(layer_id)

    # ------------------------------------------------------------------ #
    #  Combo change handlers                                               #
    # ------------------------------------------------------------------ #

    def _on_client_layer_changed(self, index: int):
        self._client_layer = self._layer_from_combo(self._client_combo)
        if self._client_layer:
            n = self._client_layer.featureCount()
            geom = self._client_layer.geometryType()
            self._client_source_label.setText(
                f"{n} features · {self._geom_type_name(geom)} · {self._client_layer.crs().authid()}"
            )
            self._client_source_label.setStyleSheet("color: #2E7D32; font-size: 11px;")
            self._refresh_id_field_combo()
        else:
            self._client_source_label.setText("No dataset loaded")
            self._client_source_label.setStyleSheet("color: #888; font-size: 11px;")

    def _on_host_layer_changed(self, index: int):
        self._host_layer = self._layer_from_combo(self._host_combo)
        if self._host_layer:
            n = self._host_layer.featureCount()
            geom = self._host_layer.geometryType()
            self._host_source_label.setText(
                f"{n} features · {self._geom_type_name(geom)} · {self._host_layer.crs().authid()}"
            )
            self._host_source_label.setStyleSheet("color: #2E7D32; font-size: 11px;")
        else:
            self._host_source_label.setText("No dataset loaded")
            self._host_source_label.setStyleSheet("color: #888; font-size: 11px;")

    def _geom_type_name(self, geom_type) -> str:
        names = {0: "Point", 1: "Line", 2: "Polygon", 3: "Unknown"}
        return names.get(geom_type, "Unknown")

    def _refresh_id_field_combo(self):
        """Populate the ID field combo from the client layer's fields."""
        self._id_field_combo.blockSignals(True)
        self._id_field_combo.clear()
        self._id_field_combo.addItem("(auto-detect)")
        if self._client_layer:
            for field in self._client_layer.fields():
                self._id_field_combo.addItem(field.name())
        self._id_field_combo.blockSignals(False)

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def showEvent(self, event):
        """Connect signals and populate combos when dialog becomes visible."""
        super().showEvent(event)
        self._connect_project_signals()
        self._refresh_layer_combos()

    def closeEvent(self, event):
        """Always disconnect project signals to prevent dangling connections."""
        self._disconnect_project_signals()
        super().closeEvent(event)

    # ------------------------------------------------------------------ #
    #  Table selection                                                     #
    # ------------------------------------------------------------------ #

    def _on_client_selection_changed(self):
        selected = self._client_table.selectedItems()
        if selected:
            fid_item = self._client_table.item(selected[0].row(), 0)
            if fid_item:
                self._selected_client_fid = fid_item.text()
                self._update_selection_state()

    def _on_host_selection_changed(self):
        selected = self._host_table.selectedItems()
        if selected:
            fid_item = self._host_table.item(selected[0].row(), 0)
            if fid_item:
                self._selected_host_fid = fid_item.text()
                self._update_selection_state()

    def _update_selection_state(self):
        has_client = self._selected_client_fid is not None
        has_host = self._selected_host_fid is not None

        if has_client and has_host:
            self._selected_feature_label.setText(
                f"Client: {self._selected_client_fid}  |  Host: {self._selected_host_fid}"
            )
        elif has_client:
            self._selected_feature_label.setText(f"Client: {self._selected_client_fid}")
        elif has_host:
            self._selected_feature_label.setText(f"Host: {self._selected_host_fid}")
        else:
            self._selected_feature_label.setText("None")

        self._view_on_map_btn.setEnabled(has_client or has_host)
        self._link_selection_btn.setEnabled(has_client and has_host)
        self._replace_geom_btn.setEnabled(has_client and has_host)
        self._replace_attr_btn.setEnabled(has_client and has_host)
        self._commit_feature_btn.setEnabled(has_client)
        self._skip_feature_btn.setEnabled(has_client)

    # ------------------------------------------------------------------ #
    #  Actions (placeholders for implementation)                           #
    # ------------------------------------------------------------------ #

    def _load_client_file(self):
        from .widgets import show_info_dialog
        show_info_dialog(self, "Load Client", "Load client dataset — select GeoJSON, GPKG or Shapefile")

    def _load_host_file(self):
        from .widgets import show_info_dialog
        show_info_dialog(self, "Load Host", "Load host dataset — select GeoJSON, GPKG or Shapefile")

    def _pick_id_field(self):
        from .widgets import show_info_dialog
        show_info_dialog(self, "Pick ID Field", "Pick the unique ID field from the client layer")

    def _compare_datasets(self):
        from .widgets import show_info_dialog, show_error_dialog
        if not self._client_layer or not self._host_layer:
            show_info_dialog(self, "Compare", "Please select both a Client and Host layer first.")
            return

        try:
            from ..versioning.loader import DataLoader
            from ..versioning.matcher import MatchingEngine
            from ..versioning.differ import DiffEngine

            loader = DataLoader()
            client_dataset = loader._normalize_layer(
                self._client_layer, self._client_layer.source(), 'qgis'
            )
            host_dataset = loader._normalize_layer(
                self._host_layer, self._host_layer.source(), 'qgis'
            )

            matcher = MatchingEngine()
            id_text = self._id_field_combo.currentText()
            id_field = None if id_text == '(auto-detect)' else id_text
            tolerance = float(self._tolerance_spin.value())
            threshold = self._threshold_spin.value() / 100.0
            matcher.configure(
                id_field=id_field,
                spatial_tolerance=tolerance,
                bbox_threshold=threshold
            )
            match_result = matcher.smart_match(client_dataset, host_dataset)

            engine = DiffEngine()
            engine.configure(geometry_tolerance=tolerance)
            self._diff_summary = engine.compute_diff(
                match_result.matches,
                match_result.unmatched_client,
                match_result.unmatched_host
            )

            s = self._diff_summary
            matched = s.total_features - s.new_features - s.deleted_features
            self._stat_labels["Total"].setText(str(s.total_features))
            self._stat_labels["Matched"].setText(str(matched))
            self._stat_labels["Added"].setText(str(s.new_features))
            self._stat_labels["Deleted"].setText(str(s.deleted_features))
            self._stat_labels["Geom Diff"].setText(str(s.geometry_only + s.both_changed))
            self._stat_labels["Attr Diff"].setText(str(s.attribute_only + s.both_changed))

            self._populate_feature_tables(self._diff_summary)

        except Exception as e:
            show_error_dialog(self, "Compare Error", str(e))

    def _view_on_map(self):
        if not HAS_QGIS:
            return
        layer = self._client_layer if self._selected_client_fid else self._host_layer
        fid = self._selected_client_fid or self._selected_host_fid
        if layer and fid:
            try:
                layer.selectByIds([int(fid)])
                iface.mapCanvas().zoomToSelected(layer)
                iface.mapCanvas().refresh()
            except Exception as e:
                from .widgets import show_error_dialog
                show_error_dialog(self, "View on Map Error", str(e))

    def _link_selection(self):
        from .widgets import show_info_dialog
        show_info_dialog(
            self, "Link Selection",
            f"Linking Client {self._selected_client_fid} ↔ Host {self._selected_host_fid}"
        )

    def _replace_geometry(self):
        from .widgets import show_info_dialog
        show_info_dialog(self, "Replace Geometry", "Replacing host geometry with client geometry")

    def _replace_attributes(self):
        from .widgets import show_info_dialog
        show_info_dialog(self, "Replace Attributes", "Replacing host attributes with client attributes")

    def _commit_single_feature(self):
        from .widgets import show_info_dialog
        show_info_dialog(self, "Commit Feature", f"Committing feature {self._selected_client_fid}")

    def _skip_feature(self):
        from .widgets import show_info_dialog
        show_info_dialog(self, "Skip Feature", f"Skipping feature {self._selected_client_fid}")

    def _show_diff_layers(self):
        """
        Add all 9 diff layers to the QGIS map canvas.

        Layer set (render order bottom → top):
          1  [Context]  Unchanged          – gray   (matched, no change)
          2  [Diff]     Attributes Changed – blue   (same geom, attrs differ)
          3  [Diff]     Geometry Moved     – orange (new position of moved feature)
          4  [Diff]     Geom Removed Parts – red    (old_geom.difference(new_geom))
          5  [Diff]     Geom Added Parts   – green  (new_geom.difference(old_geom))
          6  [Diff]     Both Changed       – magenta(geom + attrs differ)
          7  [Diff]     Displacement Arrows– dark-orange line centroid→centroid
          8  [Client]   New Features       – bright green (INSERT candidates)
          9  [Host]     Deleted Features   – red    (DELETE candidates)

        The colored style swatch that QGIS draws to the LEFT of every layer
        name in the Layers panel comes from QgsSingleSymbolRenderer.  QGIS
        renders it automatically — call iface.layerTreeView()
        .refreshLayerSymbology() after adding to make it appear immediately.
        """
        from .widgets import show_info_dialog, show_error_dialog
        if not HAS_QGIS:
            return
        if self._diff_summary is None:
            show_info_dialog(self, "Diff Layers",
                             "Please run 'Compare Datasets' first.")
            return
        if not self._client_layer or not self._host_layer:
            show_info_dialog(self, "Diff Layers",
                             "Client / host layers are no longer available.")
            return

        try:
            from qgis.core import (
                QgsVectorLayer, QgsFeature, QgsGeometry,
                QgsSymbol, QgsSingleSymbolRenderer, QgsLayerTreeLayer
            )
            from qgis.PyQt.QtGui import QColor
            from ..versioning.differ import DiffType

            proj   = QgsProject.instance()
            root   = proj.layerTreeRoot()
            canvas = iface.mapCanvas()

            # ── 1. Clean up any previous "GeoInbox Diff" group ─────────────
            existing = root.findGroup("GeoInbox Diff")
            if existing:
                for tree_lyr in existing.findLayers():
                    proj.removeMapLayer(tree_lyr.layerId())
                root.removeChildNode(existing)

            group = root.insertGroup(0, "GeoInbox Diff")

            crs    = self._client_layer.crs().authid() or "EPSG:4326"
            c_geom = self._geom_type_uri_str(self._client_layer)
            h_geom = self._geom_type_uri_str(self._host_layer)
            diffs  = self._diff_summary.feature_diffs

            # ── 2. Helpers ─────────────────────────────────────────────────
            def _build(name, geom_str, color_hex, opacity=1.0):
                """Create a styled memory layer (NOT yet added to project)."""
                uri = f"{geom_str}?crs={crs}"
                lyr = QgsVectorLayer(uri, name, "memory")
                if not lyr.isValid():
                    return None
                sym = QgsSymbol.defaultSymbol(lyr.geometryType())
                if sym:
                    sym.setColor(QColor(color_hex))
                    sym.setOpacity(opacity)
                    lyr.setRenderer(QgsSingleSymbolRenderer(sym))
                return lyr

            def _push(lyr):
                """Register layer in project + append to group + refresh icon."""
                proj.addMapLayer(lyr, False)                       # add to registry only
                group.insertChildNode(-1, QgsLayerTreeLayer(lyr))  # explicit tree node
                iface.layerTreeView().refreshLayerSymbology(lyr.id())

            def _fill(lyr, feats):
                """Add a list of QgsFeature objects via the data provider."""
                dp = lyr.dataProvider()
                if feats:
                    dp.addFeatures(feats)
                lyr.updateExtents()

            # ── 3. Build each layer (9 → 1 = top of panel → bottom) ────────

            # 9 ── [Host] Deleted Features  (red) ──────────────────────────
            lyr9 = _build("[Host] Deleted Features", h_geom, "#F44336")
            if lyr9:
                feats = []
                for d in diffs:
                    if d.diff_type == DiffType.DELETED and d.match.host_feature:
                        f = QgsFeature(); f.setGeometry(
                            QgsGeometry(d.match.host_feature.geometry)); feats.append(f)
                _fill(lyr9, feats); _push(lyr9)

            # 8 ── [Client] New Features  (bright green) ───────────────────
            lyr8 = _build("[Client] New Features", c_geom, "#00C853")
            if lyr8:
                feats = []
                for d in diffs:
                    if d.diff_type == DiffType.NEW and d.match.client_feature:
                        f = QgsFeature(); f.setGeometry(
                            QgsGeometry(d.match.client_feature.geometry)); feats.append(f)
                _fill(lyr8, feats); _push(lyr8)

            # 7 ── [Diff] Displacement Arrows  (dark orange, LineString) ───
            lyr7 = _build("[Diff] Displacement Arrows", "LineString", "#E65100")
            if lyr7:
                feats = []
                for d in diffs:
                    if (d.diff_type in (DiffType.GEOMETRY_ONLY, DiffType.BOTH)
                            and d.geometry_diff is not None):
                        p0 = d.geometry_diff.host_geometry.centroid().asPoint()
                        p1 = d.geometry_diff.client_geometry.centroid().asPoint()
                        f = QgsFeature()
                        f.setGeometry(QgsGeometry.fromPolylineXY([p0, p1]))
                        feats.append(f)
                _fill(lyr7, feats); _push(lyr7)

            # 6 ── [Diff] Both Changed  (magenta) ──────────────────────────
            lyr6 = _build("[Diff] Both Changed", c_geom, "#E91E63")
            if lyr6:
                feats = []
                for d in diffs:
                    if d.diff_type == DiffType.BOTH and d.match.client_feature:
                        f = QgsFeature(); f.setGeometry(
                            QgsGeometry(d.match.client_feature.geometry)); feats.append(f)
                _fill(lyr6, feats); _push(lyr6)

            # 5 ── [Diff] Geom Added Parts  (green, 60 % opacity) ──────────
            lyr5 = _build("[Diff] Geom Added Parts", c_geom, "#4CAF50", opacity=0.6)
            if lyr5:
                feats = []
                for d in diffs:
                    if (d.diff_type in (DiffType.GEOMETRY_ONLY, DiffType.BOTH)
                            and d.geometry_diff is not None):
                        try:
                            g = d.geometry_diff.client_geometry.difference(
                                d.geometry_diff.host_geometry)
                            if g and not g.isEmpty():
                                f = QgsFeature(); f.setGeometry(g); feats.append(f)
                        except Exception as exc:
                            from ..utils.security import log_debug_exception
                            log_debug_exception("Geom added parts diff failed", exc)
                _fill(lyr5, feats); _push(lyr5)

            # 4 ── [Diff] Geom Removed Parts  (red, 60 % opacity) ──────────
            lyr4 = _build("[Diff] Geom Removed Parts", h_geom, "#D32F2F", opacity=0.6)
            if lyr4:
                feats = []
                for d in diffs:
                    if (d.diff_type in (DiffType.GEOMETRY_ONLY, DiffType.BOTH)
                            and d.geometry_diff is not None):
                        try:
                            g = d.geometry_diff.host_geometry.difference(
                                d.geometry_diff.client_geometry)
                            if g and not g.isEmpty():
                                f = QgsFeature(); f.setGeometry(g); feats.append(f)
                        except Exception as exc:
                            from ..utils.security import log_debug_exception
                            log_debug_exception("Geom removed parts diff failed", exc)
                _fill(lyr4, feats); _push(lyr4)

            # 3 ── [Diff] Geometry Moved  (orange) ─────────────────────────
            lyr3 = _build("[Diff] Geometry Moved", c_geom, "#FF9800")
            if lyr3:
                feats = []
                for d in diffs:
                    if d.diff_type == DiffType.GEOMETRY_ONLY and d.match.client_feature:
                        f = QgsFeature(); f.setGeometry(
                            QgsGeometry(d.match.client_feature.geometry)); feats.append(f)
                _fill(lyr3, feats); _push(lyr3)

            # 2 ── [Diff] Attributes Changed  (blue) ───────────────────────
            lyr2 = _build("[Diff] Attributes Changed", c_geom, "#1565C0")
            if lyr2:
                feats = []
                for d in diffs:
                    if d.diff_type == DiffType.ATTRIBUTE_ONLY and d.match.client_feature:
                        f = QgsFeature(); f.setGeometry(
                            QgsGeometry(d.match.client_feature.geometry)); feats.append(f)
                _fill(lyr2, feats); _push(lyr2)

            # 1 ── [Context] Unchanged  (gray, 40 % opacity) ───────────────
            lyr1 = _build("[Context] Unchanged", c_geom, "#9E9E9E", opacity=0.4)
            if lyr1:
                feats = []
                for d in diffs:
                    if d.diff_type == DiffType.NO_CHANGE and d.match.client_feature:
                        f = QgsFeature(); f.setGeometry(
                            QgsGeometry(d.match.client_feature.geometry)); feats.append(f)
                _fill(lyr1, feats); _push(lyr1)

            canvas.refresh()

        except Exception as e:
            import traceback
            show_error_dialog(self, "Diff Layers Error",
                              str(e), traceback.format_exc())

    def _geom_type_uri_str(self, layer) -> str:
        """Return the geometry type string used in a memory-layer URI."""
        return {0: "Point", 1: "LineString", 2: "Polygon"}.get(
            layer.geometryType(), "Point"
        )

    def _populate_feature_tables(self, diff_summary):
        """Fill both feature tables from a DiffSummary."""
        from ..versioning.differ import DiffType
        from qgis.PyQt.QtGui import QColor

        STATUS = {
            DiffType.NO_CHANGE:      ('#9E9E9E', 'Unchanged'),
            DiffType.ATTRIBUTE_ONLY: ('#1565C0', 'Attr Changed'),
            DiffType.GEOMETRY_ONLY:  ('#FF9800', 'Geom Moved'),
            DiffType.BOTH:           ('#E91E63', 'Both Changed'),
            DiffType.NEW:            ('#00C853', 'New'),
            DiffType.DELETED:        ('#F44336', 'Deleted'),
        }

        self._client_table.setRowCount(0)
        self._host_table.setRowCount(0)

        for diff in diff_summary.feature_diffs:
            color_hex, status_text = STATUS.get(diff.diff_type, ('#000000', '?'))
            color = QColor(color_hex)
            n_changed = len([a for a in diff.attribute_diffs if a.is_different])
            match_text = diff.match.match_type.value if diff.match.match_type else ''
            diff_text = diff.diff_type.value
            attr_text = str(n_changed) if n_changed else ''

            def _status_item(text, clr):
                item = QTableWidgetItem(text)
                item.setForeground(clr)
                return item

            # ── Client table (all except DELETED) ──────────────────────
            if diff.diff_type != DiffType.DELETED and diff.match.client_feature:
                cf = diff.match.client_feature
                row = self._client_table.rowCount()
                self._client_table.insertRow(row)
                self._client_table.setItem(row, 0, QTableWidgetItem(cf.feature_id))
                self._client_table.setItem(row, 1, QTableWidgetItem(match_text))
                self._client_table.setItem(row, 2, QTableWidgetItem(diff_text))
                self._client_table.setItem(row, 3, QTableWidgetItem(attr_text))
                self._client_table.setItem(row, 4, _status_item(status_text, color))

            # ── Host table (all except NEW) ─────────────────────────────
            if diff.diff_type != DiffType.NEW and diff.match.host_feature:
                hf = diff.match.host_feature
                row = self._host_table.rowCount()
                self._host_table.insertRow(row)
                self._host_table.setItem(row, 0, QTableWidgetItem(hf.feature_id))
                self._host_table.setItem(row, 1, QTableWidgetItem(match_text))
                self._host_table.setItem(row, 2, QTableWidgetItem(diff_text))
                self._host_table.setItem(row, 3, QTableWidgetItem(attr_text))
                self._host_table.setItem(row, 4, _status_item(status_text, color))

    def _show_geometry_diff(self):
        from .widgets import show_info_dialog
        show_info_dialog(self, "Geometry Diff", "Geometry diff visualization")

    def _show_attribute_diff(self):
        from .widgets import show_info_dialog
        show_info_dialog(self, "Attribute Diff", "Attribute diff table")

    def _commit_changes(self):
        from .widgets import show_info_dialog
        show_info_dialog(self, "Commit Changes", "Committing all changes to host dataset")

    def _undo(self):
        from .widgets import show_info_dialog
        show_info_dialog(self, "Undo", "Undo last action")

    def _redo(self):
        from .widgets import show_info_dialog
        show_info_dialog(self, "Redo", "Redo last undone action")
