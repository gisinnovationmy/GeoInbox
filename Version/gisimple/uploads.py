"""GeoInbox - GISimple Uploads Widget

Widget for browsing and downloading uploaded files from GISimple.
"""

from typing import Optional, List, Dict, Any
from datetime import datetime

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QPushButton, QLabel, QProgressBar, QGroupBox, QComboBox
)
from qgis.PyQt.QtCore import Qt, pyqtSignal

from .client import GISimpleClient
from ..utils.errors import GISimpleServerError, get_user_friendly_message


class GISimpleUploadsWidget(QWidget):
    """Widget for browsing uploaded files from GISimple."""
    
    fileDownloaded = pyqtSignal(str)
    fileSelected = pyqtSignal(dict)
    
    def __init__(self, client: Optional[GISimpleClient] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._client = client or GISimpleClient()
        self._files: List[Dict[str, Any]] = []
        self._groups: List[Dict[str, Any]] = []
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        
        self._auth_status = QLabel("Not connected to GISimple")
        self._auth_status.setStyleSheet("color: #666;")
        layout.addWidget(self._auth_status)
        
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Group:"))
        self._group_filter = QComboBox()
        self._group_filter.addItem("All Groups", None)
        self._group_filter.currentIndexChanged.connect(self._on_group_filter_changed)
        filter_row.addWidget(self._group_filter)
        filter_row.addStretch()
        layout.addLayout(filter_row)
        
        files_panel = self._create_files_panel()
        layout.addWidget(files_panel)
        
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
        
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)
    
    def _create_files_panel(self) -> QWidget:
        panel = QGroupBox("Uploaded Files")
        layout = QVBoxLayout(panel)
        
        self._files_table = QTableWidget()
        self._files_table.setColumnCount(5)
        self._files_table.setHorizontalHeaderLabels([
            "Filename", "Owner", "Size", "Modified", "Group"
        ])
        self._files_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._files_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._files_table.itemSelectionChanged.connect(self._on_file_selection_changed)
        layout.addWidget(self._files_table)
        
        return panel
    
    def set_client(self, client: GISimpleClient):
        self._client = client
        self._update_auth_status()
    
    def _update_auth_status(self):
        if self._client.is_authenticated:
            user = self._client.user
            self._auth_status.setText(f"Connected as: {user.display_name}")
            self._auth_status.setStyleSheet("color: #2E7D32;")
        else:
            self._auth_status.setText("Not connected to GISimple")
            self._auth_status.setStyleSheet("color: #666;")
    
    def refresh(self):
        self._update_auth_status()
        
        if not self._client.is_authenticated:
            self._files_table.setRowCount(0)
            self._group_filter.clear()
            self._group_filter.addItem("All Groups", None)
            return
        
        try:
            self._groups = self._client.get_upload_groups()
            self._group_filter.clear()
            self._group_filter.addItem("All Groups", None)
            for group in self._groups:
                self._group_filter.addItem(group.get('name', 'Unknown'), group.get('id'))
            
            self._files = self._client.get_uploaded_files()
            self._populate_files_table()
            
        except GISimpleServerError as e:
            from ..ui.widgets import show_error_dialog
            show_error_dialog(
                self, "Error",
                get_user_friendly_message(e.code),
                e.details
            )
    
    def _populate_files_table(self, filter_group_id: Optional[str] = None):
        self._files_table.setRowCount(0)
        
        for file_info in self._files:
            if filter_group_id and file_info.get('groupId') != filter_group_id:
                continue
            
            row = self._files_table.rowCount()
            self._files_table.insertRow(row)
            
            self._files_table.setItem(row, 0, QTableWidgetItem(file_info.get('filename', '')))
            self._files_table.setItem(row, 1, QTableWidgetItem(file_info.get('owner', '')))
            self._files_table.setItem(row, 2, QTableWidgetItem(self._format_size(file_info.get('size', 0))))
            
            modified = file_info.get('modified', '')
            if modified:
                try:
                    dt = datetime.fromisoformat(modified.replace('Z', '+00:00'))
                    modified = dt.strftime('%Y-%m-%d %H:%M')
                except (TypeError, ValueError, OverflowError):
                    pass
            self._files_table.setItem(row, 3, QTableWidgetItem(modified))
            
            group_name = ''
            group_id = file_info.get('groupId')
            if group_id:
                for g in self._groups:
                    if g.get('id') == group_id:
                        group_name = g.get('name', '')
                        break
            self._files_table.setItem(row, 4, QTableWidgetItem(group_name))
    
    def _format_size(self, size: int) -> str:
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size // 1024} KB"
        else:
            return f"{size // (1024 * 1024)} MB"
    
    def _on_group_filter_changed(self, index: int):
        group_id = self._group_filter.itemData(index)
        self._populate_files_table(group_id)
    
    def _on_file_selection_changed(self):
        selected = self._files_table.selectedItems()
        has_selection = len(selected) > 0
        
        self._download_btn.setEnabled(has_selection)
        self._use_as_client_btn.setEnabled(has_selection)
        
        if has_selection:
            row = selected[0].row()
            if row < len(self._files):
                self.fileSelected.emit(self._files[row])
    
    def _get_selected_files(self) -> List[Dict[str, Any]]:
        selected_rows = set()
        for item in self._files_table.selectedItems():
            selected_rows.add(item.row())
        
        return [self._files[row] for row in selected_rows if row < len(self._files)]
    
    def _download_selected(self):
        files = self._get_selected_files()
        if not files:
            return
        
        self._progress.setVisible(True)
        self._progress.setRange(0, len(files))
        
        for i, file_info in enumerate(files):
            self._progress.setValue(i)
            
            try:
                path = self._client.download_uploaded_file(
                    file_info.get('owner', ''),
                    file_info.get('filename', '')
                )
                self.fileDownloaded.emit(str(path))
            except GISimpleServerError as e:
                from ..ui.widgets import show_error_dialog
                show_error_dialog(
                    self, "Download Failed",
                    f"Failed to download {file_info.get('filename', '')}",
                    e.details
                )
        
        self._progress.setValue(len(files))
        self._progress.setVisible(False)
    
    def _use_as_client(self):
        files = self._get_selected_files()
        if not files:
            return
        
        file_info = files[0]
        
        try:
            self._progress.setVisible(True)
            self._progress.setRange(0, 0)
            
            path = self._client.download_uploaded_file(
                file_info.get('owner', ''),
                file_info.get('filename', '')
            )
            
            self._progress.setVisible(False)
            
            from ..ui.versioning_dialog import VersioningDialog
            dialog = VersioningDialog(self)
            dialog.exec()
            
        except GISimpleServerError as e:
            self._progress.setVisible(False)
            from ..ui.widgets import show_error_dialog
            show_error_dialog(
                self, "Download Failed",
                f"Failed to download {file_info.get('filename', '')}",
                e.details
            )
