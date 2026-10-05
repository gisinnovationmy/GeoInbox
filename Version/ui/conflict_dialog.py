"""
GeoInbox - Conflict Resolution Dialog

Dialog for resolving conflicts when host data has changed since snapshot.
"""

from typing import Optional, List, Dict, Any
from enum import Enum

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
    QGroupBox, QDialogButtonBox, QSplitter, QTextBrowser,
    QSizePolicy, QWidget
)
from qgis.PyQt.QtCore import Qt

from ..versioning.loader import NormalizedFeature
from ..versioning.differ import FeatureDiff, AttributeDiff


class ConflictResolution(Enum):
    """Resolution options for conflicts."""
    OVERWRITE = "overwrite"  # Use client value
    KEEP = "keep"            # Keep host value
    MERGE = "merge"          # Merge (for compatible types)
    SKIP = "skip"            # Skip this feature
    NEW = "new"              # Create as new feature


class ConflictItem:
    """A single conflict to resolve."""
    
    def __init__(
        self,
        feature_diff: FeatureDiff,
        conflict_fields: List[str],
        current_host_values: Dict[str, Any]
    ):
        self.feature_diff = feature_diff
        self.conflict_fields = conflict_fields
        self.current_host_values = current_host_values
        self.resolution = ConflictResolution.OVERWRITE
        self.field_resolutions: Dict[str, ConflictResolution] = {}


