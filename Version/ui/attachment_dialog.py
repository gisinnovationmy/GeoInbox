"""
GeoInbox - Attachment Selection Dialog

Dialog for selecting and downloading email attachments.
"""

from pathlib import Path
from typing import Optional, List, Dict, Any, Callable

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QCheckBox, QGroupBox, QScrollArea, QWidget, QTextBrowser,
    QProgressBar, QDialogButtonBox, QFileDialog, QSplitter,
    QSizePolicy
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QThread, QObject
from qgis.core import QgsProject

from .widgets import show_error_dialog, show_info_dialog
from ..adapters.base import MessageMetadata, AttachmentMetadata
from ..storage.sanitizer import (
    get_attachment_storage_path, ensure_parent_dir,
    set_user_only_permissions, resolve_collision
)
from ..storage.extractor import (
    extract_zip, validate_gis_file, get_file_preview,
    is_allowed_extension
)


class DownloadWorker(QObject):
    """Worker for downloading attachments in background."""
    
    progress = pyqtSignal(int, int, str)  # current, total, message
    finished = pyqtSignal(list)  # list of downloaded paths
    error = pyqtSignal(str)
    
    def __init__(
        self,
        adapter,
        message: MessageMetadata,
        attachments: List[AttachmentMetadata],
        output_dir: Path
    ):
        super().__init__()
        self._adapter = adapter
        self._message = message
        self._attachments = attachments
        self._output_dir = output_dir
        self._cancelled = False
    
    def cancel(self):
        self._cancelled = True
    
    def run(self):
        """Download all selected attachments."""
        downloaded = []
        total = len(self._attachments)
        
        for i, attachment in enumerate(self._attachments):
            if self._cancelled:
                break
            
            self.progress.emit(i, total, f"Downloading {attachment.filename}...")
            
            try:
                # Determine output path
                output_path = get_attachment_storage_path(
                    sender=self._message.from_address,
                    message_id=self._message.message_id,
                    filename=attachment.filename,
                    message_date=self._message.date
                )
                
                ensure_parent_dir(output_path)
                output_path = resolve_collision(output_path)
                
                # Download attachment
                with open(output_path, 'wb') as f:
                    self._adapter.fetch_attachment(
                        self._message.message_id,
                        attachment.attachment_id,
                        lambda chunk: f.write(chunk)
                    )
                
                set_user_only_permissions(output_path)
                downloaded.append(str(output_path))
                
            except Exception as e:
                self.error.emit(f"Failed to download {attachment.filename}: {e}")
        
        self.progress.emit(total, total, "Complete")
        self.finished.emit(downloaded)


