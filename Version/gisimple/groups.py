"""
GeoInbox - GISimple Groups

Group dataset browsing and management for GISimple integration.
"""

from typing import Optional, List, Dict, Any

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem,
    QHeaderView, QPushButton, QLabel, QProgressBar, QGroupBox
)
from qgis.PyQt.QtCore import Qt, pyqtSignal

from .client import GISimpleClient
from ..security.gisimple_auth import GISimpleDataset
from ..utils.errors import GISimpleServerError, get_user_friendly_message


class GISimpleGroupsWidget(QWidget):
    """
    Widget for browsing GISimple groups and datasets.
    
    Can be embedded in the Email Browser Panel as a tab.
    """
    
    datasetDownloaded = pyqtSignal(str)  # Emits path to downloaded file
    datasetSelected = pyqtSignal(object)  # Emits GISimpleDataset
    
    def __init__(self, client: Optional[GISimpleClient] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._client = client or GISimpleClient()
        self._current_group_id: Optional[str] = None
        self._datasets: List[GISimpleDataset] = []
        self._setup_ui()
    
    def _setup_ui(self):
        """Setup the widget UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        
        # Auth status
        self._auth_status = QLabel("Not connected to GISimple")
        self._auth_status.setStyleSheet("color: #666;")
        layout.addWidget(self._auth_status)
        
        # Main splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Left - Groups list
        groups_panel = self._create_groups_panel()
        splitter.addWidget(groups_panel)
        
        # Right - Datasets table
        datasets_panel = self._create_datasets_panel()
        splitter.addWidget(datasets_panel)
        
        splitter.setSizes([200, 400])
        layout.addWidget(splitter)
        
        # Action buttons
        btn_row = QHBoxLayout()
        
        self._download_btn = QPushButton("Download Selected")
        self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._download_selected)
        btn_row.addWidget(self._download_btn)
        
        self._use_as_client_btn = QPushButton("Use as Client Dataset")
        self._use_as_client_btn.setEnabled(False)
        self._use_as_client_btn.clicked.connect(self._use_as_client)
        btn_row.addWidget(self._use_as_client_btn)
        
        btn_row.addStretch()
        
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        btn_row.addWidget(self._refresh_btn)
        
        layout.addLayout(btn_row)
        
        # Progress bar
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)
    
    def _create_groups_panel(self) -> QWidget:
        """Create the groups list panel."""
        panel = QGroupBox("Groups")
        layout = QVBoxLayout(panel)
        
        self._groups_list = QListWidget()
        self._groups_list.itemClicked.connect(self._on_group_selected)
        layout.addWidget(self._groups_list)
        
        return panel
    
    def _create_datasets_panel(self) -> QWidget:
        """Create the datasets table panel."""
        panel = QGroupBox("Datasets")
        layout = QVBoxLayout(panel)
        
        self._datasets_table = QTableWidget()
        self._datasets_table.setColumnCount(5)
        self._datasets_table.setHorizontalHeaderLabels([
            "Name", "Format", "Size", "Owner", "Modified"
        ])
        self._datasets_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._datasets_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._datasets_table.itemSelectionChanged.connect(self._on_dataset_selection_changed)
        layout.addWidget(self._datasets_table)
        
        return panel
    
    def set_client(self, client: GISimpleClient):
        """Set the GISimple client."""
        self._client = client
        self._update_auth_status()
    
    def _update_auth_status(self):
        """Update the authentication status display."""
        if self._client.is_authenticated:
            user = self._client.user
            self._auth_status.setText(f"Connected as: {user.display_name}")
            self._auth_status.setStyleSheet("color: #2E7D32;")
        else:
            self._auth_status.setText("Not connected to GISimple")
            self._auth_status.setStyleSheet("color: #666;")
    
    def refresh(self):
        """Refresh groups and datasets."""
        self._update_auth_status()
        
        if not self._client.is_authenticated:
            self._groups_list.clear()
            self._datasets_table.setRowCount(0)
            return
        
        try:
            # Load groups
            groups = self._client.get_groups()
            self._groups_list.clear()
            
            for group in groups:
                item = QListWidgetItem(group.get('name', 'Unknown'))
                item.setData(Qt.ItemDataRole.UserRole, group.get('id'))
                self._groups_list.addItem(item)
            
            # Clear datasets
            self._datasets_table.setRowCount(0)
            self._current_group_id = None
            
        except GISimpleServerError as e:
            from ..ui.widgets import show_error_dialog
            show_error_dialog(
                self, "Error",
                get_user_friendly_message(e.code),
                e.details
            )
    
    def _on_group_selected(self, item: QListWidgetItem):
        """Handle group selection."""
        group_id = item.data(Qt.ItemDataRole.UserRole)
        if not group_id:
            return
        
        self._current_group_id = group_id
        self._load_datasets(group_id)
    
    def _load_datasets(self, group_id: str):
        """Load datasets for a group."""
        try:
            self._datasets = self._client.get_group_datasets(group_id)
            self._datasets_table.setRowCount(0)
            
            for dataset in self._datasets:
                row = self._datasets_table.rowCount()
                self._datasets_table.insertRow(row)
                
                self._datasets_table.setItem(row, 0, QTableWidgetItem(dataset.name))
                self._datasets_table.setItem(row, 1, QTableWidgetItem(dataset.format))
                self._datasets_table.setItem(row, 2, QTableWidgetItem(
                    self._format_size(dataset.size)
                ))
                self._datasets_table.setItem(row, 3, QTableWidgetItem(dataset.owner))
                self._datasets_table.setItem(row, 4, QTableWidgetItem(dataset.last_modified))
                
        except GISimpleServerError as e:
            from ..ui.widgets import show_error_dialog
            show_error_dialog(
                self, "Error",
                get_user_friendly_message(e.code),
                e.details
            )
    
    def _format_size(self, size: int) -> str:
        """Format file size for display."""
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size // 1024} KB"
        else:
            return f"{size // (1024 * 1024)} MB"
    
    def _on_dataset_selection_changed(self):
        """Handle dataset selection change."""
        selected = self._datasets_table.selectedItems()
        has_selection = len(selected) > 0
        
        self._download_btn.setEnabled(has_selection)
        self._use_as_client_btn.setEnabled(has_selection)
        
        if has_selection:
            row = selected[0].row()
            if row < len(self._datasets):
                self.datasetSelected.emit(self._datasets[row])
    
    def _get_selected_datasets(self) -> List[GISimpleDataset]:
        """Get list of selected datasets."""
        selected_rows = set()
        for item in self._datasets_table.selectedItems():
            selected_rows.add(item.row())
        
        return [self._datasets[row] for row in selected_rows if row < len(self._datasets)]
    
    def _download_selected(self):
        """Download selected datasets."""
        datasets = self._get_selected_datasets()
        if not datasets:
            return
        
        self._progress.setVisible(True)
        self._progress.setRange(0, len(datasets))
        
        for i, dataset in enumerate(datasets):
            self._progress.setValue(i)
            
            try:
                path = self._client.download_dataset(dataset)
                self.datasetDownloaded.emit(str(path))
            except GISimpleServerError as e:
                from ..ui.widgets import show_error_dialog
                show_error_dialog(
                    self, "Download Failed",
                    f"Failed to download {dataset.name}",
                    e.details
                )
        
        self._progress.setValue(len(datasets))
        self._progress.setVisible(False)
    
    def _use_as_client(self):
        """Use selected dataset as client dataset for versioning."""
        datasets = self._get_selected_datasets()
        if not datasets:
            return
        
        # Download first, then open versioning dialog
        dataset = datasets[0]
        
        try:
            self._progress.setVisible(True)
            self._progress.setRange(0, 0)  # Indeterminate
            
            path = self._client.download_dataset(dataset)
            
            self._progress.setVisible(False)
            
            # Open versioning dialog with this dataset
            from ..ui.versioning_dialog import VersioningDialog
            dialog = VersioningDialog(self)
            # TODO: Pre-load the downloaded dataset as client
            dialog.exec()
            
        except GISimpleServerError as e:
            self._progress.setVisible(False)
            from ..ui.widgets import show_error_dialog
            show_error_dialog(
                self, "Download Failed",
                f"Failed to download {dataset.name}",
                e.details
            )
