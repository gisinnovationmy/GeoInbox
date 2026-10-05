"""
GeoInbox - Reusable UI Widgets

Common UI components used across the plugin.
"""

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QFrame, QSizePolicy, QToolButton, QMenu,
    QLineEdit, QComboBox, QCheckBox, QSpinBox, QGroupBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QMessageBox, QInputDialog
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QSize
from qgis.PyQt.QtGui import QIcon, QFont, QColor

from typing import Optional, List, Dict, Any, Callable


class StatusIndicator(QWidget):
    """A colored status indicator dot with label."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._setup_ui()
        self._status = 'disconnected'

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._dot = QLabel()
        self._dot.setFixedSize(12, 12)
        self._dot.setStyleSheet(self._get_dot_style('#9E9E9E'))
        layout.addWidget(self._dot)

        self._label = QLabel('Disconnected')
        layout.addWidget(self._label)

    def _get_dot_style(self, color: str) -> str:
        return f"""
            background-color: {color};
            border-radius: 6px;
            border: 1px solid rgba(0,0,0,0.2);
        """

    def set_status(self, status: str, label: Optional[str] = None):
        """Set the status indicator."""
        self._status = status
        
        colors = {
            'connected': '#4CAF50',
            'connecting': '#FFC107',
            'disconnected': '#9E9E9E',
            'error': '#F44336'
        }
        
        labels = {
            'connected': 'Connected',
            'connecting': 'Connecting...',
            'disconnected': 'Disconnected',
            'error': 'Error'
        }
        
        color = colors.get(status, '#9E9E9E')
        self._dot.setStyleSheet(self._get_dot_style(color))
        self._label.setText(label or labels.get(status, status))


class ProgressWidget(QWidget):
    """A progress bar with label and cancel button."""

    cancelled = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Label row
        label_row = QHBoxLayout()
        self._label = QLabel('Processing...')
        label_row.addWidget(self._label)
        label_row.addStretch()
        
        self._cancel_btn = QPushButton('Cancel')
        self._cancel_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._cancel_btn.clicked.connect(self.cancelled.emit)
        label_row.addWidget(self._cancel_btn)
        layout.addLayout(label_row)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)

    def set_label(self, text: str):
        """Set the progress label."""
        self._label.setText(text)

    def set_progress(self, value: int, maximum: int = 100):
        """Set the progress value."""
        self._progress.setRange(0, maximum)
        self._progress.setValue(value)

    def set_indeterminate(self, indeterminate: bool = True):
        """Set indeterminate mode."""
        if indeterminate:
            self._progress.setRange(0, 0)
        else:
            self._progress.setRange(0, 100)

    def reset(self):
        """Reset the progress widget."""
        self._label.setText('Processing...')
        self._progress.setRange(0, 100)
        self._progress.setValue(0)


class SearchBox(QWidget):
    """A search input with clear button."""

    searchChanged = pyqtSignal(str)
    searchSubmitted = pyqtSignal(str)

    def __init__(self, placeholder: str = 'Search...', parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._setup_ui(placeholder)

    def _setup_ui(self, placeholder: str):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._input = QLineEdit()
        self._input.setPlaceholderText(placeholder)
        self._input.textChanged.connect(self.searchChanged.emit)
        self._input.returnPressed.connect(lambda: self.searchSubmitted.emit(self._input.text()))
        layout.addWidget(self._input)

        self._clear_btn = QToolButton()
        self._clear_btn.setText('×')
        self._clear_btn.setFixedSize(24, 24)
        self._clear_btn.clicked.connect(self.clear)
        self._clear_btn.setVisible(False)
        layout.addWidget(self._clear_btn)

        self._input.textChanged.connect(
            lambda t: self._clear_btn.setVisible(bool(t))
        )

    def text(self) -> str:
        """Get the search text."""
        return self._input.text()

    def clear(self):
        """Clear the search box."""
        self._input.clear()

    def setPlaceholderText(self, text: str):
        """Set placeholder text."""
        self._input.setPlaceholderText(text)


class FilterComboBox(QComboBox):
    """A combo box for filtering with 'All' option."""

    filterChanged = pyqtSignal(object)  # Emits None for 'All', or the selected value

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.currentIndexChanged.connect(self._on_index_changed)

    def set_options(self, options: List[tuple], include_all: bool = True):
        """
        Set filter options.
        
        Args:
            options: List of (label, value) tuples
            include_all: Whether to include 'All' option
        """
        self.clear()
        
        if include_all:
            self.addItem('All', None)
        
        for label, value in options:
            self.addItem(label, value)

    def _on_index_changed(self, index: int):
        value = self.itemData(index)
        self.filterChanged.emit(value)

    def get_value(self):
        """Get the current filter value."""
        return self.currentData()


class TrustBadge(QLabel):
    """A badge showing trust status."""

    def __init__(self, trusted: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.set_trusted(trusted)

    def set_trusted(self, trusted: bool):
        """Set the trust status."""
        if trusted:
            self.setText('✓ Trusted')
            self.setStyleSheet(
                'background-color: #E8F5E9; color: #2E7D32;'
                ' padding: 2px 6px; border-radius: 3px; font-size: 11px;'
            )
        else:
            self.setText('⚠ Untrusted')
            self.setStyleSheet(
                'background-color: #FFF3E0; color: #E65100;'
                ' padding: 2px 6px; border-radius: 3px; font-size: 11px;'
            )


class AttachmentItem(QWidget):
    """A widget representing an attachment."""

    downloadClicked = pyqtSignal(str)  # attachment_id
    openClicked = pyqtSignal(str)  # attachment_id

    def __init__(
        self,
        attachment_id: str,
        filename: str,
        size: int,
        content_type: str,
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self._attachment_id = attachment_id
        self._filename = filename
        self._size = size
        self._content_type = content_type
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        # Icon based on type
        icon_label = QLabel()
        icon = self._get_icon_for_type()
        icon_label.setText(icon)
        icon_label.setFixedWidth(24)
        layout.addWidget(icon_label)

        # File info
        info_layout = QVBoxLayout()
        info_layout.setSpacing(0)

        name_label = QLabel(self._filename)
        name_label.setStyleSheet('font-weight: bold;')
        info_layout.addWidget(name_label)

        size_label = QLabel(self._format_size(self._size))
        size_label.setStyleSheet('color: #666; font-size: 11px;')
        info_layout.addWidget(size_label)
        
        layout.addLayout(info_layout)
        layout.addStretch()

        # Buttons
        download_btn = QPushButton('Download')
        download_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        download_btn.clicked.connect(lambda: self.downloadClicked.emit(self._attachment_id))
        layout.addWidget(download_btn)

        open_btn = QPushButton('Open')
        open_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        open_btn.clicked.connect(lambda: self.openClicked.emit(self._attachment_id))
        layout.addWidget(open_btn)

    def _get_icon_for_type(self) -> str:
        """Get an emoji icon for the content type."""
        ext = self._filename.lower().split('.')[-1] if '.' in self._filename else ''
        
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
        """Format file size for display."""
        if size < 1024:
            return f'{size} B'
        elif size < 1024 * 1024:
            return f'{size / 1024:.1f} KB'
        else:
            return f'{size / (1024 * 1024):.1f} MB'


class MessageListTable(QTableWidget):
    """A table widget for displaying email messages."""

    messageSelected = pyqtSignal(str)  # message_id
    messageDoubleClicked = pyqtSignal(str)  # message_id

    COLUMNS = ['From', 'Subject', 'Date', 'Size', 'Read', 'Trusted']

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._setup_ui()
        self._messages: Dict[int, str] = {}  # row -> message_id

    def _setup_ui(self):
        self.setColumnCount(len(self.COLUMNS))
        self.setHorizontalHeaderLabels(self.COLUMNS)
        
        # Configure header
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        
        self.setColumnWidth(0, 180)
        
        # Configure selection
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)
        
        # Hide vertical header
        self.verticalHeader().setVisible(False)
        
        # Connect signals
        self.itemSelectionChanged.connect(self._on_selection_changed)
        self.cellDoubleClicked.connect(self._on_double_click)

    def set_messages(self, messages: List[Dict[str, Any]]):
        """
        Set the messages to display.
        
        Args:
            messages: List of message dictionaries
        """
        self.setRowCount(0)
        self._messages.clear()
        
        for msg in messages:
            row = self.rowCount()
            self.insertRow(row)
            
            message_id = msg.get('message_id', '')
            self._messages[row] = message_id
            
            # From
            from_item = QTableWidgetItem(msg.get('from_address', ''))
            self.setItem(row, 0, from_item)
            
            # Subject
            subject_item = QTableWidgetItem(msg.get('subject', ''))
            if not msg.get('is_read', True):
                font = subject_item.font()
                font.setBold(True)
                subject_item.setFont(font)
            self.setItem(row, 1, subject_item)
            
            # Date
            date = msg.get('date')
            if isinstance(date, str):
                date_str = date[:10] if len(date) > 10 else date
            elif hasattr(date, 'strftime'):
                date_str = date.strftime('%Y-%m-%d')
            else:
                date_str = ''
            date_item = QTableWidgetItem(date_str)
            self.setItem(row, 2, date_item)
            
            # Size
            size = msg.get('size', 0)
            size_str = self._format_size(size)
            size_item = QTableWidgetItem(size_str)
            self.setItem(row, 3, size_item)
            
            # Read
            read_item = QTableWidgetItem('✓' if msg.get('is_read') else '')
            read_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setItem(row, 4, read_item)
            
            # Trusted
            trusted_item = QTableWidgetItem('✓' if msg.get('is_trusted') else '⚠')
            trusted_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if msg.get('is_trusted'):
                trusted_item.setForeground(QColor('#2E7D32'))
            else:
                trusted_item.setForeground(QColor('#E65100'))
            self.setItem(row, 5, trusted_item)

    def _format_size(self, size: int) -> str:
        """Format file size."""
        if size < 1024:
            return f'{size} B'
        elif size < 1024 * 1024:
            return f'{size // 1024} KB'
        else:
            return f'{size // (1024 * 1024)} MB'

    def _on_selection_changed(self):
        """Handle selection change."""
        rows = set(item.row() for item in self.selectedItems())
        if len(rows) == 1:
            row = list(rows)[0]
            message_id = self._messages.get(row)
            if message_id:
                self.messageSelected.emit(message_id)

    def _on_double_click(self, row: int, column: int):
        """Handle double click."""
        message_id = self._messages.get(row)
        if message_id:
            self.messageDoubleClicked.emit(message_id)

    def get_selected_message_ids(self) -> List[str]:
        """Get list of selected message IDs."""
        rows = set(item.row() for item in self.selectedItems())
        return [self._messages[row] for row in rows if row in self._messages]


class MailboxList(QWidget):
    """A list widget for mailboxes."""

    mailboxSelected = pyqtSignal(str)  # mailbox name

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._setup_ui()
        self._mailboxes: List[Dict[str, Any]] = []

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._container = QVBoxLayout()
        self._container.setSpacing(2)
        layout.addLayout(self._container)
        layout.addStretch()

    def set_mailboxes(self, mailboxes: List[Dict[str, Any]]):
        """Set the mailboxes to display."""
        # Clear existing
        while self._container.count():
            item = self._container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        self._mailboxes = mailboxes
        
        for mb in mailboxes:
            btn = QPushButton()
            name = mb.get('name', '')
            unread = mb.get('unread_count', 0)
            
            if unread > 0:
                btn.setText(f"{name} ({unread})")
                # btn.setStyleSheet('font-weight: bold;')
            else:
                btn.setText(name)
            
            btn.setFlat(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, n=name: self.mailboxSelected.emit(n))
            
            self._container.addWidget(btn)

    def highlight_mailbox(self, name: str):
        """Highlight the selected mailbox button."""
        for i in range(self._container.count()):
            widget = self._container.itemAt(i).widget()
            if isinstance(widget, QPushButton):
                if name in widget.text():
                    widget.setStyleSheet(
                        'QPushButton { background-color: palette(highlight);'
                        ' color: palette(highlighted-text); font-weight: bold; }'
                    )
                else:
                    widget.setStyleSheet('')


def show_error_dialog(parent: QWidget, title: str, message: str, details: Optional[str] = None):
    """Show an error dialog."""
    msg_box = QMessageBox(parent)
    msg_box.setIcon(QMessageBox.Icon.Critical)
    msg_box.setWindowTitle(title)
    msg_box.setText(message)
    if details:
        msg_box.setDetailedText(details)
    msg_box.exec()


def show_warning_dialog(parent: QWidget, title: str, message: str) -> bool:
    """Show a warning dialog with Yes/No buttons. Returns True if Yes clicked."""
    result = QMessageBox.warning(
        parent,
        title,
        message,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No
    )
    return result == QMessageBox.StandardButton.Yes


def show_info_dialog(parent: QWidget, title: str, message: str):
    """Show an info dialog."""
    QMessageBox.information(parent, title, message)