class AttachmentItem(QWidget):
    """Widget for a single attachment with checkbox and preview."""
    
    selectionChanged = pyqtSignal(bool)
    previewRequested = pyqtSignal()
    
    def __init__(
        self,
        attachment: AttachmentMetadata,
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self._attachment = attachment
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        
        # Checkbox
        self._checkbox = QCheckBox()
        self._checkbox.stateChanged.connect(
            lambda state: self.selectionChanged.emit(state == Qt.CheckState.Checked)
        )
        layout.addWidget(self._checkbox)
        
        # Icon
        icon = self._get_icon()
        icon_label = QLabel(icon)
        icon_label.setFixedWidth(24)
        layout.addWidget(icon_label)
        
        # File info
        info_layout = QVBoxLayout()
        info_layout.setSpacing(0)
        
        name_label = QLabel(self._attachment.filename)
        # name_label.setStyleSheet("font-weight: bold;")
        info_layout.addWidget(name_label)
        
        details = f"{self._format_size(self._attachment.size)} • {self._attachment.content_type}"
        details_label = QLabel(details)
        # details_label.setStyleSheet("color: #666; font-size: 11px;")
        info_layout.addWidget(details_label)
        
        layout.addLayout(info_layout)
        layout.addStretch()
        
        # Preview button
        preview_btn = QPushButton("Preview")
        preview_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        preview_btn.clicked.connect(self.previewRequested.emit)
        layout.addWidget(preview_btn)
        
        # Validation status
        self._status_label = QLabel()
        self._status_label.setFixedWidth(80)
        layout.addWidget(self._status_label)
        
        # Validate extension
        if is_allowed_extension(self._attachment.filename, top_level=True):
            self._status_label.setText("✓ Valid")
            # self._status_label.setStyleSheet("color: #2E7D32;")
            self._checkbox.setChecked(True)
        else:
            self._status_label.setText("✗ Unsupported")
            # self._status_label.setStyleSheet("color: #C62828;")
            self._checkbox.setEnabled(False)
    
    def _get_icon(self) -> str:
        """Get emoji icon for file type."""
        ext = self._attachment.filename.lower().split('.')[-1] if '.' in self._attachment.filename else ''
        icons = {
            'geojson': '🗺️',
            'json': '🗺️',
            'gpkg': '📦',
            'kml': '🌍',
            'kmz': '🌍',
            'shp': '📐',
            'zip': '📁',
        }
        return icons.get(ext, '📎')
    
    def _format_size(self, size: int) -> str:
        """Format file size."""
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size // 1024} KB"
        else:
            return f"{size // (1024 * 1024)} MB"
    
    @property
    def attachment(self) -> AttachmentMetadata:
        return self._attachment
    
    @property
    def is_selected(self) -> bool:
        return self._checkbox.isChecked()
    
    def set_selected(self, selected: bool):
        self._checkbox.setChecked(selected)


class AttachmentDialog(QDialog):
    """
    Dialog for selecting and downloading email attachments.
    
    Features:
    - Checkbox selection for each attachment
    - File preview (GeoJSON/KML text, GPKG layers)
    - Download to configured directory
    - Option to open in QGIS after download
    """
    
    def __init__(
        self,
        adapter,
        message: MessageMetadata,
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self._adapter = adapter
        self._message = message
        self._attachment_items: List[AttachmentItem] = []
        self._downloaded_paths: List[str] = []
        self._worker: Optional[DownloadWorker] = None
        self._thread: Optional[QThread] = None
        
        self._setup_ui()
    
    def _setup_ui(self):
        self.setWindowTitle(f"Attachments - {self._message.subject}")
        self.setMinimumSize(600, 400)
        
        layout = QVBoxLayout(self)
        
        # Message info
        info_group = QGroupBox("Message")
        info_layout = QVBoxLayout(info_group)
        info_layout.addWidget(QLabel(f"<b>From:</b> {self._message.from_address}"))
        info_layout.addWidget(QLabel(f"<b>Subject:</b> {self._message.subject}"))
        info_layout.addWidget(QLabel(f"<b>Date:</b> {self._message.date}"))
        layout.addWidget(info_group)
        
        # Splitter for attachments and preview
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Attachments list
        attachments_group = QGroupBox(f"Attachments ({len(self._message.attachments)})")
        attachments_layout = QVBoxLayout(attachments_group)
        
        # Select all / none buttons
        btn_row = QHBoxLayout()
        btn_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        select_all_btn = QPushButton("Select All")
        select_all_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        select_all_btn.clicked.connect(self._select_all)
        btn_row.addWidget(select_all_btn)
        
        select_none_btn = QPushButton("Select None")
        select_none_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        select_none_btn.clicked.connect(self._select_none)
        btn_row.addWidget(select_none_btn)
        attachments_layout.addLayout(btn_row)
        
        # Scrollable attachment list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(2)
        
        for attachment in self._message.attachments:
            item = AttachmentItem(attachment)
            item.previewRequested.connect(
                lambda a=attachment: self._show_preview(a)
            )
            self._attachment_items.append(item)
            scroll_layout.addWidget(item)
        
        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        attachments_layout.addWidget(scroll)
        
        splitter.addWidget(attachments_group)
        
        # Preview panel
        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_group)
        
        self._preview_browser = QTextBrowser()
        self._preview_browser.setPlaceholderText("Select an attachment to preview")
        preview_layout.addWidget(self._preview_browser)
        
        splitter.addWidget(preview_group)
        splitter.setSizes([300, 300])
        
        layout.addWidget(splitter)
        
        # Progress bar
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)
        
        # Action buttons
        btn_layout = QHBoxLayout()
        btn_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        
        self._download_btn = QPushButton("Download Selected")
        self._download_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._download_btn.clicked.connect(self._download_selected)
        btn_layout.addWidget(self._download_btn)
        
        self._download_open_btn = QPushButton("Download && Open in QGIS")
        self._download_open_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._download_open_btn.clicked.connect(self._download_and_open)
        btn_layout.addWidget(self._download_open_btn)
        
        close_btn = QPushButton("Close")
        close_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        
        layout.addLayout(btn_layout)
    
    def _select_all(self):
        """Select all valid attachments."""
        for item in self._attachment_items:
            if is_allowed_extension(item.attachment.filename, top_level=True):
                item.set_selected(True)
    
    def _select_none(self):
        """Deselect all attachments."""
        for item in self._attachment_items:
            item.set_selected(False)
    
    def _get_selected_attachments(self) -> List[AttachmentMetadata]:
        """Get list of selected attachments."""
        return [item.attachment for item in self._attachment_items if item.is_selected]
    
    def _show_preview(self, attachment: AttachmentMetadata):
        """Show preview for an attachment."""
        # For now, show basic info
        # Full preview would require downloading first
        preview = f"<h3>{attachment.filename}</h3>"
        preview += f"<p><b>Size:</b> {attachment.size} bytes</p>"
        preview += f"<p><b>Type:</b> {attachment.content_type}</p>"
        
        ext = attachment.filename.lower().split('.')[-1] if '.' in attachment.filename else ''
        
        if ext in {'geojson', 'json', 'kml'}:
            preview += "<p><i>Text preview available after download</i></p>"
        elif ext == 'gpkg':
            preview += "<p><i>Layer list available after download</i></p>"
        elif ext == 'zip':
            preview += "<p><i>Contents will be extracted after download</i></p>"
        elif ext == 'shp':
            preview += "<p><i>Shapefile components will be validated</i></p>"
        
        self._preview_browser.setHtml(preview)
    
    def _download_selected(self):
        """Download selected attachments."""
        self._start_download(open_after=False)
    
    def _download_and_open(self):
        """Download and open in QGIS."""
        self._start_download(open_after=True)
    
    def _start_download(self, open_after: bool):
        """Start the download process."""
        selected = self._get_selected_attachments()
        if not selected:
            show_info_dialog(self, "No Selection", "Please select attachments to download.")
            return
        
        # Get output directory
        from ..storage.sanitizer import get_email_attachments_dir
        output_dir = get_email_attachments_dir()
        
        # Disable buttons during download
        self._download_btn.setEnabled(False)
        self._download_open_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setRange(0, len(selected))
        
        # Create worker and thread
        self._thread = QThread()
        self._worker = DownloadWorker(
            self._adapter,
            self._message,
            selected,
            output_dir
        )
        self._worker.moveToThread(self._thread)
        
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(lambda paths: self._on_download_complete(paths, open_after))
        self._worker.error.connect(self._on_download_error)
        
        self._thread.start()
    
    def _on_progress(self, current: int, total: int, message: str):
        """Handle progress update."""
        self._progress.setValue(current)
        self._progress.setFormat(message)
    
    def _on_download_complete(self, paths: List[str], open_after: bool):
        """Handle download completion."""
        self._cleanup_worker()
        
        self._download_btn.setEnabled(True)
        self._download_open_btn.setEnabled(True)
        self._progress.setVisible(False)
        
        self._downloaded_paths = paths
        
        if not paths:
            show_error_dialog(self, "Download Failed", "No files were downloaded.")
            return
        
        # Extract zips if configured
        from ..storage.database import DatabaseManager
        db = DatabaseManager()
        auto_extract = db.get_setting('auto_extract', 'true') == 'true'
        
        extracted_paths = []
        for path in paths:
            if path.lower().endswith('.zip') and auto_extract:
                result = extract_zip(
                    Path(path),
                    Path(path).parent,
                    recursive=True,
                    validate_shapefiles=db.get_setting('validate_shapefiles', 'true') == 'true'
                )
                extracted_paths.extend([str(f.path) for f in result.extracted_files])
            else:
                extracted_paths.append(path)
        
        if open_after:
            self._open_in_qgis(extracted_paths)
        
        show_info_dialog(
            self,
            "Download Complete",
            f"Downloaded {len(paths)} file(s).\n"
            f"Extracted {len(extracted_paths)} file(s)."
        )
    
    def _on_download_error(self, message: str):
        """Handle download error."""
        show_error_dialog(self, "Download Error", message)
    
    def _open_in_qgis(self, paths: List[str]):
        """Open downloaded files in QGIS."""
        for path in paths:
            ext = path.lower().split('.')[-1]
            
            if ext in {'geojson', 'json', 'gpkg', 'kml', 'shp'}:
                try:
                    from qgis.core import QgsVectorLayer
                    layer = QgsVectorLayer(path, Path(path).stem, "ogr")
                    if layer.isValid():
                        QgsProject.instance().addMapLayer(layer)
                except Exception as e:
                    show_error_dialog(
                        self,
                        "Failed to Open",
                        f"Could not open {Path(path).name}: {e}"
                    )
    
    def _cleanup_worker(self):
        """Cleanup worker thread."""
        if self._thread:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
        self._worker = None
    
    def closeEvent(self, event):
        """Handle dialog close."""
        if self._worker:
            self._worker.cancel()
        self._cleanup_worker()
        super().closeEvent(event)
    
    def get_downloaded_paths(self) -> List[str]:
        """Get list of downloaded file paths."""
        return self._downloaded_paths