class ConflictDialog(QDialog):
    """
    Dialog for resolving conflicts between client changes and current host state.
    
    Shows when host data has changed since the snapshot was taken.
    """
    
    def __init__(
        self,
        conflicts: List[ConflictItem],
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self._conflicts = conflicts
        self._current_index = 0
        self._setup_ui()
        self._load_conflict(0)
    
    def _setup_ui(self):
        self.setWindowTitle("Resolve Conflicts")
        self.setMinimumSize(800, 600)
        
        layout = QVBoxLayout(self)
        
        # Header
        header = QHBoxLayout()
        self._conflict_label = QLabel()
        # self._conflict_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        header.addWidget(self._conflict_label)
        header.addStretch()
        
        # Navigation
        self._prev_btn = QPushButton("← Previous")
        self._prev_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._prev_btn.clicked.connect(self._prev_conflict)
        header.addWidget(self._prev_btn)
        
        self._next_btn = QPushButton("Next →")
        self._next_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._next_btn.clicked.connect(self._next_conflict)
        header.addWidget(self._next_btn)
        
        layout.addLayout(header)
        
        # Main content splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Left - Values comparison
        values_group = QGroupBox("Value Comparison")
        values_layout = QVBoxLayout(values_group)
        
        self._values_table = QTableWidget()
        self._values_table.setColumnCount(4)
        self._values_table.setHorizontalHeaderLabels([
            "Field", "Client Value", "Host Value (Snapshot)", "Current Host Value"
        ])
        self._values_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        values_layout.addWidget(self._values_table)
        
        splitter.addWidget(values_group)
        
        # Right - Resolution options
        resolution_group = QGroupBox("Resolution")
        resolution_layout = QVBoxLayout(resolution_group)
        
        # Global resolution
        global_row = QHBoxLayout()
        global_row.addWidget(QLabel("Apply to all fields:"))
        
        self._global_resolution = QComboBox()
        self._global_resolution.addItem("Overwrite (use client)", ConflictResolution.OVERWRITE)
        self._global_resolution.addItem("Keep (use current host)", ConflictResolution.KEEP)
        self._global_resolution.addItem("Skip this feature", ConflictResolution.SKIP)
        self._global_resolution.addItem("Create as new feature", ConflictResolution.NEW)
        self._global_resolution.currentIndexChanged.connect(self._on_global_resolution_changed)
        global_row.addWidget(self._global_resolution)
        
        resolution_layout.addLayout(global_row)
        
        # Per-field resolutions
        resolution_layout.addWidget(QLabel("Per-field resolutions:"))
        
        self._field_table = QTableWidget()
        self._field_table.setColumnCount(2)
        self._field_table.setHorizontalHeaderLabels(["Field", "Resolution"])
        self._field_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        resolution_layout.addWidget(self._field_table)
        
        # Preview
        resolution_layout.addWidget(QLabel("Result preview:"))
        self._preview = QTextBrowser()
        self._preview.setMaximumHeight(150)
        resolution_layout.addWidget(self._preview)
        
        splitter.addWidget(resolution_group)
        splitter.setSizes([500, 300])
        
        layout.addWidget(splitter)
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        
        apply_all_btn = QPushButton("Apply Resolution to All")
        apply_all_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        apply_all_btn.clicked.connect(self._apply_to_all)
        btn_layout.addWidget(apply_all_btn)
        
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        btn_layout.addWidget(buttons)
        
        layout.addLayout(btn_layout)
    
    def _load_conflict(self, index: int):
        """Load a conflict into the UI."""
        if index < 0 or index >= len(self._conflicts):
            return
        
        self._current_index = index
        conflict = self._conflicts[index]
        
        # Update header
        self._conflict_label.setText(
            f"Conflict {index + 1} of {len(self._conflicts)}: "
            f"Feature {conflict.feature_diff.match.client_feature.feature_id}"
        )
        
        # Update navigation buttons
        self._prev_btn.setEnabled(index > 0)
        self._next_btn.setEnabled(index < len(self._conflicts) - 1)
        
        # Load values table
        self._values_table.setRowCount(0)
        
        client = conflict.feature_diff.match.client_feature
        host = conflict.feature_diff.match.host_feature
        
        for attr_diff in conflict.feature_diff.attribute_diffs:
            row = self._values_table.rowCount()
            self._values_table.insertRow(row)
            
            # Field name
            field_item = QTableWidgetItem(attr_diff.field_name)
            if attr_diff.field_name in conflict.conflict_fields:
                field_item.setBackground(Qt.GlobalColor.yellow)
            self._values_table.setItem(row, 0, field_item)
            
            # Client value
            self._values_table.setItem(
                row, 1, QTableWidgetItem(str(attr_diff.client_value or ''))
            )
            
            # Host value (snapshot)
            self._values_table.setItem(
                row, 2, QTableWidgetItem(str(attr_diff.host_value or ''))
            )
            
            # Current host value
            current = conflict.current_host_values.get(attr_diff.field_name, '')
            self._values_table.setItem(row, 3, QTableWidgetItem(str(current)))
        
        # Load field resolutions
        self._field_table.setRowCount(0)
        
        for field in conflict.conflict_fields:
            row = self._field_table.rowCount()
            self._field_table.insertRow(row)
            
            self._field_table.setItem(row, 0, QTableWidgetItem(field))
            
            combo = QComboBox()
            combo.addItem("Overwrite", ConflictResolution.OVERWRITE)
            combo.addItem("Keep", ConflictResolution.KEEP)
            
            # Set current resolution
            current_res = conflict.field_resolutions.get(field, ConflictResolution.OVERWRITE)
            idx = combo.findData(current_res)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            
            combo.currentIndexChanged.connect(
                lambda i, f=field: self._on_field_resolution_changed(f, i)
            )
            
            self._field_table.setCellWidget(row, 1, combo)
        
        # Set global resolution
        idx = self._global_resolution.findData(conflict.resolution)
        if idx >= 0:
            self._global_resolution.setCurrentIndex(idx)
        
        self._update_preview()
    
    def _on_global_resolution_changed(self, index: int):
        """Handle global resolution change."""
        resolution = self._global_resolution.currentData()
        if self._current_index < len(self._conflicts):
            self._conflicts[self._current_index].resolution = resolution
        self._update_preview()
    
    def _on_field_resolution_changed(self, field: str, index: int):
        """Handle per-field resolution change."""
        if self._current_index < len(self._conflicts):
            combo = self._field_table.cellWidget(
                self._field_table.currentRow(), 1
            )
            if combo:
                resolution = combo.currentData()
                self._conflicts[self._current_index].field_resolutions[field] = resolution
        self._update_preview()
    
    def _update_preview(self):
        """Update the result preview."""
        if self._current_index >= len(self._conflicts):
            return
        
        conflict = self._conflicts[self._current_index]
        
        preview = "<h4>Resulting Values:</h4><ul>"
        
        for attr_diff in conflict.feature_diff.attribute_diffs:
            field = attr_diff.field_name
            
            if conflict.resolution == ConflictResolution.SKIP:
                preview += f"<li><b>{field}:</b> (skipped)</li>"
            elif conflict.resolution == ConflictResolution.KEEP:
                value = conflict.current_host_values.get(field, attr_diff.host_value)
                preview += f"<li><b>{field}:</b> {value} (kept)</li>"
            elif conflict.resolution == ConflictResolution.NEW:
                preview += f"<li><b>{field}:</b> {attr_diff.client_value} (new feature)</li>"
            else:
                # Check per-field resolution
                field_res = conflict.field_resolutions.get(field, ConflictResolution.OVERWRITE)
                if field_res == ConflictResolution.KEEP:
                    value = conflict.current_host_values.get(field, attr_diff.host_value)
                    preview += f"<li><b>{field}:</b> {value} (kept)</li>"
                else:
                    preview += f"<li><b>{field}:</b> {attr_diff.client_value} (client)</li>"
        
        preview += "</ul>"
        self._preview.setHtml(preview)
    
    def _prev_conflict(self):
        """Go to previous conflict."""
        self._load_conflict(self._current_index - 1)
    
    def _next_conflict(self):
        """Go to next conflict."""
        self._load_conflict(self._current_index + 1)
    
    def _apply_to_all(self):
        """Apply current resolution to all conflicts."""
        resolution = self._global_resolution.currentData()
        for conflict in self._conflicts:
            conflict.resolution = resolution
        
        from .widgets import show_info_dialog
        show_info_dialog(
            self,
            "Applied",
            f"Applied '{resolution.value}' resolution to all {len(self._conflicts)} conflicts."
        )
    
    def get_resolutions(self) -> List[ConflictItem]:
        """Get all conflicts with their resolutions."""
        return self._conflicts
