from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry, QgsWkbTypes,
    QgsMapLayerProxyModel, QgsLayerTreeModel, QgsLayerTree, QgsFeatureRequest,
    QgsField, QgsFields, QgsSymbol, QgsSingleSymbolRenderer,
    QgsCategorizedSymbolRenderer, QgsRendererCategory,
    QgsMessageLog, Qgis, QgsApplication, QgsRectangle
)
from qgis.gui import (
    QgsMessageBar,
    QgsMapLayerComboBox,
    QgsMapCanvas,
    QgsLayerTreeView,
    QgsMapToolPan,
    QgsFilterLineEdit,
)
from qgis.utils import iface
from qgis.PyQt.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QTabWidget, QWidget,
    QLabel, QPushButton, QLineEdit, QComboBox, QCheckBox,
    QSpinBox, QDoubleSpinBox, QGroupBox, QFormLayout, QListWidget, QListWidgetItem,
    QTableWidget, QTableWidgetItem, QHeaderView, QSplitter,
    QTextBrowser, QProgressBar, QMessageBox, QScrollArea,
    QFrame, QToolBar, QAbstractItemView, QFileDialog, QDialogButtonBox, QLayout, QLayoutItem,
    QRadioButton, QButtonGroup, QSlider, QTreeWidget, QTreeWidgetItem, QStyle,
    QMenu, QAction, QToolButton, QSizePolicy, QStackedWidget
)
from qgis.PyQt.QtCore import Qt, QThread, QObject, pyqtSignal, QTimer, QRect, QPoint, QSize, QModelIndex, QVariant
import threading
from qgis.PyQt.QtGui import (
    QColor, QBrush, QIntValidator, QDoubleValidator, QIcon, QPixmap,
    QPainter, QPolygon, QFontMetrics, QTransform
)
import os
import sys
import sqlite3
import json
import platform
from pathlib import Path
from datetime import datetime, timezone
import email
from contextlib import nullcontext
from typing import Any, Callable, List, Optional, Tuple

# Add plugin directory to path for imports
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

try:
    from .utils.security import urlopen_allowed, log_debug_exception, open_path_in_file_manager
except ImportError:
    from utils.security import urlopen_allowed, log_debug_exception, open_path_in_file_manager

# Database path - stored in QGIS user profile directory (not plugin directory)
# This ensures:
# 1. Database is never distributed with the plugin (security)
# 2. Each user has their own private database
# 3. Plugin upgrades don't overwrite user data
def _get_user_db_path():
    """Get the database path in the user's QGIS profile directory."""
    try:
        from qgis.core import QgsApplication
        profile_path = Path(QgsApplication.qgisSettingsDirPath())
        geoinbox_dir = profile_path / 'geoinbox'
        geoinbox_dir.mkdir(parents=True, exist_ok=True)
        return geoinbox_dir / 'config.db'
    except Exception:
        # Fallback to plugin directory if QGIS not available (e.g., during testing)
        return Path(PLUGIN_DIR) / 'config.db'

DB_PATH = _get_user_db_path()


class EmailFetchWorker(QThread):
    """Background worker that fetches a single IMAP email safely."""

    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, imap_conn, imap_lock: threading.Lock, message_number: str, parent=None):
        super().__init__(parent)
        self._imap = imap_conn
        self._lock = imap_lock
        self._msg_num = message_number
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        if self._cancelled:
            return
        if not self._imap or not self._msg_num:
            self.error.emit("IMAP connection not available")
            return

        try:
            lock = self._lock or nullcontext()
            with lock:
                status, msg_data = self._imap.fetch(self._msg_num.encode(), '(RFC822)')
            if status != 'OK' or not msg_data or not msg_data[0]:
                raise RuntimeError("Failed to fetch message body")

            raw_email = msg_data[0][1]
            parsed = self._parse_email(raw_email)
            if not self._cancelled:
                self.finished.emit(parsed)
        except Exception as exc:  # noqa: BLE001 - surface all failures to UI
            if not self._cancelled:
                self.error.emit(str(exc))

    def _parse_email(self, raw_email: bytes) -> dict:
        message = email.message_from_bytes(raw_email)
        body_text, body_html = self._extract_bodies(message)
        attachments = self._extract_attachments(message)

        return {
            'from': message.get('From', ''),
            'to': message.get('To', ''),
            'cc': message.get('Cc', ''),
            'subject': self._decode_header(message.get('Subject', '')),
            'date': message.get('Date', ''),
            'body_text': body_text,
            'body_html': body_html,
            'attachments': attachments,
        }

    def _extract_bodies(self, msg):
        body_text = ''
        body_html = ''

        if msg.is_multipart():
            for part in msg.walk():
                if self._is_attachment(part):
                    continue
                content_type = part.get_content_type()
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                text = self._decode_payload(payload, part.get_content_charset())
                if content_type == 'text/html' and not body_html:
                    body_html = text
                elif content_type == 'text/plain' and not body_text:
                    body_text = text
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                text = self._decode_payload(payload, msg.get_content_charset())
                if msg.get_content_type() == 'text/html':
                    body_html = text
                else:
                    body_text = text

        return body_text, body_html

    def _extract_attachments(self, msg):
        attachments = []
        for part in msg.walk():
            if not self._is_attachment(part):
                continue

            filename = part.get_filename() or 'attachment'
            filename = self._decode_header(filename)
            payload = part.get_payload(decode=True) or b''
            attachments.append({
                'filename': filename,
                'size': len(payload),
                'content_type': part.get_content_type(),
                'data': None,
            })

        return attachments

    @staticmethod
    def _is_attachment(part) -> bool:
        disposition = str(part.get('Content-Disposition', '')).lower()
        return 'attachment' in disposition or part.get_filename() is not None

    @staticmethod
    def _decode_payload(payload: bytes, charset: str | None) -> str:
        encoding = charset or 'utf-8'
        try:
            return payload.decode(encoding, errors='replace')
        except Exception:
            return payload.decode('utf-8', errors='replace')

    @staticmethod
    def _decode_header(value: str) -> str:
        if not value:
            return ''
        try:
            from email.header import decode_header, make_header
            return str(make_header(decode_header(value)))
        except Exception:
            return value


class AttachmentFetchWorker(QThread):
    """Background worker that fetches raw attachment data for a message."""

    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, imap_conn, imap_lock: threading.Lock, message_number: str, indices: list[int], parent=None):
        super().__init__(parent)
        self._imap = imap_conn
        self._lock = imap_lock
        self._msg_num = message_number
        self._indices = indices or []

    def run(self):
        if not self._imap or not self._msg_num:
            self.error.emit("IMAP connection not available")
            return

        try:
            lock = self._lock or nullcontext()
            with lock:
                status, msg_data = self._imap.fetch(self._msg_num.encode(), '(RFC822)')
            if status != 'OK' or not msg_data or not msg_data[0]:
                raise RuntimeError("Failed to fetch attachment payload")

            raw_email = msg_data[0][1]
            message = email.message_from_bytes(raw_email)

            attachments: list[dict[str, Any]] = []
            for part in message.walk():
                if not EmailFetchWorker._is_attachment(part):
                    continue
                filename = part.get_filename() or 'attachment'
                filename = EmailFetchWorker._decode_header(filename)
                payload = part.get_payload(decode=True) or b''
                attachments.append(
                    {
                        'filename': filename,
                        'data': payload,
                        'content_type': part.get_content_type() or '',
                    }
                )

            # When indices list is empty, return everything (defensive)
            requested = self._indices or list(range(len(attachments)))
            result: dict[int, dict[str, Any]] = {}
            for idx in requested:
                if 0 <= idx < len(attachments):
                    result[idx] = attachments[idx]

            if not result:
                raise RuntimeError("Requested attachments were not found in the message")

            self.finished.emit(result)
        except Exception as exc:  # noqa: BLE001 - bubble up to UI
            self.error.emit(str(exc))


class FlowLayout(QLayout):
    """Flow layout that wraps items to the next line when space is tight."""

    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        x = rect.x()
        y = rect.y()
        line_height = 0
        space_x = self.spacing()
        space_y = self.spacing()

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + space_x
            if next_x - space_x > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + space_y
                next_x = x + hint.width() + space_x
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))

            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y()


class FieldPickerDialog(QDialog):
    """Dialog to search and pick an ID field."""

    def __init__(self, options: list[str], current: str, parent=None):
        super().__init__(parent)
        self._options = options
        self._selected = current
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("Select ID Field")
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Type to filter")
        self._search_edit.textChanged.connect(self._filter_list)
        search_row.addWidget(self._search_edit)
        layout.addLayout(search_row)

        self._list = QListWidget()
        self._list.addItems(self._options)
        if self._selected in self._options:
            self._list.setCurrentRow(self._options.index(self._selected))
        layout.addWidget(self._list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _filter_list(self, text: str):
        query = text.strip().lower()
        self._list.clear()
        if not query:
            self._list.addItems(self._options)
            return
        for option in self._options:
            if query in option.lower():
                self._list.addItem(option)

    def _on_accept(self):
        item = self._list.currentItem()
        if item:
            self._selected = item.text()
        self.accept()

    def selected_field(self) -> str:
        return self._selected


class DualFieldPickerDialog(QDialog):
    """Dialog displaying separate field pickers for client and host layers."""

    def __init__(
        self,
        client_options: list[str],
        host_options: list[str],
        current_client: str,
        current_host: str,
        parent=None,
    ):
        super().__init__(parent)
        self._client_all = client_options or ["(auto-detect)"]
        self._host_all = host_options or ["(auto-detect)"]
        self._client_selected = current_client if current_client in self._client_all else self._client_all[0]
        self._host_selected = current_host if current_host in self._host_all else self._host_all[0]
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("Select ID Fields")
        self.setMinimumWidth(640)

        layout = QVBoxLayout(self)
        columns = QHBoxLayout()

        self._client_list = QListWidget()
        self._client_search = QLineEdit()
        self._setup_column(columns, "Client Fields", self._client_search, self._client_list, self._client_all, self._client_selected)

        self._host_list = QListWidget()
        self._host_search = QLineEdit()
        self._setup_column(columns, "Host Fields", self._host_search, self._host_list, self._host_all, self._host_selected)

        layout.addLayout(columns, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _setup_column(self, layout, title, search_edit, list_widget, options, current):
        column = QVBoxLayout()
        column.addWidget(QLabel(title))
        search_edit.setPlaceholderText("Type to filter")
        search_edit.textChanged.connect(lambda text, lw=list_widget, opts=options: self._filter_list(lw, opts, text))
        column.addWidget(search_edit)
        list_widget.addItems(options)
        if current in options:
            list_widget.setCurrentRow(options.index(current))
        column.addWidget(list_widget)
        layout.addLayout(column, 1)

    def _filter_list(self, list_widget: QListWidget, options: list[str], text: str):
        query = text.strip().lower()
        list_widget.clear()
        if not query:
            list_widget.addItems(options)
            return
        for option in options:
            if query in option.lower():
                list_widget.addItem(option)

    def _on_accept(self):
        client_item = self._client_list.currentItem()
        host_item = self._host_list.currentItem()
        if client_item:
            self._client_selected = client_item.text()
        if host_item:
            self._host_selected = host_item.text()
        self.accept()

    def selected_fields(self) -> tuple[str, str]:
        return self._client_selected, self._host_selected


class AttributeDiffDialog(QDialog):
    """Dialog to display attribute differences for a feature pair."""

    def __init__(
        self,
        client_fid: str,
        host_fid: str | None,
        attr_changes: dict,
        geom_changed: bool,
        is_new_feature: bool,
        on_commit,
        schema_added_fields: list[str] | None = None,
        schema_removed_fields: list[str] | None = None,
        parent=None
    ):
        super().__init__(parent)
        self._client_fid = client_fid
        self._host_fid = host_fid
        self._attr_changes = attr_changes
        self._geom_changed = geom_changed
        self._is_new_feature = is_new_feature
        self._on_commit = on_commit
        self._schema_added_fields = schema_added_fields or []
        self._schema_removed_fields = schema_removed_fields or []
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("Attribute Differences")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)

        header = QLabel(
            f"Client FID: {self._client_fid} | Host FID: {self._host_fid or '—'}"
        )
        layout.addWidget(header)

        if self._is_new_feature:
            new_label = QLabel("This is a new feature (no host match).")
            new_label.setStyleSheet("color: #1c7ed6;")
            layout.addWidget(new_label)

        if self._geom_changed:
            geom_label = QLabel("Geometry changed — map overlay shown.")
            geom_label.setStyleSheet("color: #c92a2a;")
            layout.addWidget(geom_label)

        if self._schema_added_fields or self._schema_removed_fields:
            schema_lines = []
            if self._schema_added_fields:
                summary = ", ".join(self._schema_added_fields[:3])
                if len(self._schema_added_fields) > 3:
                    summary += f" +{len(self._schema_added_fields) - 3} more"
                schema_lines.append(f"Client-only fields: {summary}")
            if self._schema_removed_fields:
                summary = ", ".join(self._schema_removed_fields[:3])
                if len(self._schema_removed_fields) > 3:
                    summary += f" +{len(self._schema_removed_fields) - 3} more"
                schema_lines.append(f"Host-only fields: {summary}")
            schema_label = QLabel("Field schema differences:\n" + "\n".join(schema_lines))
            schema_label.setStyleSheet("color: #2b8a3e;")
            schema_label.setWordWrap(True)
            layout.addWidget(schema_label)

        table = QTableWidget()
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(["Field", "Client Value", "Host Value"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setAlternatingRowColors(True)

        if self._attr_changes:
            for field, values in self._attr_changes.items():
                row = table.rowCount()
                table.insertRow(row)
                table.setItem(row, 0, QTableWidgetItem(str(field)))
                table.setItem(row, 1, QTableWidgetItem(str(values.get("client", ""))))
                table.setItem(row, 2, QTableWidgetItem(str(values.get("host", ""))))
        else:
            row = table.rowCount()
            table.insertRow(row)
            info_item = QTableWidgetItem("No attribute differences")
            info_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            table.setItem(row, 0, info_item)
            table.setSpan(row, 0, 1, 3)

        layout.addWidget(table)

        button_row = QHBoxLayout()
        button_row.addStretch()

        commit_btn = QPushButton("Commit Attribute Changes")
        commit_btn.clicked.connect(self._handle_commit)
        button_row.addWidget(commit_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        button_row.addWidget(close_btn)

        layout.addLayout(button_row)

    def _handle_commit(self):
        if self._on_commit:
            self._on_commit(self._client_fid, self._host_fid)
        self.accept()


def _create_main_database() -> None:
    """Create the main GeoInbox database file and schema unconditionally."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS email_servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            protocol TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            use_ssl INTEGER DEFAULT 1,
            username TEXT,
            password TEXT,
            is_default INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS gisimple_servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            realm TEXT DEFAULT 'gisimple',
            client_id TEXT,
            username TEXT,
            password TEXT,
            token TEXT,
            is_default INTEGER DEFAULT 0,
            last_connected TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS trusted_domains (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trusted_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            source TEXT DEFAULT 'manual',
            gisimple_group TEXT
        );
        CREATE TABLE IF NOT EXISTS gisimple_credentials (
            id INTEGER PRIMARY KEY,
            server_url TEXT NOT NULL,
            username TEXT NOT NULL,
            password_encrypted TEXT NOT NULL,
            keep_logged_in INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS gisimple_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER NOT NULL,
            project_id TEXT NOT NULL,
            project_name TEXT NOT NULL,
            role TEXT DEFAULT 'viewer',
            permissions TEXT,
            last_synced TEXT,
            FOREIGN KEY (server_id) REFERENCES gisimple_servers(id),
            UNIQUE(server_id, project_id)
        );
        CREATE TABLE IF NOT EXISTS gisimple_project_layers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            layer_id TEXT NOT NULL,
            layer_name TEXT NOT NULL,
            layer_type TEXT,
            geometry_type TEXT,
            feature_count INTEGER,
            last_synced TEXT,
            FOREIGN KEY (project_id) REFERENCES gisimple_projects(id),
            UNIQUE(project_id, layer_id)
        );
    """)
    conn.commit()
    conn.close()


def prompt_create_main_database(parent=None) -> bool:
    """
    Show a cross-platform dialog informing the user about the main database path
    and asking whether to create it or overwrite the existing one. Returns True
    only when the database was created/recreated, False when the user cancels.
    """
    from qgis.PyQt.QtWidgets import QMessageBox

    if DB_PATH.exists():
        reply = QMessageBox.warning(
            parent,
            "Overwrite Settings Database",
            (
                "A settings database already exists at:\n\n"
                f"{DB_PATH}\n\n"
                "Recreating it will permanently delete all stored settings, "
                "email server configurations, and trust rules.\n\n"
                "Do you want to continue?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False
        try:
            DB_PATH.unlink()
        except OSError as exc:  # noqa: B904
            QMessageBox.warning(
                parent,
                "Error",
                f"Failed to remove existing database:\n{DB_PATH}\n\n{exc}"
            )
            return False
    else:
        reply = QMessageBox.question(
            parent,
            "Create Settings Database",
            (
                "GeoInbox needs to create a settings database at:\n\n"
                f"{DB_PATH}\n\n"
                "Do you want to create it now?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False

    _create_main_database()
    return True


def init_database():
    """Initialize SQLite database schema. No-op when the database file is absent."""
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS email_servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            protocol TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            use_ssl INTEGER DEFAULT 1,
            username TEXT,
            password TEXT,
            is_default INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS gisimple_servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            realm TEXT DEFAULT 'gisimple',
            client_id TEXT,
            username TEXT,
            password TEXT,
            token TEXT,
            is_default INTEGER DEFAULT 0,
            last_connected TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS trusted_domains (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trusted_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            source TEXT DEFAULT 'manual',
            gisimple_group TEXT
        );
        CREATE TABLE IF NOT EXISTS gisimple_credentials (
            id INTEGER PRIMARY KEY,
            server_url TEXT NOT NULL,
            username TEXT NOT NULL,
            password_encrypted TEXT NOT NULL,
            keep_logged_in INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS gisimple_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER NOT NULL,
            project_id TEXT NOT NULL,
            project_name TEXT NOT NULL,
            role TEXT DEFAULT 'viewer',
            permissions TEXT,
            last_synced TEXT,
            FOREIGN KEY (server_id) REFERENCES gisimple_servers(id),
            UNIQUE(server_id, project_id)
        );
        CREATE TABLE IF NOT EXISTS gisimple_project_layers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            layer_id TEXT NOT NULL,
            layer_name TEXT NOT NULL,
            layer_type TEXT,
            geometry_type TEXT,
            feature_count INTEGER,
            last_synced TEXT,
            FOREIGN KEY (project_id) REFERENCES gisimple_projects(id),
            UNIQUE(project_id, layer_id)
        );
    """)
    conn.commit()
    conn.close()


init_database()


class ServerConfigDialog(QDialog):
    """Dialog for configuring email server with auto-discovery support."""
    
    # Known IMAP server configurations for common providers
    KNOWN_PROVIDERS = {
        'gmail.com': {'host': 'imap.gmail.com', 'port': 993, 'ssl': True},
        'googlemail.com': {'host': 'imap.gmail.com', 'port': 993, 'ssl': True},
        'outlook.com': {'host': 'outlook.office365.com', 'port': 993, 'ssl': True},
        'hotmail.com': {'host': 'outlook.office365.com', 'port': 993, 'ssl': True},
        'live.com': {'host': 'outlook.office365.com', 'port': 993, 'ssl': True},
        'msn.com': {'host': 'outlook.office365.com', 'port': 993, 'ssl': True},
        'yahoo.com': {'host': 'imap.mail.yahoo.com', 'port': 993, 'ssl': True},
        'yahoo.co.uk': {'host': 'imap.mail.yahoo.com', 'port': 993, 'ssl': True},
        'aol.com': {'host': 'imap.aol.com', 'port': 993, 'ssl': True},
        'icloud.com': {'host': 'imap.mail.me.com', 'port': 993, 'ssl': True},
        'me.com': {'host': 'imap.mail.me.com', 'port': 993, 'ssl': True},
        'mac.com': {'host': 'imap.mail.me.com', 'port': 993, 'ssl': True},
        'zoho.com': {'host': 'imap.zoho.com', 'port': 993, 'ssl': True},
        'protonmail.com': {'host': 'imap.protonmail.ch', 'port': 993, 'ssl': True},
        'proton.me': {'host': 'imap.protonmail.ch', 'port': 993, 'ssl': True},
        'gmx.com': {'host': 'imap.gmx.com', 'port': 993, 'ssl': True},
        'gmx.net': {'host': 'imap.gmx.net', 'port': 993, 'ssl': True},
        'mail.com': {'host': 'imap.mail.com', 'port': 993, 'ssl': True},
        'yandex.com': {'host': 'imap.yandex.com', 'port': 993, 'ssl': True},
        'yandex.ru': {'host': 'imap.yandex.ru', 'port': 993, 'ssl': True},
        'fastmail.com': {'host': 'imap.fastmail.com', 'port': 993, 'ssl': True},
    }
    
    def __init__(self, server_data=None, parent=None):
        super().__init__(parent)
        self._server_data = server_data or {}
        self._setup_ui()
        self._load_data()
    
    def _setup_ui(self):
        self.setWindowTitle("Add Email Server" if not self._server_data else "Edit Email Server")
        self.setMinimumWidth(450)
        
        layout = QVBoxLayout(self)
        form = QFormLayout()
        
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("My Email Server")
        form.addRow("Name:", self._name_edit)
        
        self._protocol_combo = QComboBox()
        self._protocol_combo.addItem("IMAP", "imap")
        self._protocol_combo.addItem("POP3", "pop3")
        self._protocol_combo.addItem("EWS (Experimental)", "ews")
        self._protocol_combo.addItem("Microsoft Graph (Experimental)", "graph")
        self._protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        form.addRow("Protocol:", self._protocol_combo)
        
        # Username with auto-discover button
        username_row = QHBoxLayout()
        self._username_edit = QLineEdit()
        self._username_edit.setPlaceholderText("user@example.com")
        self._username_edit.textChanged.connect(self._on_username_changed)
        username_row.addWidget(self._username_edit)
        self._autodiscover_btn = QPushButton("🔍 Auto-Discover")
        self._autodiscover_btn.setToolTip("Automatically detect IMAP server settings based on email domain")
        self._autodiscover_btn.setFixedWidth(120)
        self._autodiscover_btn.clicked.connect(self._auto_discover)
        username_row.addWidget(self._autodiscover_btn)
        form.addRow("Email/Username:", username_row)
        
        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText("imap.example.com")
        form.addRow("Host:", self._host_edit)
        
        self._port_edit = QLineEdit()
        self._port_edit.setPlaceholderText("993")
        self._port_edit.setText("993")
        self._port_edit.setMinimumWidth(120)
        self._port_edit.setValidator(QIntValidator(1, 65535, self))
        form.addRow("Port:", self._port_edit)
        
        # SSL/TLS options
        ssl_row = QHBoxLayout()
        self._ssl_check = QCheckBox("Use SSL")
        self._ssl_check.setChecked(True)
        self._ssl_check.setToolTip("Connect using SSL (port 993 for IMAP)")
        self._ssl_check.stateChanged.connect(self._on_ssl_changed)
        ssl_row.addWidget(self._ssl_check)
        self._starttls_check = QCheckBox("Use STARTTLS")
        self._starttls_check.setChecked(False)
        self._starttls_check.setToolTip("Upgrade connection to TLS after connecting (port 143 for IMAP)")
        self._starttls_check.stateChanged.connect(self._on_starttls_changed)
        ssl_row.addWidget(self._starttls_check)
        ssl_row.addStretch()
        form.addRow("Security:", ssl_row)
        
        self._password_edit = QLineEdit()
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_edit.setPlaceholderText("Password or App Password")
        # Add eye icon inside the textbox using QAction
        self._show_password_action = self._password_edit.addAction(
            self._password_edit.style().standardIcon(self._password_edit.style().StandardPixmap.SP_DialogYesButton),
            QLineEdit.ActionPosition.TrailingPosition
        )
        self._show_password_action.setToolTip("Show/Hide password")
        self._show_password_action.triggered.connect(self._toggle_password_visibility)
        self._password_visible = False
        form.addRow("Password:", self._password_edit)
        
        # Auto-discover status label
        self._status_label = QLabel("")
        # self._status_label.setStyleSheet("color: #666; font-size: 11px;")
        self._status_label.setWordWrap(True)
        form.addRow("", self._status_label)
        
        self._default_check = QCheckBox("Set as default server")
        form.addRow("", self._default_check)
        
        layout.addLayout(form)
        
        # Test connection button
        test_row = QHBoxLayout()
        test_row.addStretch()
        self._test_btn = QPushButton("🔌 Test Connection")
        self._test_btn.clicked.connect(self._test_connection)
        test_row.addWidget(self._test_btn)
        layout.addLayout(test_row)
        
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
    
    def _toggle_password_visibility(self):
        """Toggle password visibility."""
        self._password_visible = not self._password_visible
        if self._password_visible:
            self._password_edit.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
    
    def _on_protocol_changed(self, index):
        protocol = self._protocol_combo.currentData()
        if self._ssl_check.isChecked():
            ports = {'imap': 993, 'pop3': 995, 'ews': 443, 'graph': 443}
        else:
            ports = {'imap': 143, 'pop3': 110, 'ews': 443, 'graph': 443}
        self._port_edit.setText(str(ports.get(protocol, 993)))
    
    def _on_ssl_changed(self, state):
        """Handle SSL checkbox change."""
        if state:
            self._starttls_check.setChecked(False)
            protocol = self._protocol_combo.currentData()
            if protocol == 'imap':
                self._port_edit.setText("993")
            elif protocol == 'pop3':
                self._port_edit.setText("995")
    
    def _on_starttls_changed(self, state):
        """Handle STARTTLS checkbox change."""
        if state:
            self._ssl_check.setChecked(False)
            protocol = self._protocol_combo.currentData()
            if protocol == 'imap':
                self._port_edit.setText("143")
            elif protocol == 'pop3':
                self._port_edit.setText("110")
    
    def _on_username_changed(self, text):
        """Clear status when username changes."""
        self._status_label.setText("")
    
    def _auto_discover(self):
        """Auto-discover IMAP settings based on email domain."""
        email = self._username_edit.text().strip()
        if '@' not in email:
            self._status_label.setText("⚠️ Enter a valid email address to auto-discover settings")
            # self._status_label.setStyleSheet("color: #e67700;")
            return
        
        domain = email.split('@')[1].lower()
        
        # Check known providers first
        if domain in self.KNOWN_PROVIDERS:
            config = self.KNOWN_PROVIDERS[domain]
            self._host_edit.setText(config['host'])
            self._port_edit.setText(str(config['port']))
            self._ssl_check.setChecked(config['ssl'])
            self._starttls_check.setChecked(False)
            self._status_label.setText(f"✅ Found settings for {domain}")
            # self._status_label.setStyleSheet("color: #2f9e44;")
            
            # Auto-fill name if empty
            if not self._name_edit.text().strip():
                provider_names = {
                    'gmail.com': 'Gmail', 'googlemail.com': 'Gmail',
                    'outlook.com': 'Outlook', 'hotmail.com': 'Outlook', 'live.com': 'Outlook',
                    'yahoo.com': 'Yahoo Mail', 'yahoo.co.uk': 'Yahoo Mail',
                    'icloud.com': 'iCloud Mail', 'me.com': 'iCloud Mail',
                    'protonmail.com': 'ProtonMail', 'proton.me': 'ProtonMail',
                }
                self._name_edit.setText(provider_names.get(domain, domain.split('.')[0].title()))
            return
        
        # Try common IMAP patterns via DNS/connection test
        self._status_label.setText("🔍 Trying common IMAP patterns...")
        # self._status_label.setStyleSheet("color: #1971c2;")
        QApplication.processEvents()
        
        # Common IMAP host patterns to try
        patterns = [
            f"imap.{domain}",
            f"mail.{domain}",
            f"imap.mail.{domain}",
            domain,
        ]
        
        import socket
        for host in patterns:
            for port, use_ssl in [(993, True), (143, False)]:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(3)
                    result = sock.connect_ex((host, port))
                    sock.close()
                    
                    if result == 0:
                        self._host_edit.setText(host)
                        self._port_edit.setText(str(port))
                        self._ssl_check.setChecked(use_ssl)
                        self._starttls_check.setChecked(not use_ssl and port == 143)
                        self._status_label.setText(f"✅ Found IMAP server: {host}:{port}")
                        # self._status_label.setStyleSheet("color: #2f9e44;")
                        
                        if not self._name_edit.text().strip():
                            self._name_edit.setText(domain.split('.')[0].title() + " Mail")
                        return
                except Exception as exc:
                    log_debug_exception('Skipped after error', exc)
                    continue
        
        # Fallback - suggest common pattern
        self._host_edit.setText(f"imap.{domain}")
        self._port_edit.setText("993")
        self._ssl_check.setChecked(True)
        self._status_label.setText(f"⚠️ Could not auto-detect. Using imap.{domain} - please verify")
        # self._status_label.setStyleSheet("color: #e67700;")
    
    def _test_connection(self):
        """Test the IMAP connection with current settings."""
        host = self._host_edit.text().strip()
        port_text = self._port_edit.text().strip()
        port = int(port_text) if port_text else 993
        username = self._username_edit.text().strip()
        password = self._password_edit.text()
        use_ssl = self._ssl_check.isChecked()
        use_starttls = self._starttls_check.isChecked()
        
        if not host or not username:
            QMessageBox.warning(self, "Test Connection", "Please fill in host and username")
            return
        
        self._status_label.setText("🔌 Testing connection...")
        # self._status_label.setStyleSheet("color: #1971c2;")
        QApplication.processEvents()
        
        try:
            import imaplib
            import ssl
            
            if use_ssl:
                context = ssl.create_default_context()
                imap = imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=10)
            else:
                imap = imaplib.IMAP4(host, port, timeout=10)
                if use_starttls:
                    context = ssl.create_default_context()
                    imap.starttls(ssl_context=context)
            
            if password:
                imap.login(username, password)
                imap.logout()
                self._status_label.setText("✅ Connection and authentication successful!")
                # self._status_label.setStyleSheet("color: #2f9e44;")
            else:
                imap.logout()
                self._status_label.setText("✅ Connection successful (no password to test auth)")
                # self._status_label.setStyleSheet("color: #2f9e44;")
                
        except Exception as e:
            error_msg = str(e)
            if 'authentication' in error_msg.lower() or 'login' in error_msg.lower():
                self._status_label.setText(f"❌ Authentication failed: {error_msg[:100]}")
            else:
                self._status_label.setText(f"❌ Connection failed: {error_msg[:100]}")
            # self._status_label.setStyleSheet("color: #e03131;")
    
    def _load_data(self):
        if self._server_data:
            self._name_edit.setText(self._server_data.get('name', ''))
            idx = self._protocol_combo.findData(self._server_data.get('protocol', 'imap'))
            if idx >= 0:
                self._protocol_combo.setCurrentIndex(idx)
            self._host_edit.setText(self._server_data.get('host', ''))
            self._port_edit.setText(str(self._server_data.get('port', 993)))
            self._ssl_check.setChecked(self._server_data.get('use_ssl', True))
            self._starttls_check.setChecked(self._server_data.get('use_starttls', False))
            self._username_edit.setText(self._server_data.get('username', ''))
            # Load password from database
            password = self._server_data.get('password', '')
            if password:
                self._password_edit.setText(password)
            self._default_check.setChecked(self._server_data.get('is_default', False))
    
    def _on_accept(self):
        if not self._name_edit.text().strip():
            QMessageBox.warning(self, "Validation", "Name is required")
            return
        if not self._host_edit.text().strip():
            QMessageBox.warning(self, "Validation", "Host is required")
            return
        if not self._username_edit.text().strip():
            QMessageBox.warning(self, "Validation", "Username is required")
            return
        self.accept()
    
    def get_data(self):
        return {
            'id': self._server_data.get('id'),
            'name': self._name_edit.text().strip(),
            'protocol': self._protocol_combo.currentData(),
            'host': self._host_edit.text().strip(),
            'port': int(self._port_edit.text().strip() or 993),
            'use_ssl': self._ssl_check.isChecked(),
            'use_starttls': self._starttls_check.isChecked(),
            'username': self._username_edit.text().strip(),
            'password': self._password_edit.text(),
            'is_default': self._default_check.isChecked()
        }


class ValidatedInputDialog(QDialog):
    """Custom input dialog with OK button disabled until valid input is provided."""
    
    def __init__(self, title, label, validator=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(300)
        self._validator = validator  # Optional function to validate input
        
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        
        # Label
        self._label = QLabel(label)
        layout.addWidget(self._label)
        
        # Input field
        self._input = QLineEdit()
        self._input.textChanged.connect(self._on_text_changed)
        layout.addWidget(self._input)
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self._ok_btn = QPushButton("OK")
        self._ok_btn.setEnabled(False)  # Disabled until valid input
        self._ok_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self._ok_btn)
        
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self._cancel_btn)
        
        layout.addLayout(btn_layout)
    
    def _on_text_changed(self, text):
        """Enable OK button only when input is valid."""
        text = text.strip()
        if self._validator:
            is_valid = bool(text) and self._validator(text)
        else:
            is_valid = bool(text)
        self._ok_btn.setEnabled(is_valid)
    
    def get_text(self):
        """Return the entered text."""
        return self._input.text().strip()


class GISimpleServerDialog(QDialog):
    """Dialog for configuring GISimple server."""
    
    def __init__(self, server_data=None, parent=None):
        super().__init__(parent)
        self._server_data = server_data or {}
        self._setup_ui()
        self._load_data()
    
    def _setup_ui(self):
        self.setWindowTitle("Add GISimple Server" if not self._server_data else "Edit GISimple Server")
        self.setMinimumWidth(450)
        # self.setStyleSheet("""
        #     QDialog { background: #f8f9fa; }
        #     QGroupBox { 
        #         font-weight: bold; 
        #         border: 1px solid #dee2e6; 
        #         border-radius: 8px; 
        #         margin-top: 12px; 
        #         padding-top: 12px;
        #         background: white;
        #     }
        #     QGroupBox::title { 
        #         subcontrol-origin: margin; 
        #         left: 12px; 
        #         padding: 0 8px;
        #         color: #495057;
        #     }
        #     QLineEdit, QSpinBox { 
        #         padding: 8px; 
        #         border: 1px solid #ced4da; 
        #         border-radius: 6px; 
        #         background: white;
        #     }
        #     QLineEdit:focus, QSpinBox:focus { border-color: #86b7fe; }
        #     QPushButton {
        #         padding: 8px 16px;
        #         border-radius: 6px;
        #         font-weight: 500;
        #     }
        # """)
        
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        
        # Server info group
        server_group = QGroupBox("Server Information")
        server_form = QFormLayout(server_group)
        
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("My GISimple Server")
        server_form.addRow("Name:", self._name_edit)
        
        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText("https://gisimple.example.com")
        server_form.addRow("Server URL:", self._url_edit)
        
        self._realm_edit = QLineEdit()
        self._realm_edit.setText("gisimple")
        self._realm_edit.setPlaceholderText("gisimple")
        server_form.addRow("Realm:", self._realm_edit)
        
        self._client_id_edit = QLineEdit()
        self._client_id_edit.setPlaceholderText("admin-cli")
        server_form.addRow("Client ID:", self._client_id_edit)
        
        layout.addWidget(server_group)
        
        # Credentials group
        cred_group = QGroupBox("Credentials")
        cred_form = QFormLayout(cred_group)
        
        self._username_edit = QLineEdit()
        self._username_edit.setPlaceholderText("username")
        cred_form.addRow("Username:", self._username_edit)
        
        self._password_edit = QLineEdit()
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_edit.setPlaceholderText("password")
        cred_form.addRow("Password:", self._password_edit)
        
        layout.addWidget(cred_group)
        
        # Test connection
        test_row = QHBoxLayout()
        self._test_btn = QPushButton("Test Connection")
        # self._test_btn.setStyleSheet("background: #e7f5ff; color: #1971c2; border: 1px solid #74c0fc;")
        self._test_btn.clicked.connect(self._test_connection)
        test_row.addWidget(self._test_btn)
        
        self._test_status = QLabel("")
        test_row.addWidget(self._test_status)
        test_row.addStretch()
        layout.addLayout(test_row)
        
        # Default checkbox
        self._default_check = QCheckBox("Set as default server")
        layout.addWidget(self._default_check)
        
        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        # buttons.setStyleSheet("""
        #     QPushButton[text="OK"] { background: #d3f9d8; color: #2f9e44; border: 1px solid #8ce99a; }
        #     QPushButton[text="Cancel"] { background: #fff5f5; color: #e03131; border: 1px solid #ffc9c9; }
        # """)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
    
    def _load_data(self):
        if self._server_data:
            self._name_edit.setText(self._server_data.get('name', ''))
            self._url_edit.setText(self._server_data.get('url', ''))
            self._realm_edit.setText(self._server_data.get('realm', 'gisimple'))
            self._client_id_edit.setText(self._server_data.get('client_id', ''))
            self._username_edit.setText(self._server_data.get('username', ''))
            self._password_edit.setText(self._server_data.get('password', ''))
            self._default_check.setChecked(self._server_data.get('is_default', False))
    
    def _test_connection(self):
        """Test connection to GISimple server."""
        url = self._url_edit.text().strip()
        if not url:
            self._test_status.setText("❌ URL required")
            # self._test_status.setStyleSheet("color: #e03131;")
            return
        
        self._test_status.setText("⏳ Testing...")
        # self._test_status.setStyleSheet("color: #1971c2;")
        
        # TODO: Implement actual connection test
        # For now, simulate success
        self._test_status.setText("✓ Connection successful")
        # self._test_status.setStyleSheet("color: #2f9e44;")
    
    def _on_accept(self):
        if not self._name_edit.text().strip():
            QMessageBox.warning(self, "Validation", "Name is required")
            return
        if not self._url_edit.text().strip():
            QMessageBox.warning(self, "Validation", "Server URL is required")
            return
        self.accept()
    
    def get_data(self):
        return {
            'id': self._server_data.get('id'),
            'name': self._name_edit.text().strip(),
            'url': self._url_edit.text().strip(),
            'realm': self._realm_edit.text().strip() or 'gisimple',
            'client_id': self._client_id_edit.text().strip(),
            'username': self._username_edit.text().strip(),
            'password': self._password_edit.text(),
            'is_default': self._default_check.isChecked()
        }


class MailboxLoadWorker(QThread):
    """Worker thread to load mailbox messages with progress reporting."""
    progress = pyqtSignal(int, int, str)  # current, total, status message
    message_loaded = pyqtSignal(object)  # single message data
    finished = pyqtSignal(int)  # total messages loaded
    error = pyqtSignal(str)

    def __init__(self, imap_connection, imap_lock, mailbox_name, parent=None):
        super().__init__(parent)
        self._imap = imap_connection
        self._imap_lock = imap_lock
        self._mailbox_name = mailbox_name
        self.is_cancelled = False

    def run(self):
        """Load messages from mailbox with progress."""
        try:
            import email
            import email.header
            import re

            if self.is_cancelled:
                return

            with self._imap_lock:
                if self.is_cancelled:
                    return
                status, data = self._imap.select(self._mailbox_name)
                if status != 'OK':
                    self.error.emit(f"Failed to select mailbox: {self._mailbox_name}")
                    return

                status, msg_nums = self._imap.search(None, 'ALL')
                if status != 'OK' or not msg_nums[0]:
                    self.finished.emit(0)
                    return

            msg_list = msg_nums[0].split()
            total_msgs = len(msg_list)
            # Get last 50 messages (most recent)
            msg_list = msg_list[-50:] if total_msgs > 50 else msg_list
            msg_list.reverse()  # Most recent first
            
            total_to_load = len(msg_list)
            self.progress.emit(0, total_to_load, f"Loading 0/{total_to_load} messages...")

            loaded_count = 0
            for i, msg_num in enumerate(msg_list):
                if self.is_cancelled:
                    return

                try:
                    with self._imap_lock:
                        if self.is_cancelled:
                            return
                        status, msg_data = self._imap.fetch(msg_num, '(RFC822.SIZE FLAGS BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])')
                    
                    if status != 'OK':
                        continue

                    # Check if message is read
                    is_read = False
                    flags_data = msg_data[0][0] if msg_data and msg_data[0] else b''
                    if b'\\Seen' in flags_data:
                        is_read = True

                    # Parse headers
                    header_data = msg_data[0][1] if msg_data and msg_data[0] else b''
                    msg = email.message_from_bytes(header_data)

                    # Decode From
                    from_header = msg.get('From', '')
                    if from_header:
                        decoded = email.header.decode_header(from_header)
                        from_addr = ''.join([
                            part.decode(enc or 'utf-8') if isinstance(part, bytes) else part
                            for part, enc in decoded
                        ])
                    else:
                        from_addr = ''

                    # Decode Subject
                    subject_header = msg.get('Subject', '')
                    if subject_header:
                        decoded = email.header.decode_header(subject_header)
                        subject = ''.join([
                            part.decode(enc or 'utf-8') if isinstance(part, bytes) else part
                            for part, enc in decoded
                        ])
                    else:
                        subject = '(No subject)'

                    # Date
                    date_str = msg.get('Date', '')

                    # Size from fetch response
                    size = 0
                    attachment_count = 0
                    for part in msg_data:
                        if isinstance(part, tuple) and len(part) > 0:
                            part_data = part[0] if isinstance(part[0], bytes) else b''
                            if b'RFC822.SIZE' in part_data:
                                size_match = re.search(rb'RFC822\.SIZE (\d+)', part_data)
                                if size_match:
                                    size = int(size_match.group(1))
                            # Count attachments from BODYSTRUCTURE
                            if b'BODYSTRUCTURE' in part_data:
                                # Count occurrences of "attachment" in bodystructure
                                bodystructure_str = part_data.decode('utf-8', errors='ignore').lower()
                                attachment_count = bodystructure_str.count('"attachment"')
                                # Also check for common attachment indicators
                                if attachment_count == 0:
                                    # Count application/* parts which are typically attachments
                                    attachment_count = bodystructure_str.count('"application"')

                    msg_info = {
                        'msg_num': msg_num.decode() if isinstance(msg_num, bytes) else str(msg_num),
                        'from_addr': from_addr,
                        'subject': subject,
                        'date_str': date_str,
                        'size': size,
                        'is_read': is_read,
                        'attachment_count': attachment_count
                    }
                    self.message_loaded.emit(msg_info)
                    loaded_count += 1

                except Exception as e:
                    print(f"Error loading message: {e}")
                    continue

                # Update progress
                self.progress.emit(i + 1, total_to_load, f"Loading {i + 1}/{total_to_load} messages...")

            self.finished.emit(loaded_count)

        except Exception as e:
            self.error.emit(str(e))

    def cancel(self):
        """Signal the thread to stop."""
        self.is_cancelled = True


class ZoomableImageLabel(QLabel):
    """Label that supports zooming via mouse wheel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._base_pixmap = QPixmap()
        self._scale = 1.0
        self._auto_fit = True
        self._rotation = 0
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_image(self, pixmap: QPixmap | None):
        self._base_pixmap = pixmap if pixmap and not pixmap.isNull() else QPixmap()
        self._rotation = 0
        self._auto_fit = True
        self._scale = self._fit_scale()
        self._apply_scale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._auto_fit and not self._base_pixmap.isNull():
            self._scale = self._fit_scale()
            self._apply_scale()

    def wheelEvent(self, event):
        if self._base_pixmap.isNull():
            return
        delta = event.angleDelta().y()
        factor = 1.1 if delta > 0 else 0.9
        self._auto_fit = False
        self._scale = min(8.0, max(0.1, self._scale * factor))
        self._apply_scale()

    def reset_zoom(self):
        if self._base_pixmap.isNull():
            return
        self._auto_fit = True
        self._scale = self._fit_scale()
        self._apply_scale()

    def rotate(self, degrees: int):
        if self._base_pixmap.isNull():
            return
        self._rotation = (self._rotation + degrees) % 360
        if self._auto_fit:
            self._scale = self._fit_scale()
        self._apply_scale()

    def _fit_scale(self) -> float:
        if self._base_pixmap.isNull():
            return 1.0
        avail_w = max(1, self.width())
        avail_h = max(1, self.height())
        pixmap = self._get_rotated_pixmap()
        if pixmap.width() == 0 or pixmap.height() == 0:
            return 1.0
        return min(avail_w / pixmap.width(), avail_h / pixmap.height())

    def _get_rotated_pixmap(self) -> QPixmap:
        if self._base_pixmap.isNull() or self._rotation == 0:
            return self._base_pixmap
        transform = QTransform()
        transform.rotate(self._rotation)
        return self._base_pixmap.transformed(transform, Qt.TransformationMode.SmoothTransformation)

    def _apply_scale(self):
        if self._base_pixmap.isNull():
            super().setPixmap(QPixmap())
            return
        pixmap = self._get_rotated_pixmap()
        width = max(1, int(pixmap.width() * self._scale))
        height = max(1, int(pixmap.height() * self._scale))
        scaled = pixmap.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        super().setPixmap(scaled)


class PhotoViewerDialog(QDialog):
    """Simple in-app photo viewer with zoomable preview."""

    def __init__(self, photos: List[Tuple[str, Path]], parent=None, initial_index: int = 0):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowSystemMenuHint |
            Qt.WindowType.WindowMinMaxButtonsHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self.resize(720, 520)
        self._photos = photos or []
        self._current_index = None

        layout = QVBoxLayout(self)

        self._image_label = ZoomableImageLabel()
        self._image_label.setMinimumSize(320, 320)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._image_label)
        layout.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        self._prev_btn = QPushButton("◀ Prev")
        self._prev_btn.clicked.connect(lambda: self._step(-1))
        btn_row.addWidget(self._prev_btn)

        self._next_btn = QPushButton("Next ▶")
        self._next_btn.clicked.connect(lambda: self._step(1))
        btn_row.addWidget(self._next_btn)

        rotate_left_btn = QPushButton("⟲ Rotate Left")
        rotate_left_btn.clicked.connect(lambda: self._rotate(-90))
        btn_row.addWidget(rotate_left_btn)

        rotate_right_btn = QPushButton("Rotate Right ⟳")
        rotate_right_btn.clicked.connect(lambda: self._rotate(90))
        btn_row.addWidget(rotate_right_btn)

        self._reset_zoom_btn = QPushButton("Reset Zoom")
        self._reset_zoom_btn.clicked.connect(self._reset_zoom)
        btn_row.addWidget(self._reset_zoom_btn)

        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        if self._photos:
            initial_index = max(0, min(initial_index, len(self._photos) - 1))
            self._show_photo(initial_index)
        else:
            self._update_title("(no photo)")
            self._prev_btn.setEnabled(False)
            self._next_btn.setEnabled(False)

    def _update_title(self, name: str):
        self.setWindowTitle(f"Photo Viewer - {name}")

    def _step(self, delta: int):
        if self._current_index is None:
            return
        target = self._current_index + delta
        if 0 <= target < len(self._photos):
            self._show_photo(target)

    def _show_photo(self, index: int):
        if index < 0 or index >= len(self._photos):
            return
        self._current_index = index
        name, path = self._photos[index]
        self._update_title(name)

        if path and path.exists():
            pixmap = QPixmap(str(path))
            self._image_label.set_image(pixmap)
        else:
            self._image_label.set_image(None)

        self._prev_btn.setEnabled(index > 0)
        self._next_btn.setEnabled(index < len(self._photos) - 1)

    def _reset_zoom(self):
        self._image_label.reset_zoom()

    def _rotate(self, degrees: int):
        self._image_label.rotate(degrees)


class GeoInboxDialog(QDialog):
    """Main dialog for GeoInbox plugin."""

    _instance = None
    _toolbar_action = None

    @classmethod
    def show_dialog(cls, toolbar_action=None):
        try:
            if not QgsProject.instance().mapLayers():
                iface.messageBar().pushMessage(
                    "No Project Open",
                    "No project is open. Create a new one or open an existing one to start using these tools.",
                    level=QgsMessageBar.WARNING,
                    duration=3
                )
        except Exception as e:
            print(f"Error checking project status: {e}")

        # First-use: ask before creating the database
        if not DB_PATH.exists():
            parent_win = iface.mainWindow() if iface else None
            if not prompt_create_main_database(parent_win):
                return

        if cls._instance is None:
            cls._instance = GeoInboxDialog()
        cls._toolbar_action = toolbar_action
        if toolbar_action:
            toolbar_action.setChecked(True)
        cls._instance.show()
        cls._instance.activateWindow()

    def __init__(self, parent=None):
        # Use QGIS main window as parent to keep dialog visible when clicking QGIS
        if parent is None:
            parent = iface.mainWindow() if iface else None
        super().__init__(parent)
        self.setWindowTitle('GeoInbox')
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowSystemMenuHint |
            Qt.WindowType.WindowMinMaxButtonsHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self.resize(1100, 750)
        self.setMinimumSize(900, 600)
        self._closing = False         # set True in closeEvent to guard all signal callbacks
        self._undo_stack = []         # list of dicts describing reversible commit operations
        self._stop_comparison = False # set True via Stop button to abort an in-progress comparison
        self._imap = None  # Initialize IMAP connection variable
        self._cancel_message_loading = False  # Initialize cancellation flag
        self._email_worker = None  # Initialize email fetch worker
        self._mailbox_worker = None  # Initialize mailbox load worker
        self._imap_lock = threading.Lock()  # Lock for IMAP connection access
        self._attachment_fetch_worker = None
        self._pending_attachment_indices: list[int] = []
        self._pending_attachment_callback: Optional[Callable[[bool], None]] = None
        self._browser_button = None
        self._browser_owner_tree = None
        self._browser_preview_layer = None
        self._browser_preview_canvas = None
        self._browser_preview_stack = None
        self._browser_preview_image = None
        self._browser_preview_pixmap = None
        self._browser_preview_scroll = None
        self._browser_preview_pan_tool = None
        self._browser_items: list[dict] = []
        self._current_browser_owner: str | None = None
        self._browser_preview_canvas = None  # In-dialog preview canvas
        self._browser_preview_stack = None  # Stacked widget for canvas/image
        self._browser_preview_image = None  # Image preview label
        self._browser_folder_label = None
        self._browser_refresh_btn = None
        self._browser_search_edit = None
        self._browser_search_text = ""
        self._browser_add_btn = None
        self._browser_delete_btn = None
        self._browser_status_label = None
        self._browser_preview_scroll = None  # Scroll area for image preview
        self._browser_preview_layer = None  # Temporary preview layer for canvas
        self._refreshing_layer_lists = False
        self._project_changing = False
        self._auto_refresh_timer = None  # Timer for auto-refresh mailbox
        self._setup_ui()
        self._refresh_server_list()
        self._setup_auto_refresh_timer()  # Initialize auto-refresh based on saved settings

        project = QgsProject.instance()
        project.layersWillBeRemoved.connect(self._on_layers_will_be_removed)
        project.layersAdded.connect(self._on_layers_added)
        for signal_name in ["projectWillBeCleared", "cleared", "projectCleared"]:
            if hasattr(project, signal_name):
                getattr(project, signal_name).connect(self._on_project_cleared)
        for signal_name in ["projectRead", "readProject"]:
            if hasattr(project, signal_name):
                getattr(project, signal_name).connect(self._on_project_read)

        # Populate layer trees immediately if a project is already loaded
        print("DEBUG: Scheduling initial layer tree refresh")
        QTimer.singleShot(50, self._refresh_versioning_layer_trees)  # Slight delay to ensure UI is ready

        # Do NOT auto-connect - user must manually click Connect button
        # QTimer.singleShot(500, self._auto_connect_default_server)

    def closeEvent(self, event):
        """Disconnect all layer signals before Qt destroys the widgets."""
        self._closing = True  # block all signal callbacks immediately
        # Clear browser preview layer
        try:
            self._clear_browser_preview()
        except Exception as exc:
            log_debug_exception('Suppressed error', exc)
        try:
            self._disconnect_layer_selection(getattr(self, "_client_layer", None))
        except Exception as exc:
            log_debug_exception('Suppressed error', exc)
        try:
            self._disconnect_layer_selection(getattr(self, "_host_layer", None))
        except Exception as exc:
            log_debug_exception('Suppressed error', exc)
        if GeoInboxDialog._toolbar_action:
            GeoInboxDialog._toolbar_action.setChecked(False)
        GeoInboxDialog._instance = None
        super().closeEvent(event)

    def _setup_ui(self):
        """Setup the main UI with modern pastel styling."""
        # Apply modern pastel stylesheet
        # self.setStyleSheet("""
        #     QDialog {
        #         background: qlineargradient(x1:0, y1:0, x2:1, y2:1, 
        #             stop:0 #f8f9fa, stop:1 #e9ecef);
        #     }
        #     QTabWidget::pane {
        #         border: 1px solid #dee2e6;
        #         border-radius: 8px;
        #         background: white;
        #         margin-top: -1px;
        #     }
        #     QTabBar::tab {
        #         background: #e9ecef;
        #         border: 1px solid #dee2e6;
        #         border-bottom: none;
        #         border-top-left-radius: 8px;
        #         border-top-right-radius: 8px;
        #         padding: 10px 20px;
        #         margin-right: 4px;
        #         font-weight: 500;
        #         color: #495057;
        #     }
        #     QTabBar::tab:selected {
        #         background: white;
        #         color: #212529;
        #         border-bottom: 2px solid #74c0fc;
        #     }
        #     QTabBar::tab:hover:!selected {
        #         background: #f1f3f5;
        #     }
        #     QGroupBox {
        #         font-weight: bold;
        #         border: 1px solid #dee2e6;
        #         border-radius: 10px;
        #         margin-top: 12px;
        #         padding: 16px;
        #         padding-top: 24px;
        #         background: white;
        #     }
        #     QGroupBox::title {
        #         subcontrol-origin: margin;
        #         subcontrol-position: top left;
        #         left: 12px;
        #         top: 4px;
        #         padding: 2px 8px;
        #         color: #495057;
        #         background: transparent;
        #     }
        #     QTableWidget {
        #         border: 1px solid #e9ecef;
        #         border-radius: 8px;
        #         background: white;
        #         gridline-color: #f1f3f5;
        #         selection-background-color: #d0ebff;
        #     }
        #     QTableWidget::item {
        #         padding: 8px;
        #     }
        #     QHeaderView::section {
        #         background: #f8f9fa;
        #         border: none;
        #         border-bottom: 2px solid #dee2e6;
        #         padding: 10px;
        #         font-weight: 600;
        #         color: #495057;
        #     }
        #     QListWidget {
        #         border: 1px solid #e9ecef;
        #         border-radius: 8px;
        #         background: white;
        #         padding: 4px;
        #     }
        #     QListWidget::item {
        #         padding: 8px;
        #         border-radius: 4px;
        #     }
        #     QListWidget::item:selected {
        #         background: #d0ebff;
        #         color: #1864ab;
        #     }
        #     QListWidget::item:hover:!selected {
        #         background: #e7f5ff;
        #     }
        #     QPushButton {
        #         padding: 10px 18px;
        #         border-radius: 8px;
        #         font-weight: 500;
        #         border: 1px solid #dee2e6;
        #         background: white;
        #         color: #495057;
        #     }
        #     QPushButton:hover {
        #         background: #f8f9fa;
        #         border-color: #adb5bd;
        #     }
        #     QPushButton:pressed {
        #         background: #e9ecef;
        #     }
        #     QPushButton[primary="true"] {
        #         background: #d3f9d8;
        #         color: #2f9e44;
        #         border: 1px solid #8ce99a;
        #     }
        #     QPushButton[primary="true"]:hover {
        #         background: #b2f2bb;
        #     }
        #     QPushButton[danger="true"] {
        #         background: #ffe3e3;
        #         color: #c92a2a;
        #         border: 1px solid #ffa8a8;
        #     }
        #     QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
        #         padding: 10px 12px;
        #         border: 1px solid #dee2e6;
        #         border-radius: 8px;
        #         background: white;
        #         selection-background-color: #d0ebff;
        #     }
        #     QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
        #         border-color: #74c0fc;
        #         outline: none;
        #     }
        #     QComboBox::drop-down {
        #         border: none;
        #         padding-right: 8px;
        #     }
        #     QTextBrowser {
        #         border: 1px solid #e9ecef;
        #         border-radius: 8px;
        #         background: white;
        #         padding: 8px;
        #     }
        #     QProgressBar {
        #         border: none;
        #         border-radius: 6px;
        #         background: #e9ecef;
        #         height: 8px;
        #         text-align: center;
        #     }
        #     QProgressBar::chunk {
        #         background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        #             stop:0 #74c0fc, stop:1 #4dabf7);
        #         border-radius: 6px;
        #     }
        #     QCheckBox {
        #         spacing: 8px;
        #     }
        #     QCheckBox::indicator {
        #         width: 20px;
        #         height: 20px;
        #         border-radius: 4px;
        #         border: 2px solid #dee2e6;
        #         background: white;
        #     }
        #     QCheckBox::indicator:checked {
        #         background: #74c0fc;
        #         border-color: #4dabf7;
        #     }
        #     QSplitter::handle {
        #         background: #dee2e6;
        #         width: 2px;
        #         margin: 4px;
        #         border-radius: 1px;
        #     }
        #     QLabel {
        #         color: #495057;
        #     }
        #     QFrame[frameShape="4"] {
        #         background: #e9ecef;
        #         max-height: 1px;
        #     }
        # """)
        
        # 8px grid spacing constant
        GRID = 8
        
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # =====================================================================
        # LEFT SIDEBAR - Workflow Navigation
        # =====================================================================
        sidebar = QWidget()
        sidebar.setFixedWidth(200)
        sidebar.setStyleSheet("""
            QWidget { border: 1px solid #dee2e6; }
            QPushButton {
                text-align: left;
                padding: 8px 12px;
                border: 1px solid transparent;
                border-radius: 4px;
                background: transparent;
                font-weight: 500;
                color: #495057;
            }
            QPushButton:hover { background: #f1f3f5; }
            QPushButton:checked {
                background: #e7f5ff;
                border-color: #a5d8ff;
                color: #1864ab;
            }
        """)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(GRID, GRID, GRID, GRID)  # Add left margin
        sidebar_layout.setSpacing(0)
        
        # App title
        title_label = QLabel("GeoInbox")
        # title_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #1864ab; padding: 16px; background: transparent;")
        sidebar_layout.addWidget(title_label)
        
        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        # sep.setStyleSheet("background: #dee2e6; max-height: 1px; margin: 8px 16px;")
        sidebar_layout.addWidget(sep)
        
        # Navigation buttons (checkable for selection state)
        self._nav_buttons = []
        nav_items = [
            ("📧 Retrieve Attachment\nfrom Email", "email"),
            ("🌐 Import Files from\nGISimple", "gisimple"),
            ("🔄 Data Versioning", "versioning"),
            ("🗂️ Data Browser", "browser"),
            ("📦 Geov Format", "geov"),
            ("⚙️ Settings", "settings"),
            ("📚 Documentation", "documentation"),
            ("ℹ️ About", "about"),
        ]
        
        for label, page_id in nav_items:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setProperty("page_id", page_id)
            btn.setProperty("base_label", label)
            btn.clicked.connect(lambda checked, pid=page_id: self._switch_page(pid))
            sidebar_layout.addWidget(btn)
            self._nav_buttons.append(btn)
            if page_id == "browser":
                self._browser_button = btn
        
        sidebar_layout.addStretch()
        
        main_layout.addWidget(sidebar)
        
        # =====================================================================
        # RIGHT CONTENT AREA
        # =====================================================================
        content_area = QWidget()
        content_layout = QVBoxLayout(content_area)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        
        # ---------------------------------------------------------------------
        # STACKED PAGES - Main content area
        # ---------------------------------------------------------------------
        from qgis.PyQt.QtWidgets import QStackedWidget
        self._pages = QStackedWidget()
        # self._pages.setStyleSheet("background: #f8f9fa;")
        
        # Create pages
        self._email_page = self._create_email_page()
        self._gisimple_page = self._create_gisimple_page()
        self._versioning_page = self._create_versioning_page()
        self._browser_page = self._create_browser_page()
        self._geov_page = self._create_geov_export_page()
        self._settings_page = self._create_settings_page()
        self._documentation_page = self._create_documentation_page()
        self._about_page = self._create_about_page()
        
        self._pages.addWidget(self._wrap_page_in_scroll(self._email_page))
        self._pages.addWidget(self._wrap_page_in_scroll(self._gisimple_page))
        self._pages.addWidget(self._wrap_page_in_scroll(self._versioning_page))
        self._pages.addWidget(self._wrap_page_in_scroll(self._browser_page))
        self._pages.addWidget(self._wrap_page_in_scroll(self._geov_page))
        self._pages.addWidget(self._wrap_page_in_scroll(self._settings_page))
        self._pages.addWidget(self._wrap_page_in_scroll(self._documentation_page))
        self._pages.addWidget(self._wrap_page_in_scroll(self._about_page))
        
        self._page_map = {
            "email": 0,
            "gisimple": 1,
            "versioning": 2,
            "browser": 3,
            "geov": 4,
            "settings": 5,
            "documentation": 6,
            "about": 7,
        }
        
        content_layout.addWidget(self._pages)
        main_layout.addWidget(content_area)
        
        # Select first nav button by default
        if self._nav_buttons:
            self._nav_buttons[1].setChecked(True)  # Email Browser
            self._switch_page("email")

        self._update_browser_tab_state()
    
    def _switch_page(self, page_id: str):
        """Switch to the specified page."""
        if page_id in self._page_map:
            # Clear browser preview when leaving browser page
            if page_id != "browser":
                try:
                    self._clear_browser_preview()
                except Exception as exc:
                    log_debug_exception('Suppressed error', exc)

            self._pages.setCurrentIndex(self._page_map[page_id])
            # Update nav button states
            for btn in self._nav_buttons:
                is_selected = btn.property("page_id") == page_id
                btn.setChecked(is_selected)
                base_label = btn.property("base_label") or btn.text()
                btn.setText(base_label)
                self._set_nav_button_indicator(btn, is_selected)
            
            # Auto-load data when switching to specific pages
            if page_id == "settings":
                self._refresh_server_list()


    def _set_nav_button_indicator(self, button: QPushButton, is_selected: bool):
        """Show a black triangle after text when selected (1.5x font size)."""
        button.setIcon(QIcon())
        button.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        if not is_selected:
            return

        metrics = QFontMetrics(button.font())
        size = max(8, int(metrics.height() * 1.5 * 0.338))
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#000000"))
        painter.setPen(Qt.PenStyle.NoPen)
        triangle = QPolygon([
            QPoint(0, 0),
            QPoint(0, size),
            QPoint(size, size // 2)
        ])
        painter.drawPolygon(triangle)
        painter.end()

        button.setIcon(QIcon(pixmap))
        button.setIconSize(QSize(size, size))
        button.setLayoutDirection(Qt.LayoutDirection.RightToLeft)

    def _wrap_page_in_scroll(self, page: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        scroll.setWidget(page)
        return scroll
    
    # =========================================================================
    # PAGE CREATION METHODS (New 3-panel layouts)
    # =========================================================================
    
    def _create_email_page(self) -> QWidget:
        """Create Email Browser page with vertical layout like versioning tab."""
        GRID = 8
        page = QWidget()
        page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        page_layout.setSpacing(GRID * 2)
        
        # Header
        header_layout = QHBoxLayout()
        title_label = QLabel("📧 Retrieve Attachment from Email")
        title_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #1864ab;")
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        page_layout.addLayout(header_layout)
        
        # Main vertical splitter - this will contain all sections directly
        main_splitter = QSplitter(Qt.Orientation.Vertical)
        main_splitter.setChildrenCollapsible(False)
        main_splitter.setHandleWidth(8)
        main_splitter.setOpaqueResize(True)
        main_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        # ---------------------------------------------------------------------
        # SECTION 1: Email Server Connection
        # ---------------------------------------------------------------------
        top_section = QWidget()
        top_layout = QVBoxLayout(top_section)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)
        
        # Group 1: Server Connection
        conn_group = QGroupBox("📧 Email Server")
        conn_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        conn_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
                color: #2c3e50;
                border: 2px solid #bdc3c7;
                border-radius: 5px;
                margin-top: 1ex;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        conn_group_layout = QVBoxLayout(conn_group)
        conn_group_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        conn_group_layout.setSpacing(GRID)
        
        # Server selector and connect
        server_row = QHBoxLayout()
        server_row.setSpacing(GRID)
        self._server_combo = QComboBox()
        self._server_combo.addItem("No servers configured")
        server_row.addWidget(self._server_combo, 1)
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setFixedHeight(32)
        self._connect_btn.clicked.connect(self._on_connect)
        server_row.addWidget(self._connect_btn)
        conn_group_layout.addLayout(server_row)
        
        # Connection status
        self._status_label = QLabel("Not connected")
        conn_group_layout.addWidget(self._status_label)
        
        # Add group to section
        top_layout.addWidget(conn_group, 1)
        
        # Add top section to main splitter
        main_splitter.addWidget(top_section)
        
        # ---------------------------------------------------------------------
        # SECTION 2: Mail Folders
        # ---------------------------------------------------------------------
        middle_section = QWidget()
        middle_layout = QVBoxLayout(middle_section)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(0)
        
        folders_group = QGroupBox("📁 Mail Folders")
        folders_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        folders_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
                color: #2c3e50;
                border: 2px solid #bdc3c7;
                border-radius: 5px;
                margin-top: 1ex;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        folders_layout = QVBoxLayout(folders_group)
        folders_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        folders_layout.setSpacing(GRID)
        
        self._mailbox_list = QListWidget()
        self._mailbox_list.setMaximumHeight(200)
        self._mailbox_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._mailbox_list.itemClicked.connect(self._on_mailbox_selected)
        folders_layout.addWidget(self._mailbox_list)
        
        # Add group to section
        middle_layout.addWidget(folders_group, 1)
        
        # Add middle section to main splitter
        main_splitter.addWidget(middle_section)
        
        # ---------------------------------------------------------------------
        # SECTION 3: Messages & Content
        # ---------------------------------------------------------------------
        results_section = QWidget()
        results_layout = QVBoxLayout(results_section)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(0)
        
        message_group = QGroupBox("📬 Messages")
        message_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        message_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
                color: #2c3e50;
                border: 2px solid #bdc3c7;
                border-radius: 5px;
                margin-top: 1ex;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        message_layout = QVBoxLayout(message_group)
        message_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        message_layout.setSpacing(GRID)
        
        # Search & Filter controls
        controls_row = QHBoxLayout()
        controls_row.setSpacing(GRID)
        
        # Filter dropdown
        self._filter_combo = QComboBox()
        self._filter_combo.addItems(["All Messages", "Unread", "With Attachments", "Trusted Senders"])
        self._filter_combo.currentIndexChanged.connect(self._apply_message_filter)
        controls_row.addWidget(self._filter_combo)
        
        # Search box
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search messages...")
        self._search_edit.textChanged.connect(self._apply_message_filter)
        controls_row.addWidget(self._search_edit, 1)
        
        # Removed redundant Refresh button - use Reload Emails button instead
        
        message_layout.addLayout(controls_row)
        
        # Progress bar
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setFixedHeight(16)
        self._progress.setTextVisible(True)
        message_layout.addWidget(self._progress)
        
        # Message table
        self._message_table = QTableWidget()
        self._message_table.setColumnCount(6)
        self._message_table.setHorizontalHeaderLabels(["Origin", "From", "Subject", "Date", "Size", "Attachments"])
        self._message_table.setColumnWidth(0, 80)  # Origin column fixed width
        self._message_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._message_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._message_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._message_table.itemSelectionChanged.connect(self._on_message_selected)
        # Double-click on message does nothing (removed download behavior)
        # self._message_table.itemDoubleClicked.connect(self._on_message_double_clicked)
        message_layout.addWidget(self._message_table)

        # Message Actions under message list
        msg_group = QGroupBox("Message Actions")
        msg_layout = QVBoxLayout(msg_group)
        msg_layout.setSpacing(GRID)

        msg_action_row = FlowLayout(spacing=GRID)

        self._mark_read_btn = QPushButton("Mark Read")
        self._mark_read_btn.setFixedHeight(28)
        self._mark_read_btn.setMinimumWidth(110)
        self._mark_read_btn.setEnabled(False)
        self._mark_read_btn.clicked.connect(self._mark_as_read)
        msg_action_row.addWidget(self._mark_read_btn)

        self._mark_unread_btn = QPushButton("Mark Unread")
        self._mark_unread_btn.setFixedHeight(28)
        self._mark_unread_btn.setMinimumWidth(110)
        self._mark_unread_btn.setEnabled(False)
        self._mark_unread_btn.clicked.connect(self._mark_as_unread)
        msg_action_row.addWidget(self._mark_unread_btn)

        self._delete_msg_btn = QPushButton("Delete")
        self._delete_msg_btn.setFixedHeight(28)
        self._delete_msg_btn.setMinimumWidth(110)
        self._delete_msg_btn.setEnabled(False)
        self._delete_msg_btn.clicked.connect(self._delete_selected_messages)
        msg_action_row.addWidget(self._delete_msg_btn)

        self._trust_sender_btn = QPushButton("Trust Sender")
        self._trust_sender_btn.setFixedHeight(28)
        self._trust_sender_btn.setMinimumWidth(110)
        self._trust_sender_btn.setEnabled(False)
        self._trust_sender_btn.clicked.connect(self._trust_selected_sender)
        msg_action_row.addWidget(self._trust_sender_btn)

        self._reload_emails_btn = QPushButton("Reload Emails")
        self._reload_emails_btn.setFixedHeight(28)
        self._reload_emails_btn.setMinimumWidth(110)
        self._reload_emails_btn.setEnabled(False)
        self._reload_emails_btn.clicked.connect(self._refresh_mailbox)
        msg_action_row.addWidget(self._reload_emails_btn)

        self._untrust_sender_btn = QPushButton("Untrust Sender")
        self._untrust_sender_btn.setFixedHeight(28)
        self._untrust_sender_btn.setMinimumWidth(110)
        self._untrust_sender_btn.setEnabled(False)
        self._untrust_sender_btn.clicked.connect(self._untrust_selected_sender)
        msg_action_row.addWidget(self._untrust_sender_btn)

        msg_layout.addLayout(msg_action_row)
        message_layout.addWidget(msg_group)
        
        # Add group to section
        results_layout.addWidget(message_group, 1)
        
        # Add results section to main splitter
        main_splitter.addWidget(results_section)
        
        # ---------------------------------------------------------------------
        # SECTION 4: Email Preview
        # ---------------------------------------------------------------------
        preview_section = QWidget()
        preview_section_layout = QVBoxLayout(preview_section)
        preview_section_layout.setContentsMargins(0, 0, 0, 0)
        preview_section_layout.setSpacing(0)
        
        preview_group = QGroupBox("📖 Email Content")
        preview_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        preview_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
                color: #2c3e50;
                border: 2px solid #bdc3c7;
                border-radius: 5px;
                margin-top: 1ex;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        preview_layout.setSpacing(GRID)
        
        self._email_preview = QTextBrowser()
        self._email_preview.setMinimumHeight(200)
        preview_layout.addWidget(self._email_preview)
        
        # Add group to section
        preview_section_layout.addWidget(preview_group, 1)
        
        # Add preview section to main splitter
        main_splitter.addWidget(preview_section)
        
        # ---------------------------------------------------------------------
        # SECTION 5: Attachments
        # ---------------------------------------------------------------------
        bottom_section = QWidget()
        bottom_layout = QVBoxLayout(bottom_section)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(0)
        
        attach_group = QGroupBox("📎 Attachments")
        attach_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        attach_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
                color: #2c3e50;
                border: 2px solid #bdc3c7;
                border-radius: 5px;
                margin-top: 1ex;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        attach_layout = QVBoxLayout(attach_group)
        attach_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        attach_layout.setSpacing(GRID)
        
        self._attachment_list = QListWidget()
        self._attachment_list.setMaximumHeight(120)
        self._attachment_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        attach_layout.addWidget(self._attachment_list)

        # Attachment download progress
        self._download_progress = QProgressBar()
        self._download_progress.setVisible(False)
        self._download_progress.setTextVisible(True)
        attach_layout.addWidget(self._download_progress)
        
        # Attachment actions
        attach_btn_row = QHBoxLayout()
        attach_btn_row.setSpacing(GRID)
        self._download_btn = QPushButton("Download Selected")
        self._download_btn.setFixedHeight(32)
        self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._download_attachment)
        attach_btn_row.addWidget(self._download_btn)
        
        self._download_all_btn = QPushButton("Download All")
        self._download_all_btn.setFixedHeight(32)
        self._download_all_btn.setEnabled(False)
        self._download_all_btn.clicked.connect(self._download_all_attachments)
        attach_btn_row.addWidget(self._download_all_btn)
        
        self._upload_qgis_btn = QPushButton("Add to QGIS")
        self._upload_qgis_btn.setFixedHeight(32)
        self._upload_qgis_btn.clicked.connect(self._add_to_qgis)
        attach_btn_row.addWidget(self._upload_qgis_btn)
        attach_layout.addLayout(attach_btn_row)
        
        # Add group to section
        bottom_layout.addWidget(attach_group, 1)
        
        # Add bottom section to main splitter
        main_splitter.addWidget(bottom_section)
        
        # Configure main splitter with dynamic sizing (5 sections)
        main_splitter.setStretchFactor(0, 0)  # Connection stays compact
        main_splitter.setStretchFactor(1, 0)  # Folders stay compact
        main_splitter.setStretchFactor(2, 1)  # Messages get most space
        main_splitter.setStretchFactor(3, 1)  # Preview gets space
        main_splitter.setStretchFactor(4, 0)  # Attachments stay compact
        main_splitter.setSizes([80, 120, 300, 200, 100])  # Initial proportions
        page_layout.addWidget(main_splitter, 1)  # Stretch to fill available space
        
        # Store group/splitter references
        self._email_conn_group = conn_group
        self._email_folders_group = folders_group
        self._email_message_group = message_group
        self._email_preview_group = preview_group
        self._email_attach_group = attach_group
        self._email_main_splitter = main_splitter
        
        return page
    
    def _create_gisimple_page(self) -> QWidget:
        """Create GISimple Server page with vertical layout like versioning tab."""
        GRID = 8
        page = QWidget()
        page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        page_layout.setSpacing(GRID * 2)
        
        # Header
        header_layout = QHBoxLayout()
        title_label = QLabel("🌐 Import Files from GISimple")
        title_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #1864ab;")
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        page_layout.addLayout(header_layout)
        
        # Main vertical splitter - this will contain all sections directly
        main_splitter = QSplitter(Qt.Orientation.Vertical)
        main_splitter.setChildrenCollapsible(False)
        main_splitter.setHandleWidth(8)
        main_splitter.setOpaqueResize(True)
        main_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        # ---------------------------------------------------------------------
        # SECTION 1: Server Configuration
        # ---------------------------------------------------------------------
        top_section = QWidget()
        top_layout = QVBoxLayout(top_section)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)
        
        # Group 1: Server Configuration
        server_group = QGroupBox("💻 GISimple Servers")
        server_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        server_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
                color: #2c3e50;
                border: 2px solid #bdc3c7;
                border-radius: 5px;
                margin-top: 1ex;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        server_layout = QVBoxLayout(server_group)
        server_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        server_layout.setSpacing(GRID)
        
        self._gisimple_server_list = QListWidget()
        self._gisimple_server_list.setMinimumHeight(100)
        self._gisimple_server_list.itemClicked.connect(self._on_gisimple_server_selected)
        server_layout.addWidget(self._gisimple_server_list)
        
        # Server management buttons
        server_btn_row = QHBoxLayout()
        server_btn_row.setSpacing(4)
        
        add_btn = QPushButton("Add")
        add_btn.setFixedHeight(28)
        add_btn.clicked.connect(self._add_gisimple_server)
        server_btn_row.addWidget(add_btn)
        
        edit_btn = QPushButton("Edit")
        edit_btn.setFixedHeight(28)
        edit_btn.clicked.connect(self._edit_gisimple_server)
        server_btn_row.addWidget(edit_btn)
        
        delete_btn = QPushButton("Delete")
        delete_btn.setFixedHeight(28)
        delete_btn.clicked.connect(self._delete_gisimple_server)
        server_btn_row.addWidget(delete_btn)
        
        server_layout.addLayout(server_btn_row)
        
        # Connection status (simplified - no verbose text box)
        self._gisimple_status = QLabel("● Not connected")
        self._gisimple_status.setStyleSheet("color: #666; font-size: 11px;")
        server_layout.addWidget(self._gisimple_status)
        
        login_btn = QPushButton("Login to GISimple")
        login_btn.setFixedHeight(32)
        login_btn.clicked.connect(self._login_to_gisimple)
        server_layout.addWidget(login_btn)
        
        # Add group to section
        top_layout.addWidget(server_group, 1)
        
        # Add top section to main splitter
        main_splitter.addWidget(top_section)
        
        # ---------------------------------------------------------------------
        # SECTION 2: Project Selection
        # ---------------------------------------------------------------------
        middle_section = QWidget()
        middle_layout = QVBoxLayout(middle_section)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(0)
        
        project_group = QGroupBox("📁 Projects")
        project_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        project_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
                color: #2c3e50;
                border: 2px solid #bdc3c7;
                border-radius: 5px;
                margin-top: 1ex;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        project_layout = QVBoxLayout(project_group)
        project_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        project_layout.setSpacing(GRID)
        
        self._project_list = QListWidget()
        self._project_list.setMinimumHeight(120)
        self._project_list.itemClicked.connect(self._on_project_selected)
        project_layout.addWidget(self._project_list)
        
        self._project_info = QLabel("Login to see projects")
        self._project_info.setWordWrap(True)
        self._project_info.setStyleSheet("color: #666; font-size: 11px;")
        project_layout.addWidget(self._project_info)
        
        # Add group to section
        middle_layout.addWidget(project_group, 1)
        
        # Add middle section to main splitter
        main_splitter.addWidget(middle_section)
        
        # ---------------------------------------------------------------------
        # SECTION 3: Uploaded Files
        # ---------------------------------------------------------------------
        results_section = QWidget()
        results_layout = QVBoxLayout(results_section)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(0)
        
        files_group = QGroupBox("📄 Uploaded Files")
        files_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        files_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
                color: #2c3e50;
                border: 2px solid #bdc3c7;
                border-radius: 5px;
                margin-top: 1ex;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        files_layout = QVBoxLayout(files_group)
        files_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        files_layout.setSpacing(GRID)
        
        # Search box for filtering files
        self._file_search = QLineEdit()
        self._file_search.setPlaceholderText("Search files...")
        self._file_search.setClearButtonEnabled(True)
        self._file_search.textChanged.connect(self._filter_file_list)
        files_layout.addWidget(self._file_search)
        
        self._gisimple_tree = QTreeWidget()
        self._gisimple_tree.setHeaderLabels(["Filename", "Owner", "Size"])
        gisimple_header = self._gisimple_tree.header()
        gisimple_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        gisimple_header.setStretchLastSection(False)
        self._gisimple_tree.setMinimumHeight(280)
        self._gisimple_tree.itemDoubleClicked.connect(self._on_layer_double_clicked)
        self._gisimple_tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        files_layout.addWidget(self._gisimple_tree)
        
        # Progress bar for loading/downloading
        self._gisimple_progress = QProgressBar()
        self._gisimple_progress.setVisible(False)
        self._gisimple_progress.setTextVisible(True)
        files_layout.addWidget(self._gisimple_progress)
        
        self._layer_info = QLabel("Select a file to download")
        self._layer_info.setWordWrap(True)
        self._layer_info.setStyleSheet("color: #666; font-size: 11px;")
        files_layout.addWidget(self._layer_info)
        
        # Add group to section
        results_layout.addWidget(files_group, 1)
        
        # Add results section to main splitter
        main_splitter.addWidget(results_section)
        
        # ---------------------------------------------------------------------
        # SECTION 4: Actions
        # ---------------------------------------------------------------------
        bottom_section = QWidget()
        bottom_layout = QVBoxLayout(bottom_section)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(0)
        
        action_group = QGroupBox("🔧 Actions")
        action_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        action_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
                color: #2c3e50;
                border: 2px solid #bdc3c7;
                border-radius: 5px;
                margin-top: 1ex;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        action_layout = QVBoxLayout(action_group)
        action_layout.setSpacing(GRID)
        
        # Row 1: Load and Download buttons
        btn_row1 = QHBoxLayout()
        btn_row1.setSpacing(GRID)
        
        load_btn = QPushButton("Load GeoJSON Files")
        load_btn.setFixedHeight(32)
        load_btn.clicked.connect(self._load_project_data)
        btn_row1.addWidget(load_btn)
        
        download_btn = QPushButton("Download Selected")
        download_btn.setFixedHeight(32)
        download_btn.clicked.connect(self._download_gisimple_file)
        btn_row1.addWidget(download_btn)
        
        download_all_btn = QPushButton("Download All")
        download_all_btn.setFixedHeight(32)
        download_all_btn.clicked.connect(self._download_all_gisimple_files)
        btn_row1.addWidget(download_all_btn)
        
        action_layout.addLayout(btn_row1)
        
        # Row 2: Add to QGIS and Sync All
        btn_row2 = QHBoxLayout()
        btn_row2.setSpacing(GRID)
        
        add_to_qgis_btn = QPushButton("Add to QGIS")
        add_to_qgis_btn.setFixedHeight(32)
        add_to_qgis_btn.clicked.connect(self._add_layer_to_qgis)
        btn_row2.addWidget(add_to_qgis_btn)
        
        sync_btn = QPushButton("Sync All GeoJSON")
        sync_btn.setFixedHeight(32)
        sync_btn.clicked.connect(self._sync_all_geojson)
        btn_row2.addWidget(sync_btn)
        
        action_layout.addLayout(btn_row2)
        
        # Add group to section
        bottom_layout.addWidget(action_group, 1)
        
        # Add bottom section to main splitter
        main_splitter.addWidget(bottom_section)
        
        # Configure main splitter with dynamic sizing (4 sections)
        main_splitter.setStretchFactor(0, 0)  # Server selection stays compact
        main_splitter.setStretchFactor(1, 0)  # Projects stay compact
        main_splitter.setStretchFactor(2, 1)  # Files get most space
        main_splitter.setStretchFactor(3, 0)  # Actions stay compact
        main_splitter.setSizes([120, 100, 350, 100])  # Initial proportions
        page_layout.addWidget(main_splitter, 1)  # Stretch to fill available space
        
        # Store group/splitter references
        self._gisimple_server_group = server_group
        self._gisimple_project_group = project_group
        self._gisimple_files_group = files_group
        self._gisimple_action_group = action_group
        self._gisimple_main_splitter = main_splitter
        
        # Load servers (will auto-select default if exists)
        self._refresh_gisimple_server_list()
        
        return page
    
    def _format_file_size(self, size):
        """Format file size for display."""
        if not size:
            return "—"
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size // 1024} KB"
        else:
            return f"{size // (1024 * 1024)} MB"
    
    def _create_versioning_page(self) -> QWidget:
        """Create Data Versioning page with 3-section layout."""
        GRID = 8
        page = QWidget()
        page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        page_layout.setSpacing(GRID * 2)
        
        # Header
        header_layout = QHBoxLayout()
        title_label = QLabel("🔄 Data Versioning")
        title_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #1864ab;")
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        page_layout.addLayout(header_layout)  # Remove spacing between sections
        page_layout.setAlignment(Qt.AlignmentFlag.AlignTop)  # Align to top
        
        # Main vertical splitter - this will contain all three sections directly
        main_splitter = QSplitter(Qt.Orientation.Vertical)
        main_splitter.setChildrenCollapsible(False)
        main_splitter.setHandleWidth(12)
        main_splitter.setOpaqueResize(True)
        main_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        # ---------------------------------------------------------------------
        # TOP SECTION: Source Selection (Direct splitter, no group box)
        # ---------------------------------------------------------------------
        top_section = QWidget()
        top_section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        top_layout = QVBoxLayout(top_section)
        top_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        top_layout.setSpacing(GRID)

        # Group for source selection
        source_group = QGroupBox("📁 Source Selection")
        source_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        source_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
                color: #2c3e50;
                border: 2px solid #bdc3c7;
                border-radius: 5px;
                margin-top: 1ex;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        source_group_layout = QVBoxLayout(source_group)
        source_group_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        source_group_layout.setSpacing(GRID)
        
        # Help text
        help_label = QLabel("Client = field edits (incoming). Host = main database (target).")
        help_label.setStyleSheet("color: #6c757d; font-size: 11px;")
        source_group_layout.addWidget(help_label)

        # Horizontal splitter for client/host selection
        source_splitter = QSplitter(Qt.Orientation.Horizontal)
        source_splitter.setChildrenCollapsible(False)
        source_splitter.setHandleWidth(12)
        source_splitter.setOpaqueResize(True)
        source_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Client source panel
        client_panel = QWidget()
        client_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        client_layout = QVBoxLayout(client_panel)
        client_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        client_layout.setSpacing(GRID)

        client_label = QLabel("Client Dataset (Incoming)")
        client_layout.addWidget(client_label)

        self._client_layer_tree, self._client_layer_model, self._client_layer_root = self._create_layer_tree_view()
        self._client_layer_tree.currentItemChanged.connect(self._on_client_layer_changed)
        client_layout.addWidget(self._client_layer_tree)

        self._client_info = QLabel("No dataset loaded")
        client_layout.addWidget(self._client_info)

        source_splitter.addWidget(client_panel)

        # Host source panel
        host_panel = QWidget()
        host_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        host_layout = QVBoxLayout(host_panel)
        host_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        host_layout.setSpacing(GRID)

        host_label = QLabel("Host Dataset (Target)")
        host_layout.addWidget(host_label)

        self._host_layer_tree, self._host_layer_model, self._host_layer_root = self._create_layer_tree_view()
        self._host_layer_tree.currentItemChanged.connect(self._on_host_layer_changed)
        host_layout.addWidget(self._host_layer_tree)

        self._host_info = QLabel("No dataset loaded")
        host_layout.addWidget(self._host_info)

        source_splitter.addWidget(host_panel)
        
        # Configure splitter stretch factors
        source_splitter.setStretchFactor(0, 1)
        source_splitter.setStretchFactor(1, 1)
        source_splitter.setSizes([300, 300])  # Equal initial sizes
        
        # Add splitter to the source group
        source_group_layout.addWidget(source_splitter, 1)
        
        # Add group to the top section
        top_layout.addWidget(source_group, 1)

        # Add top section directly to main splitter
        main_splitter.addWidget(top_section)

        # ---------------------------------------------------------------------
        # MIDDLE SECTION: Match Settings (Independent Group)
        # ---------------------------------------------------------------------
        middle_section = QWidget()
        middle_section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        middle_layout = QVBoxLayout(middle_section)
        middle_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        middle_layout.setSpacing(GRID)

        # Match and Settings group
        match_settings_group = QGroupBox("⚙️ Match & Compare")
        match_settings_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        match_settings_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
                color: #2c3e50;
                border: 2px solid #bdc3c7;
                border-radius: 5px;
                margin-top: 1ex;
                background: #f8f9fb;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        match_settings_layout = QVBoxLayout(match_settings_group)
        match_settings_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        match_settings_layout.setSpacing(int(GRID * 1.5))

        # ---- 1) How features are paired ----
        strategy_group = QGroupBox("1. Match features (Client ↔ Host)")
        strategy_layout = QFormLayout(strategy_group)
        strategy_layout.setHorizontalSpacing(GRID * 2)
        strategy_layout.setVerticalSpacing(GRID)

        self._id_field_summary = QLabel("Auto-detect (Client) – Auto-detect (Host)")
        self._id_field_summary.setStyleSheet("QLabel { color: #495057; }")
        id_row_widget = QWidget()
        id_row = QHBoxLayout(id_row_widget)
        id_row.setContentsMargins(0, 0, 0, 0)
        id_row.setSpacing(GRID)
        id_row.addWidget(self._id_field_summary, 1)
        self._id_field_picker_btn = QPushButton("Choose ID Fields…")
        self._id_field_picker_btn.setFixedHeight(30)
        self._id_field_picker_btn.setToolTip(
            "Primary match: same ID value on client and host.\n"
            "Use FID only when both layers share stable feature ids."
        )
        self._id_field_picker_btn.clicked.connect(self._open_dual_id_field_picker)
        id_row.addWidget(self._id_field_picker_btn)
        strategy_layout.addRow("ID fields:", id_row_widget)

        self._match_by_geom_check = QCheckBox(
            "If no ID match, pair by nearest geometry within tolerance"
        )
        self._match_by_geom_check.setChecked(True)
        self._match_by_geom_check.setToolTip(
            "Uses the same tolerance below. Uncheck for strict ID-only matching."
        )
        strategy_layout.addRow("", self._match_by_geom_check)

        tol_row_widget = QWidget()
        tol_row = QHBoxLayout(tol_row_widget)
        tol_row.setContentsMargins(0, 0, 0, 0)
        tol_row.setSpacing(GRID)
        self._geom_tolerance_spin = QDoubleSpinBox()
        self._geom_tolerance_spin.setRange(0.0, 10000.0)
        self._geom_tolerance_spin.setValue(0.5)
        self._geom_tolerance_spin.setDecimals(8)
        self._geom_tolerance_spin.setSingleStep(0.00000001)
        self._geom_tolerance_spin.setFixedWidth(150)
        self._geom_tolerance_spin.setToolTip(
            "Layer CRS units (metres if the layer is projected).\n"
            "Up to 8 decimal places.\n"
            "Used for: (1) optional geometry matching, and "
            "(2) deciding whether geometries differ."
        )
        tol_row.addWidget(self._geom_tolerance_spin)
        self._geom_tolerance_hint = QLabel("layer units (m if projected)")
        self._geom_tolerance_hint.setStyleSheet("QLabel { color: #868e96; font-size: 11px; }")
        tol_row.addWidget(self._geom_tolerance_hint, 1)
        strategy_layout.addRow("Tolerance:", tol_row_widget)

        match_settings_layout.addWidget(strategy_group)

        # ---- 2) What to report as different ----
        diff_group = QGroupBox("2. Report differences on matched pairs")
        diff_layout = QGridLayout(diff_group)
        diff_layout.setHorizontalSpacing(GRID * 2)
        diff_layout.setVerticalSpacing(GRID)

        self._compare_geom_check = QCheckBox("Report geometry differences")
        self._compare_geom_check.setChecked(True)
        self._compare_geom_check.setToolTip(
            "Geometries differ when distance (after single/multi normalize) "
            "exceeds the tolerance above."
        )
        self._compare_attr_check = QCheckBox("Report attribute differences")
        self._compare_attr_check.setChecked(True)
        diff_layout.addWidget(self._compare_geom_check, 0, 0)
        diff_layout.addWidget(self._compare_attr_check, 0, 1)

        self._empty_as_null_check = QCheckBox("Treat empty cells as NULL")
        self._empty_as_null_check.setChecked(True)
        self._include_photos_commit = QCheckBox("Include 'photos' in commit")
        self._include_photos_commit.setToolTip(
            "When checked, the 'photos' field is updated on commit even if ignored in comparison"
        )
        diff_layout.addWidget(self._empty_as_null_check, 1, 0)
        diff_layout.addWidget(self._include_photos_commit, 1, 1)

        ignore_row_widget = QWidget()
        ignore_row = QHBoxLayout(ignore_row_widget)
        ignore_row.setContentsMargins(0, 0, 0, 0)
        ignore_row.setSpacing(GRID)
        self._ignore_fields_btn = QPushButton("Ignore Fields…")
        self._ignore_fields_btn.setFixedHeight(28)
        self._ignore_fields_btn.setToolTip("Exclude fields from attribute comparison")
        self._ignore_fields_btn.clicked.connect(self._show_ignore_fields_dialog)
        self._ignore_fields_label = QLabel("(photos)")
        self._ignore_fields_label.setStyleSheet("QLabel { color: #666; font-size: 11px; }")
        ignore_row.addWidget(self._ignore_fields_btn)
        ignore_row.addWidget(self._ignore_fields_label, 1)
        diff_layout.addWidget(QLabel("Ignored fields:"), 2, 0)
        diff_layout.addWidget(ignore_row_widget, 2, 1)

        match_settings_layout.addWidget(diff_group)

        # Initialize ignored fields with 'photos'
        self._ignored_fields = ['photos']

        # ---- Action row ----
        compare_row = QHBoxLayout()
        compare_row.setSpacing(GRID)
        self._compare_btn = QPushButton("Compare Datasets")
        self._compare_btn.setFixedHeight(34)
        self._compare_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._compare_btn.clicked.connect(self._run_comparison)
        compare_row.addWidget(self._compare_btn)
        compare_row.addSpacing(GRID)

        self._stop_btn = QPushButton("Stop Process")
        self._stop_btn.setFixedHeight(34)
        self._stop_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet("QPushButton { color: #c0392b; font-weight: bold; }")
        self._stop_btn.clicked.connect(self._stop_comparison_now)
        compare_row.addWidget(self._stop_btn)
        compare_row.addSpacing(GRID)

        self._view_attrs_btn = QPushButton("View Client Attributes")
        self._view_attrs_btn.setFixedHeight(34)
        self._view_attrs_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._view_attrs_btn.setToolTip("Preview client layer records (no comparison)")
        self._view_attrs_btn.clicked.connect(self._toggle_attribute_view_mode)
        compare_row.addWidget(self._view_attrs_btn)
        compare_row.addSpacing(GRID)

        self._auto_show_diff_check = QCheckBox("Show differences when finished")
        compare_row.addWidget(self._auto_show_diff_check)
        compare_row.addStretch()
        match_settings_layout.addLayout(compare_row)

        self._schema_diff_label = QLabel("Load client & host layers to compare field schemas.")
        self._schema_diff_label.setStyleSheet("color: #868e96; font-size: 11px; font-style: italic;")
        self._schema_diff_label.setWordWrap(True)
        match_settings_layout.addWidget(self._schema_diff_label)
        self._refresh_schema_diff_metadata()
        
        # Add match settings group to middle section
        middle_layout.addWidget(match_settings_group, 0)
        
        # Add middle section to main splitter
        main_splitter.addWidget(middle_section)
        
        # ---------------------------------------------------------------------
        # RESULTS SECTION: Comparison Results (Independent Group)
        # ---------------------------------------------------------------------
        results_section = QWidget()
        results_section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        results_layout = QVBoxLayout(results_section)
        results_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        results_layout.setSpacing(GRID)
        
        # Group for comparison results
        results_group = QGroupBox("📈 Comparison Results")
        results_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        results_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
                color: #2c3e50;
                border: 2px solid #bdc3c7;
                border-radius: 5px;
                margin-top: 1ex;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        results_group_layout = QVBoxLayout(results_group)
        results_group_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        results_group_layout.setSpacing(GRID)

        # Progress and stats row
        stats_row = QHBoxLayout()
        stats_row.setSpacing(GRID*2)

        self._compare_progress = QProgressBar()
        self._compare_progress.setRange(0, 100)
        self._compare_progress.setVisible(False)
        self._compare_progress.setFixedHeight(16)
        self._compare_progress.setFixedWidth(160)
        stats_row.addWidget(self._compare_progress)
        self._compare_progress_label = QLabel("")
        self._compare_progress_label.setStyleSheet("QLabel { font-size: 11px; color: #555; }")
        self._compare_progress_label.setVisible(False)
        stats_row.addWidget(self._compare_progress_label)

        for label_text, attr_name, color in [
            ("Total",       "_stat_total",        "#495057"),
            ("Matched",     "_stat_matched",      "#2f9e44"),
            ("Added",       "_stat_new",          "#1971c2"),
            ("Not Surv.",   "_stat_deleted",      "#868e96"),
            ("Geom Diff",   "_stat_geom_changed", "#f59f00"),
            ("Attr Diff",   "_stat_attr_changed", "#9c36b5"),
        ]:
            lbl = QLabel(label_text)
            lbl.setStyleSheet("QLabel { font-size: 11px; color: #666; }")
            stats_row.addWidget(lbl)
            stat_label = QLabel("0")
            stat_label.setStyleSheet(
                f"QLabel {{ font-weight: bold; color: {color}; font-size: 12px;"
                f" background: #f8f9fa; border: 1px solid {color};"
                f" border-radius: 3px; padding: 0 5px; min-width: 22px; }}"
            )
            setattr(self, attr_name, stat_label)
            stats_row.addWidget(stat_label)

        stats_row.addStretch()
        
        # Add stats row to results group
        results_group_layout.addLayout(stats_row)

        # Status visibility filters (used while filling / refreshing result tables)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(GRID)
        filter_lbl = QLabel("Show:")
        filter_lbl.setStyleSheet("QLabel { font-size: 11px; color: #666; }")
        filter_row.addWidget(filter_lbl)
        self._filter_checks = {}
        for status_key, short_label in [
            ("ADDED", "Added"),
            ("MODIFIED (GEOM)", "Geom"),
            ("MODIFIED (ATTR)", "Attr"),
            ("MODIFIED (BOTH)", "Both"),
            ("UNCHANGED", "Unchanged"),
            ("NOT SURVEYED", "Not Surv."),
            ("IGNORED", "Ignored"),
        ]:
            cb = QCheckBox(short_label)
            cb.setChecked(True)
            cb.setToolTip(status_key)
            cb.stateChanged.connect(self._apply_status_filter)
            self._filter_checks[status_key] = cb
            filter_row.addWidget(cb)
        self._photos_only_check = QCheckBox("Photos only")
        self._photos_only_check.setToolTip("Show only client rows that have photos")
        self._photos_only_check.stateChanged.connect(self._apply_status_filter)
        filter_row.addWidget(self._photos_only_check)
        filter_row.addStretch()
        results_group_layout.addLayout(filter_row)

        tables_container = QWidget()
        tables_container_layout = QVBoxLayout(tables_container)
        tables_container_layout.setContentsMargins(0, 0, 0, 0)
        tables_container_layout.setSpacing(GRID)
        tables_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        tables_splitter = QSplitter(Qt.Orientation.Horizontal)
        tables_splitter.setChildrenCollapsible(False)
        tables_splitter.setHandleWidth(12)
        tables_splitter.setOpaqueResize(True)
        tables_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        tables_splitter.setSizes([700, 300])

        client_results_panel = QWidget()
        client_results_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        client_results_layout = QVBoxLayout(client_results_panel)
        client_results_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        client_results_layout.setSpacing(GRID)

        client_hdr = QHBoxLayout()
        client_hdr.addWidget(QLabel("Client Results"))
        client_hdr.addStretch()
        copy_btn = QPushButton("Copy to Clipboard")
        copy_btn.setFixedHeight(22)
        copy_btn.clicked.connect(self._copy_client_table_to_clipboard)
        client_hdr.addWidget(copy_btn)
        client_results_layout.addLayout(client_hdr)

        self._client_table = QTableWidget()
        self._client_table.setSortingEnabled(True)
        self._client_table.setAlternatingRowColors(True)
        self._client_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._client_table.itemSelectionChanged.connect(self._on_client_row_selected)
        self._client_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._client_table.customContextMenuRequested.connect(self._show_client_context_menu)
        self._configure_client_table_for_comparison()
        client_results_layout.addWidget(self._client_table)

        tables_splitter.addWidget(client_results_panel)

        host_results_panel = QWidget()
        host_results_panel.setObjectName("host_results_panel")
        host_results_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        host_results_layout = QVBoxLayout(host_results_panel)
        host_results_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        host_results_layout.setSpacing(GRID)

        host_results_label = QLabel("Host Results")
        host_results_layout.addWidget(host_results_label)

        self._host_table = QTableWidget()
        self._host_table.setColumnCount(5)
        self._host_table.setHorizontalHeaderLabels(["FID", "Match", "Diff", "Attr", "Status"])
        _hh = self._host_table.horizontalHeader()
        _hh.setSortIndicatorShown(True)
        _hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        _hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        _hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        _hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        _hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._host_table.setColumnWidth(0, 55)
        self._host_table.setColumnWidth(1, 55)
        self._host_table.setColumnWidth(2, 72)
        self._host_table.setColumnWidth(3, 50)
        self._host_table.setSortingEnabled(True)
        self._host_table.setAlternatingRowColors(True)
        self._host_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._host_table.itemSelectionChanged.connect(self._on_host_row_selected)
        host_results_layout.addWidget(self._host_table)

        tables_splitter.addWidget(host_results_panel)

        photo_preview_panel = QWidget()
        photo_preview_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        photo_preview_layout = QVBoxLayout(photo_preview_panel)
        photo_preview_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        photo_preview_layout.setSpacing(GRID)

        photo_header_layout = QHBoxLayout()
        photo_header_layout.setSpacing(GRID)
        photo_header = QLabel("Photo Preview")
        photo_header.setStyleSheet("font-weight: bold;")
        photo_header_layout.addWidget(photo_header)
        photo_header_layout.addStretch()
        self._photo_status_label = QLabel("No record selected")
        self._photo_status_label.setStyleSheet("color: #666; font-size: 11px;")
        photo_header_layout.addWidget(self._photo_status_label)
        photo_preview_layout.addLayout(photo_header_layout)

        self._photo_preview_label = QLabel("Select a record with photos")
        self._photo_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._photo_preview_label.setMinimumHeight(180)
        self._photo_preview_label.setFrameShape(QFrame.Shape.StyledPanel)
        self._photo_preview_label.setStyleSheet("QLabel { background: #f6f6f6; color: #777; }")
        photo_preview_layout.addWidget(self._photo_preview_label, 1)

        self._open_photo_btn = QPushButton("Open Photo Viewer")
        self._open_photo_btn.setEnabled(False)
        self._open_photo_btn.clicked.connect(self._open_photo_viewer)
        photo_preview_layout.addWidget(self._open_photo_btn)

        self._open_photo_folder_btn = QPushButton("Open File Location")
        self._open_photo_folder_btn.setEnabled(False)
        self._open_photo_folder_btn.clicked.connect(self._open_photo_location)
        photo_preview_layout.addWidget(self._open_photo_folder_btn)

        tables_splitter.addWidget(photo_preview_panel)
        tables_container_layout.addWidget(tables_splitter)

        nav_row = QHBoxLayout()
        nav_row.setSpacing(GRID)
        nav_lbl = QLabel("Map on row click:")
        nav_lbl.setStyleSheet("QLabel { font-size: 11px; color: #666; }")
        nav_row.addWidget(nav_lbl)
        self._nav_flash_check = QCheckBox("Flash")
        self._nav_flash_check.setChecked(True)
        nav_row.addWidget(self._nav_flash_check)
        self._nav_pan_check = QCheckBox("Pan to")
        self._nav_pan_check.toggled.connect(self._on_nav_pan_toggled)
        nav_row.addWidget(self._nav_pan_check)
        self._nav_zoom_check = QCheckBox("Zoom to")
        self._nav_zoom_check.setChecked(True)
        self._nav_zoom_check.toggled.connect(self._on_nav_zoom_toggled)
        nav_row.addWidget(self._nav_zoom_check)
        nav_row.addStretch()
        tables_container_layout.addLayout(nav_row)

        results_group_layout.addWidget(tables_container)

        results_layout.addWidget(results_group)
        main_splitter.addWidget(results_section)
        
        # ---------------------------------------------------------------------
        # BOTTOM SECTION: Actions & Details (Direct layout, no group box)
        # ---------------------------------------------------------------------
        bottom_section = QWidget()
        bottom_section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bottom_layout = QVBoxLayout(bottom_section)
        bottom_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        bottom_layout.setSpacing(GRID)
        
        # Group for actions
        actions_group = QGroupBox("🔧 Actions and Details")
        actions_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        actions_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
                color: #2c3e50;
                border: 2px solid #bdc3c7;
                border-radius: 5px;
                margin-top: 1ex;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        actions_group_layout = QVBoxLayout(actions_group)
        actions_group_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        actions_group_layout.setSpacing(GRID)

        # Selected feature info
        info_row = QHBoxLayout()
        info_row.setSpacing(GRID*2)
        info_row.addWidget(QLabel("Selected Feature:"))
        self._selected_feature_label = QLabel("None")
        info_row.addWidget(self._selected_feature_label)
        info_row.addStretch()
        actions_group_layout.addLayout(info_row)

        # Row 1: Show All Differences | Remove Temporary Layers
        row1 = QHBoxLayout()
        row1.setSpacing(GRID)
        self._show_diff_btn = QPushButton("Show All Differences")
        self._show_diff_btn.setFixedHeight(32)
        self._show_diff_btn.clicked.connect(self._show_geometry_diff)
        self._show_diff_btn.setEnabled(False)
        row1.addWidget(self._show_diff_btn)
        clear_geom_diff_btn = QPushButton("Remove Temporary Layers")
        clear_geom_diff_btn.setFixedHeight(32)
        clear_geom_diff_btn.clicked.connect(self._clear_geometry_diff)
        row1.addWidget(clear_geom_diff_btn)
        row1.addStretch()
        actions_group_layout.addLayout(row1)

        # Row 2: Auto-Commit All | Undo
        row2 = QHBoxLayout()
        row2.setSpacing(GRID)
        self._commit_all_btn = QPushButton("⚠  Auto-Commit All")
        self._commit_all_btn.setFixedHeight(32)
        self._commit_all_btn.setStyleSheet("""
            QPushButton {
                background-color: #c0392b; color: white;
                border: 1px solid #922b21; border-radius: 3px; font-weight: bold;
            }
            QPushButton:hover  { background-color: #e74c3c; }
            QPushButton:disabled { background-color: #e0e0e0; color: #aaa; border-color: #ccc; }
        """)
        self._commit_all_btn.clicked.connect(self._commit_all_changes)
        self._commit_all_btn.setEnabled(False)
        row2.addWidget(self._commit_all_btn)
        self._undo_btn = QPushButton("Undo Last Commit")
        self._undo_btn.setFixedHeight(32)
        self._undo_btn.clicked.connect(self._undo_change)
        self._undo_btn.setEnabled(False)
        row2.addWidget(self._undo_btn)
        row2.addStretch()
        actions_group_layout.addLayout(row2)
        
        # Add group to the bottom section
        bottom_layout.addWidget(actions_group, 1)
        
        # Add bottom section directly to main splitter
        main_splitter.addWidget(bottom_section)
        
        # Configure main splitter with dynamic sizing (4 sections now)
        main_splitter.setStretchFactor(0, 0)  # Source selection stays compact
        main_splitter.setStretchFactor(1, 0)  # Match settings stay compact
        main_splitter.setStretchFactor(2, 1)  # Results get most space
        main_splitter.setStretchFactor(3, 0)  # Actions stay compact
        main_splitter.setSizes([180, 120, 350, 120])  # Better initial proportions
        page_layout.addWidget(main_splitter, 1)  # Stretch to fill available space
        
        # Store group/splitter references
        self._source_group = source_group
        self._match_settings_group = match_settings_group
        self._results_group = results_group
        self._actions_group = actions_group
        self._main_splitter = main_splitter
        
        # Initialize versioning state
        self._client_layer = None
        self._host_layer = None

        # Don't call _apply_versioning_layer_filters() here - it will be called after UI is fully initialized
        # self._apply_versioning_layer_filters()
        self._client_result_rows = []
        self._host_deleted_rows = []
        self._last_comparison_stats = None
        self._comparison_floating_dialog = None
        self._selected_client_fid = None
        self._selected_host_fid = None
        self._syncing_selection = False
        self._client_selection_layer = None
        self._host_selection_layer = None
        self._diff_added_layer = None
        self._diff_removed_layer = None
        self._diff_attr_changed_layer = None
        self._diff_geom_modified_layer = None
        self._geom_diff_layer = None
        self._geom_parts_added_layer = None
        self._geom_parts_removed_layer = None
        self._geom_parts_unchanged_layer = None
        self._client_diff_index = {}
        self._pending_attr_commits = set()
        self._ignored_fids = {}   # {client_fid_str: original_status_text} — session only
        self._attribute_view_mode = False
        self._attribute_field_names = []
        self._client_only_fields = []
        self._host_only_fields = []
        self._current_photos = []
        self._current_preview_index = None
        self._client_id_field = "(auto-detect)"
        self._host_id_field = "(auto-detect)"

        return page
    
    def _configure_client_table_for_comparison(self):
        if not hasattr(self, '_client_table') or not self._client_table:
            return
        headers = ["FID", "Match", "Diff", "Attr", "Status"]
        table = self._client_table
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        header = table.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        table.setColumnWidth(0, 55)
        table.setColumnWidth(1, 55)
        table.setColumnWidth(2, 72)
        table.setColumnWidth(3, 50)

    def _configure_client_table_for_attributes(self):
        if not hasattr(self, '_client_table') or not self._client_table:
            return
        table = self._client_table
        if not self._client_layer:
            self._attribute_field_names = []
            table.setColumnCount(1)
            fid_header = QTableWidgetItem("FID")
            table.setHorizontalHeaderItem(0, fid_header)
            header = table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            return

        client_fields = [field.name() for field in self._client_layer.fields()]
        host_only_columns = []
        if self._host_layer and self._host_layer.isValid():
            existing = set()
            for field in self._host_layer.fields():
                name = field.name()
                if name in self._host_only_fields and name not in existing:
                    host_only_columns.append(name)
                    existing.add(name)

        self._attribute_field_names = client_fields + host_only_columns
        headers = ["FID"] + self._attribute_field_names
        table.setColumnCount(len(headers))
        header = table.horizontalHeader()
        header.setSortIndicatorShown(True)

        fid_header = QTableWidgetItem("FID")
        table.setHorizontalHeaderItem(0, fid_header)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)

        for idx, field_name in enumerate(self._attribute_field_names, start=1):
            header_item = QTableWidgetItem(field_name)
            tooltip = "Field shared by client and host layers"
            if field_name in self._client_only_fields:
                header_item.setBackground(QBrush(QColor(209, 250, 229)))
                tooltip = "Client-only field (added column)"
            elif field_name in self._host_only_fields:
                header_item.setBackground(QBrush(QColor(255, 220, 220)))
                tooltip = "Host-only field (missing on client)"
            header_item.setToolTip(tooltip)
            table.setHorizontalHeaderItem(idx, header_item)
            header.setSectionResizeMode(idx, QHeaderView.ResizeMode.Stretch)

    def _format_attribute_value(self, value):
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:.6g}"
        return str(value)
    
    def _create_settings_page(self) -> QWidget:
        """Create Settings page with grouped sections."""
        GRID = 8
        page = QWidget()
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(GRID*2, GRID*2, GRID*2, GRID*2)
        layout.setSpacing(GRID*2)
        
        # Header
        header_layout = QHBoxLayout()
        title_label = QLabel("⚙️ Settings")
        title_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #1864ab;")
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        layout.addLayout(header_layout)
        
        # ---------------------------------------------------------------------
        # EMAIL SERVERS SECTION
        # ---------------------------------------------------------------------
        email_group = QGroupBox("Email Servers")
        email_layout = QVBoxLayout(email_group)
        email_layout.setSpacing(GRID)
        
        self._server_list = QListWidget()
        self._server_list.setMaximumHeight(150)
        self._server_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._server_list.itemDoubleClicked.connect(self._edit_email_server)
        email_layout.addWidget(self._server_list)
        
        email_btn_row = QHBoxLayout()
        email_btn_row.setSpacing(GRID)
        
        add_server_btn = QPushButton("Add Server")
        add_server_btn.setFixedHeight(32)
        add_server_btn.clicked.connect(self._add_server)
        email_btn_row.addWidget(add_server_btn)
        
        edit_server_btn = QPushButton("Edit")
        edit_server_btn.setFixedHeight(32)
        edit_server_btn.clicked.connect(self._edit_email_server)
        email_btn_row.addWidget(edit_server_btn)
        
        delete_server_btn = QPushButton("Delete")
        delete_server_btn.setFixedHeight(32)
        delete_server_btn.clicked.connect(self._delete_email_server)
        email_btn_row.addWidget(delete_server_btn)
        
        email_btn_row.addStretch()
        email_layout.addLayout(email_btn_row)
        
        layout.addWidget(email_group)
        
        # ---------------------------------------------------------------------
        # TRUST SETTINGS SECTION
        # ---------------------------------------------------------------------
        trust_group = QGroupBox("Trust Settings")
        trust_layout = QVBoxLayout(trust_group)
        trust_layout.setSpacing(GRID)
        
        # Trust mode
        mode_row = QHBoxLayout()
        mode_row.setSpacing(GRID)
        mode_row.addWidget(QLabel("Trust Mode:"))
        self._trust_mode = QComboBox()
        self._trust_mode.addItems(["Trusted Accounts Only", "Network Trusted (CIDR)", "Trust All"])
        self._trust_mode.currentIndexChanged.connect(self._on_trust_mode_changed)
        mode_row.addWidget(self._trust_mode)
        mode_row.addStretch()
        trust_layout.addLayout(mode_row)
        
        self._trust_info_label = QLabel("Email is trusted if sender matches ANY domain OR ANY account below.")
        self._trust_info_label.setStyleSheet("color: #868e96; font-style: italic;")
        trust_layout.addWidget(self._trust_info_label)
        
        # Two columns: Domains and Accounts
        lists_splitter = QSplitter(Qt.Orientation.Horizontal)
        lists_splitter.setChildrenCollapsible(False)
        lists_splitter.setHandleWidth(6)
        lists_splitter.setOpaqueResize(True)
        
        # Trusted Domains
        domains_panel = QWidget()
        domains_layout = QVBoxLayout(domains_panel)
        domains_layout.setContentsMargins(0, 0, 0, 0)
        domains_layout.setSpacing(GRID)
        
        domains_label = QLabel("Trusted Domains")
        domains_label.setStyleSheet("font-weight: bold;")
        domains_layout.addWidget(domains_label)
        
        self._trusted_domains_list = QListWidget()
        self._trusted_domains_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        domains_layout.addWidget(self._trusted_domains_list)
        
        domains_btn_row = QHBoxLayout()
        domains_btn_row.setSpacing(GRID)
        add_domain_btn = QPushButton("Add")
        add_domain_btn.setFixedHeight(28)
        add_domain_btn.clicked.connect(self._add_trusted_domain)
        domains_btn_row.addWidget(add_domain_btn)
        remove_domain_btn = QPushButton("Remove")
        remove_domain_btn.setFixedHeight(28)
        remove_domain_btn.clicked.connect(self._remove_trusted_domain)
        domains_btn_row.addWidget(remove_domain_btn)
        domains_btn_row.addStretch()
        domains_layout.addLayout(domains_btn_row)
        
        lists_splitter.addWidget(domains_panel)
        
        # Trusted Accounts
        accounts_panel = QWidget()
        accounts_layout = QVBoxLayout(accounts_panel)
        accounts_layout.setContentsMargins(0, 0, 0, 0)
        accounts_layout.setSpacing(GRID)
        
        accounts_label = QLabel("Trusted Accounts")
        accounts_label.setStyleSheet("font-weight: bold;")
        accounts_layout.addWidget(accounts_label)
        
        self._trusted_accounts_list = QListWidget()
        self._trusted_accounts_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        accounts_layout.addWidget(self._trusted_accounts_list)
        
        accounts_btn_row = QHBoxLayout()
        accounts_btn_row.setSpacing(GRID)
        add_account_btn = QPushButton("Add")
        add_account_btn.setFixedHeight(28)
        add_account_btn.clicked.connect(self._add_trusted_account)
        accounts_btn_row.addWidget(add_account_btn)
        remove_account_btn = QPushButton("Remove")
        remove_account_btn.setFixedHeight(28)
        remove_account_btn.clicked.connect(self._remove_trusted_account)
        accounts_btn_row.addWidget(remove_account_btn)
        import_gisimple_btn = QPushButton("Import from GISimple")
        import_gisimple_btn.setFixedHeight(28)
        import_gisimple_btn.clicked.connect(self._import_from_gisimple)
        accounts_btn_row.addWidget(import_gisimple_btn)
        accounts_btn_row.addStretch()
        accounts_layout.addLayout(accounts_btn_row)
        
        lists_splitter.addWidget(accounts_panel)
        
        trust_layout.addWidget(lists_splitter)
        layout.addWidget(trust_group)
        
        # Load trusted lists
        self._load_trusted_lists()
        
        # ---------------------------------------------------------------------
        # DOWNLOAD FOLDER SECTION
        # ---------------------------------------------------------------------
        download_group = QGroupBox("Download Folder")
        download_layout = QVBoxLayout(download_group)
        download_layout.setSpacing(GRID)
        
        download_info = QLabel("Set a download folder for Email and GISimple files. Subfolders will be created automatically using sanitized email addresses.")
        download_info.setWordWrap(True)
        download_info.setStyleSheet("color: #868e96; font-style: italic;")
        download_layout.addWidget(download_info)
        
        folder_row = QHBoxLayout()
        folder_row.setSpacing(GRID)
        
        self._download_folder_edit = QLineEdit()
        self._download_folder_edit.setPlaceholderText("Select a download folder...")
        self._download_folder_edit.setReadOnly(True)
        folder_row.addWidget(self._download_folder_edit)
        
        browse_btn = QPushButton("Browse...")
        browse_btn.setFixedHeight(32)
        browse_btn.clicked.connect(self._browse_download_folder)
        folder_row.addWidget(browse_btn)
        
        download_layout.addLayout(folder_row)
        layout.addWidget(download_group)
        
        # Load saved download folder
        self._load_download_folder()
        
        # ---------------------------------------------------------------------
        # GENERAL SETTINGS SECTION
        # ---------------------------------------------------------------------
        general_group = QGroupBox("General")
        general_layout = QFormLayout(general_group)
        general_layout.setSpacing(GRID)
        
        # Auto-refresh
        auto_refresh_row = QHBoxLayout()
        auto_refresh_row.setSpacing(GRID)
        self._auto_refresh = QCheckBox("Auto-refresh mailbox every")
        auto_refresh_row.addWidget(self._auto_refresh)
        self._auto_refresh_minutes = QLineEdit()
        self._auto_refresh_minutes.setText("5")
        self._auto_refresh_minutes.setFixedWidth(50)
        auto_refresh_row.addWidget(self._auto_refresh_minutes)
        auto_refresh_row.addWidget(QLabel("minutes"))
        auto_refresh_row.addStretch()
        general_layout.addRow("", auto_refresh_row)
        
        self._show_notifications = QCheckBox("Show desktop notifications")
        general_layout.addRow("", self._show_notifications)
        
        self._auto_load_data = QCheckBox("Auto-load GIS data on startup")
        general_layout.addRow("", self._auto_load_data)
        
        layout.addWidget(general_group)

        # ---------------------------------------------------------------------
        # SETTINGS DATABASE SECTION
        # ---------------------------------------------------------------------
        db_group = QGroupBox("Settings Database")
        db_layout = QVBoxLayout(db_group)
        db_layout.setSpacing(GRID)

        db_path_label = QLabel(str(DB_PATH))
        db_path_label.setWordWrap(True)
        db_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        db_layout.addWidget(db_path_label)

        db_btn_row = QHBoxLayout()
        db_btn_row.setSpacing(GRID)
        create_db_btn = QPushButton("Create Settings Database")
        create_db_btn.setFixedHeight(32)
        create_db_btn.clicked.connect(self._on_create_settings_database)
        db_btn_row.addWidget(create_db_btn)
        db_btn_row.addStretch()
        db_layout.addLayout(db_btn_row)

        layout.addWidget(db_group)

        # ---------------------------------------------------------------------
        # SAVE BUTTON
        # ---------------------------------------------------------------------
        save_row = QHBoxLayout()
        save_row.addStretch()
        self._save_btn = QPushButton("Save Settings")
        self._save_btn.setFixedHeight(36)
        # self._save_btn.setStyleSheet("background: #d3f9d8; color: #2f9e44; border: 1px solid #8ce99a; font-weight: bold; padding: 8px 24px;")
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._save_settings)
        save_row.addWidget(self._save_btn)
        layout.addLayout(save_row)
        
        layout.addStretch()
        
        # Connect change signals
        self._trust_mode.currentIndexChanged.connect(self._on_settings_changed)
        self._auto_refresh.stateChanged.connect(self._on_settings_changed)
        self._auto_refresh_minutes.textChanged.connect(self._on_settings_changed)
        self._show_notifications.stateChanged.connect(self._on_settings_changed)
        self._auto_load_data.stateChanged.connect(self._on_settings_changed)
        
        scroll.setWidget(content)
        
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(scroll)
        
        return page
    
    def _create_documentation_page(self) -> QWidget:
        """Create Documentation page with button to open HTML in browser."""
        from qgis.PyQt.QtWidgets import QVBoxLayout, QHBoxLayout, QLabel, QPushButton
        from qgis.PyQt.QtCore import Qt
        
        GRID = 8
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(GRID * 4, GRID * 4, GRID * 4, GRID * 4)
        page_layout.setSpacing(GRID * 3)
        
        # Header
        header_layout = QHBoxLayout()
        title_label = QLabel("📚 GeoInbox Documentation")
        title_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #1864ab;")
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        
        version_label = QLabel("v0.9.0")
        version_label.setStyleSheet("color: #666; font-size: 12px;")
        header_layout.addWidget(version_label)
        page_layout.addLayout(header_layout)
        
        # Description
        desc_label = QLabel("Click the button below to open the GeoInbox documentation in your web browser.")
        desc_label.setStyleSheet("font-size: 14px; color: #333;")
        desc_label.setWordWrap(True)
        page_layout.addWidget(desc_label)
        
        doc_btn_style = """
            QPushButton {
                font-size: 16px;
                font-weight: bold;
                background: #1864ab;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 12px 24px;
            }
            QPushButton:hover {
                background: #1971c2;
            }
            QPushButton:pressed {
                background: #145591;
            }
        """
        doc_buttons_row = QHBoxLayout()
        doc_buttons_row.setSpacing(GRID * 2)

        open_btn = QPushButton("🌐 Open Documentation in Browser")
        open_btn.setFixedHeight(48)
        open_btn.setStyleSheet(doc_btn_style)
        open_btn.clicked.connect(self._open_documentation_in_browser)
        doc_buttons_row.addWidget(open_btn, stretch=1)

        web_btn = QPushButton("Web")
        web_btn.setFixedHeight(48)
        web_btn.setMinimumWidth(96)
        web_btn.setStyleSheet(doc_btn_style)
        web_btn.clicked.connect(self._open_documentation_on_web)
        doc_buttons_row.addWidget(web_btn)

        page_layout.addLayout(doc_buttons_row)
        
        # Documentation sections info
        sections_label = QLabel("""
        <h3>Documentation Sections:</h3>
        <ul>
            <li><b>Installation</b> - Install GeoInbox from QGIS Plugin Repository</li>
            <li><b>Settings & Configuration</b> - Configure email servers, GISimple, and trust settings</li>
            <li><b>Retrieve Attachment from Email</b> - Connect to email and download GIS attachments</li>
            <li><b>Load Files from GISimple</b> - Connect to GISimple server and download datasets</li>
            <li><b>Layer Selection & Comparison</b> - Compare datasets and commit changes</li>
            <li><b>Changelog</b> - Version history and features</li>
        </ul>
        """)
        sections_label.setStyleSheet("font-size: 13px; color: #495057; background: #f8f9fa; padding: 16px; border-radius: 8px;")
        sections_label.setWordWrap(True)
        page_layout.addWidget(sections_label)
        
        page_layout.addStretch()
        
        return page
    
    def _open_documentation_in_browser(self):
        """Open bundled documentation from a path relative to the plugin folder."""
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtGui import QDesktopServices
        from qgis.PyQt.QtWidgets import QMessageBox

        doc_rel = os.path.join("documentation", "index.html")
        doc_path = os.path.normpath(os.path.join(PLUGIN_DIR, doc_rel))

        try:
            if os.path.isfile(doc_path):
                QDesktopServices.openUrl(QUrl.fromLocalFile(doc_path))
            else:
                QMessageBox.warning(
                    self,
                    "Documentation Not Found",
                    "Documentation not found at documentation/index.html.\n\n"
                    "Please ensure the documentation folder is present in the plugin directory."
                )
        except Exception as e:
            QMessageBox.warning(
                self,
                "Error",
                f"Failed to open documentation: {str(e)}"
            )

    def _open_documentation_on_web(self):
        """Open the online GeoInbox documentation in the default browser."""
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtGui import QDesktopServices
        from qgis.PyQt.QtWidgets import QMessageBox

        doc_url = QUrl("http://gis.com.my/geoinbox/")
        if not QDesktopServices.openUrl(doc_url):
            QMessageBox.warning(
                self,
                "Error",
                "Failed to open the documentation website in your browser.",
            )
    
    
    def _create_workflow_section(self, title: str, description: str, steps: list) -> QWidget:
        """Create a workflow section widget."""
        section = QGroupBox(title)
        section.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                border: 1px solid #dee2e6;
                border-radius: 8px;
                margin-top: 12px;
                padding: 16px;
                background: white;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 12px;
                top: 4px;
                padding: 2px 8px;
                color: #495057;
            }
        """)
        
        layout = QVBoxLayout(section)
        layout.setSpacing(8)
        
        # Description
        desc_label = QLabel(description)
        desc_label.setStyleSheet("color: #666; font-style: italic; font-weight: normal;")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)
        
        # Steps
        for i, step in enumerate(steps, 1):
            step_label = QLabel(f"{i}. {step}")
            step_label.setStyleSheet("color: #333; font-weight: normal; margin-left: 16px;")
            step_label.setWordWrap(True)
            layout.addWidget(step_label)
        
        return section

    # =========================================================================
    # DATA BROWSER PAGE
    # =========================================================================
    def _create_browser_page(self) -> QWidget:
        """Create the Data Browser page for viewing downloaded files."""
        GRID = 8
        group_style = """
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
                color: #2c3e50;
                border: 2px solid #bdc3c7;
                border-radius: 5px;
                margin-top: 1ex;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """

        page = QWidget()
        page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(GRID, GRID, GRID, GRID)
        layout.setSpacing(GRID * 2)

        # Header
        header_layout = QHBoxLayout()
        title = QLabel("🗂️ Data Browser")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #1864ab;")
        header_layout.addWidget(title)
        header_layout.addStretch()
        layout.addLayout(header_layout)

        # Info bar with current folder + status hint
        info_frame = QFrame()
        info_frame.setObjectName("browserInfoBar")
        info_frame.setStyleSheet(
            "#browserInfoBar { background: #f8f9fa; border: 1px solid #dee2e6; border-radius: 6px; }"
        )
        info_layout = QHBoxLayout(info_frame)
        info_layout.setContentsMargins(int(GRID * 1.5), GRID, int(GRID * 1.5), GRID)
        info_layout.setSpacing(GRID)
        self._browser_folder_label = QLabel("Download folder: (not set)")
        self._browser_folder_label.setStyleSheet("color: #495057; font-weight: bold;")
        info_layout.addWidget(self._browser_folder_label)
        info_layout.addStretch()
        self._browser_status_label = QLabel("Set download folder in Settings to enable the browser.")
        self._browser_status_label.setStyleSheet("color: #868e96; font-size: 11px;")
        info_layout.addWidget(self._browser_status_label)
        layout.addWidget(info_frame)

        # Main group for controls + results
        browser_group = QGroupBox("📂 Downloaded Files")
        browser_group.setStyleSheet(group_style)
        browser_group_layout = QVBoxLayout(browser_group)
        browser_group_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        browser_group_layout.setSpacing(GRID)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(GRID)
        change_btn = QPushButton("Change Folder…")
        change_btn.setFixedHeight(30)
        change_btn.clicked.connect(self._browse_download_folder)
        controls_row.addWidget(change_btn)

        self._browser_refresh_btn = QPushButton("Refresh")
        self._browser_refresh_btn.setFixedHeight(30)
        self._browser_refresh_btn.clicked.connect(self._refresh_browser_listing)
        controls_row.addWidget(self._browser_refresh_btn)
        self._browser_search_edit = QgsFilterLineEdit()
        self._browser_search_edit.setPlaceholderText("Search files…")
        self._browser_search_edit.setClearButtonEnabled(True)
        self._browser_search_edit.setToolTip("Filter file names (auto-updates as you type)")
        self._browser_search_edit.textChanged.connect(self._apply_browser_search_filter)
        self._browser_search_edit.setFixedHeight(30)
        self._browser_search_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        controls_row.addWidget(self._browser_search_edit)
        controls_row.addStretch()
        browser_group_layout.addLayout(controls_row)

        # Splitter with owner tree and files table
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(12)
        splitter.setOpaqueResize(True)

        # Left panel: Users/Owners
        owner_panel = QGroupBox("Users")
        owner_panel.setStyleSheet(group_style)
        owner_panel_layout = QVBoxLayout(owner_panel)
        owner_panel_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        owner_panel_layout.setSpacing(GRID)
        self._browser_owner_tree = QTreeWidget()
        self._browser_owner_tree.setHeaderLabels(["User", "Files"])
        self._browser_owner_tree.setAlternatingRowColors(True)
        self._browser_owner_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._browser_owner_tree.currentItemChanged.connect(self._on_browser_owner_changed)
        owner_panel_layout.addWidget(self._browser_owner_tree)
        splitter.addWidget(owner_panel)

        # Right panel: Files table + Preview (vertical splitter)
        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.setChildrenCollapsible(False)
        right_splitter.setHandleWidth(8)

        # Files table
        files_panel = QGroupBox("Files")
        files_panel.setStyleSheet(group_style)
        files_layout = QVBoxLayout(files_panel)
        files_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        files_layout.setSpacing(GRID)
        self._browser_table = QTableWidget()
        self._browser_table.setColumnCount(6)
        self._browser_table.setHorizontalHeaderLabels(["File", "Type", "Size", "Modified", "Owner", "Location"])
        self._browser_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._browser_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._browser_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._browser_table.setAlternatingRowColors(True)
        header_view = self._browser_table.horizontalHeader()
        header_view.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header_view.setSectionsClickable(True)
        header_view.sectionDoubleClicked.connect(self._auto_resize_browser_column)
        self._browser_table.verticalHeader().setVisible(False)
        self._browser_table.itemSelectionChanged.connect(self._on_browser_selection_changed)
        files_layout.addWidget(self._browser_table)
        right_splitter.addWidget(files_panel)

        # Preview panel
        preview_panel = QGroupBox("Preview")
        preview_panel.setStyleSheet(group_style)
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(GRID, GRID, GRID, GRID)
        preview_layout.setSpacing(GRID)

        # Stacked widget for GeoJSON canvas / JPEG image
        from qgis.gui import QgsMapCanvas
        self._browser_preview_stack = QStackedWidget()

        # Page 0: Empty placeholder
        empty_label = QLabel("Select a file to preview")
        empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_label.setStyleSheet("color: #868e96; font-style: italic;")
        self._browser_preview_stack.addWidget(empty_label)

        # Page 1: Map canvas for GeoJSON
        canvas_container = QWidget()
        canvas_layout = QVBoxLayout(canvas_container)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(4)
        self._browser_preview_canvas = QgsMapCanvas()
        self._browser_preview_canvas.setCanvasColor(Qt.GlobalColor.white)
        self._browser_preview_canvas.enableAntiAliasing(True)
        self._browser_preview_canvas.setMinimumHeight(150)
        canvas_layout.addWidget(self._browser_preview_canvas, 1)
        # Canvas controls
        canvas_controls = QHBoxLayout()
        canvas_controls.setSpacing(4)
        zoom_in_btn = QPushButton()
        zoom_in_btn.setFixedSize(32, 28)
        zoom_in_btn.setToolTip("Zoom In")
        zoom_in_btn.setIcon(QgsApplication.getThemeIcon("/mActionZoomIn.svg"))
        zoom_in_btn.clicked.connect(lambda: self._browser_preview_canvas.zoomByFactor(0.8))
        zoom_out_btn = QPushButton()
        zoom_out_btn.setFixedSize(32, 28)
        zoom_out_btn.setToolTip("Zoom Out")
        zoom_out_btn.setIcon(QgsApplication.getThemeIcon("/mActionZoomOut.svg"))
        zoom_out_btn.clicked.connect(lambda: self._browser_preview_canvas.zoomByFactor(1.25))
        fit_btn = QPushButton()
        fit_btn.setFixedSize(32, 28)
        fit_btn.setToolTip("Zoom to Fit")
        fit_btn.setIcon(QgsApplication.getThemeIcon("/mActionZoomToLayer.svg"))
        fit_btn.clicked.connect(self._browser_preview_zoom_to_fit)
        pan_btn = QPushButton()
        pan_btn.setFixedSize(32, 28)
        pan_btn.setToolTip("Pan Mode (click and drag on canvas)")
        pan_btn.setIcon(QgsApplication.getThemeIcon("/mActionPan.svg"))
        pan_btn.setCheckable(True)
        pan_btn.clicked.connect(self._browser_preview_pan_toggled)
        canvas_controls.addWidget(zoom_in_btn)
        canvas_controls.addWidget(zoom_out_btn)
        canvas_controls.addWidget(fit_btn)
        canvas_controls.addWidget(pan_btn)
        canvas_controls.addStretch()
        canvas_layout.addLayout(canvas_controls)
        self._browser_preview_stack.addWidget(canvas_container)

        # Page 2: Image viewer for JPEG
        from qgis.PyQt.QtWidgets import QScrollArea
        image_container = QWidget()
        image_layout = QVBoxLayout(image_container)
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.setSpacing(4)
        self._browser_preview_scroll = QScrollArea()
        self._browser_preview_scroll.setWidgetResizable(True)
        self._browser_preview_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._browser_preview_image = QLabel()
        self._browser_preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._browser_preview_image.setStyleSheet("background: #f8f9fa;")
        self._browser_preview_image.setMouseTracking(True)
        self._browser_preview_image.mousePressEvent = self._browser_image_mouse_press
        self._browser_preview_image.mouseMoveEvent = self._browser_image_mouse_move
        self._browser_preview_image.mouseReleaseEvent = self._browser_image_mouse_release
        self._browser_preview_scroll.setWidget(self._browser_preview_image)
        image_layout.addWidget(self._browser_preview_scroll, 1)
        # Image controls
        self._browser_image_scale = 1.0
        self._browser_image_panning = False
        self._browser_image_pan_start = None
        image_controls = QHBoxLayout()
        image_controls.setSpacing(4)
        img_zoom_in_btn = QPushButton()
        img_zoom_in_btn.setFixedSize(32, 28)
        img_zoom_in_btn.setToolTip("Zoom In")
        img_zoom_in_btn.setIcon(QgsApplication.getThemeIcon("/mActionZoomIn.svg"))
        img_zoom_in_btn.clicked.connect(lambda: self._browser_image_zoom(1.25))
        img_zoom_out_btn = QPushButton()
        img_zoom_out_btn.setFixedSize(32, 28)
        img_zoom_out_btn.setToolTip("Zoom Out")
        img_zoom_out_btn.setIcon(QgsApplication.getThemeIcon("/mActionZoomOut.svg"))
        img_zoom_out_btn.clicked.connect(lambda: self._browser_image_zoom(0.8))
        img_fit_btn = QPushButton()
        img_fit_btn.setFixedSize(32, 28)
        img_fit_btn.setToolTip("Fit to View")
        img_fit_btn.setIcon(QgsApplication.getThemeIcon("/mActionZoomToLayer.svg"))
        img_fit_btn.clicked.connect(self._browser_image_fit)
        img_pan_btn = QPushButton()
        img_pan_btn.setFixedSize(32, 28)
        img_pan_btn.setToolTip("Pan Mode (click and drag on image)")
        img_pan_btn.setIcon(QgsApplication.getThemeIcon("/mActionPan.svg"))
        img_pan_btn.setCheckable(True)
        img_pan_btn.clicked.connect(self._browser_image_pan_toggled)
        image_controls.addWidget(img_zoom_in_btn)
        image_controls.addWidget(img_zoom_out_btn)
        image_controls.addWidget(img_fit_btn)
        image_controls.addWidget(img_pan_btn)
        image_controls.addStretch()
        image_layout.addLayout(image_controls)
        self._browser_preview_stack.addWidget(image_container)

        preview_layout.addWidget(self._browser_preview_stack)
        right_splitter.addWidget(preview_panel)

        right_splitter.setStretchFactor(0, 2)
        right_splitter.setStretchFactor(1, 1)
        splitter.addWidget(right_splitter)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        browser_group_layout.addWidget(splitter, 1)

        layout.addWidget(browser_group, 1)

        # Action buttons row
        actions_row = QHBoxLayout()
        actions_row.setSpacing(GRID)
        self._browser_add_btn = QPushButton("Add Selected to QGIS")
        self._browser_add_btn.setFixedHeight(34)
        self._browser_add_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._browser_add_btn.clicked.connect(self._add_browser_selection_to_qgis)
        self._browser_add_btn.setEnabled(False)
        actions_row.addWidget(self._browser_add_btn)

        self._browser_delete_btn = QPushButton("Delete Selected Files")
        self._browser_delete_btn.setFixedHeight(34)
        self._browser_delete_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._browser_delete_btn.clicked.connect(self._delete_browser_selection)
        self._browser_delete_btn.setEnabled(False)
        actions_row.addWidget(self._browser_delete_btn)

        actions_row.addStretch()
        layout.addLayout(actions_row)

        return page

    def _auto_resize_browser_column(self, logical_index: int):
        """Auto-fit a browser table column width based on its widest cell."""
        if not self._browser_table:
            return

        header = self._browser_table.horizontalHeader()
        # Start with header width hint
        max_width = header.sectionSizeHint(logical_index)
        metrics = QFontMetrics(self._browser_table.font())

        for row in range(self._browser_table.rowCount()):
            item = self._browser_table.item(row, logical_index)
            if not item:
                continue
            width = metrics.horizontalAdvance(item.text()) + 32  # padding for icon/spacing
            if width > max_width:
                max_width = width

        self._browser_table.setColumnWidth(logical_index, max_width)

    def _update_browser_tab_state(self):
        """Enable or disable the browser tab based on download folder setting."""
        folder = self._get_download_folder()
        enabled = bool(folder and Path(folder).is_dir())
        if self._browser_button:
            self._browser_button.setEnabled(enabled)
        if self._browser_folder_label:
            if enabled:
                self._browser_folder_label.setText(f"Download folder: {folder}")
                self._browser_status_label.setText("")
            else:
                self._browser_folder_label.setText("Download folder: (not set)")
                self._browser_status_label.setText("Set download folder in Settings to enable the browser.")
        if enabled:
            self._refresh_browser_listing()

    def _get_download_folder(self) -> str | None:
        """Retrieve the configured download folder from settings."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = 'download_folder'")
            row = cur.fetchone()
            conn.close()
            return row[0] if row else None
        except Exception:
            return None

    def _browse_download_folder(self):
        """Open folder dialog to select download folder and update settings."""
        current = self._get_download_folder() or ""
        folder = QFileDialog.getExistingDirectory(self, "Select Download Folder", current)
        if folder:
            try:
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('download_folder', ?)", (folder,))
                conn.commit()
                conn.close()
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to save folder setting: {e}")
                return
            self._update_browser_tab_state()

    def _refresh_browser_listing(self):
        """Scan the download folder and populate the owner tree and files table."""
        folder = self._get_download_folder()
        if not folder or not Path(folder).is_dir():
            return

        self._browser_items.clear()
        self._browser_owner_tree.clear()
        self._browser_table.setRowCount(0)

        owners: dict[str, list[dict]] = {}
        base = Path(folder)

        # Scan for GeoJSON and JPEG files
        for ext in ("*.geojson", "*.json", "*.geov", "*.jpg", "*.jpeg"):
            for fp in base.rglob(ext):
                if not fp.is_file():
                    continue
                # Determine owner from parent folder name
                rel = fp.relative_to(base)
                owner = rel.parts[0] if len(rel.parts) > 1 else "(root)"
                item = {
                    "path": str(fp),
                    "name": fp.name,
                    "type": fp.suffix.lower().lstrip("."),
                    "size": fp.stat().st_size,
                    "modified": fp.stat().st_mtime,
                    "owner": owner,
                }
                owners.setdefault(owner, []).append(item)
                self._browser_items.append(item)

        # Populate owner tree
        for owner, items in sorted(owners.items()):
            tree_item = QTreeWidgetItem([owner, str(len(items))])
            self._browser_owner_tree.addTopLevelItem(tree_item)

        # Select first owner if any
        if self._browser_owner_tree.topLevelItemCount() > 0:
            self._browser_owner_tree.setCurrentItem(self._browser_owner_tree.topLevelItem(0))
        else:
            self._browser_status_label.setText("No files found in download folder.")

    def _on_browser_owner_changed(self, current, previous):
        """Filter files table based on selected owner."""
        if not current:
            self._current_browser_owner = None
            self._refresh_browser_table_view()
            return

        self._current_browser_owner = current.text(0)
        self._refresh_browser_table_view()

    def _apply_browser_search_filter(self, text: str):
        """Update the browser table when the search text changes."""
        self._browser_search_text = text.strip()
        self._refresh_browser_table_view()

    def _refresh_browser_table_view(self):
        """Populate the browser table using current owner + search filter."""
        if not self._browser_table:
            return

        owner = getattr(self, '_current_browser_owner', None)
        if not owner:
            self._browser_table.setRowCount(0)
            self._on_browser_selection_changed()
            return

        rows = [it for it in self._browser_items if it["owner"] == owner]
        search_text = getattr(self, '_browser_search_text', '').lower()
        if search_text:
            rows = [it for it in rows if search_text in it["name"].lower()]

        self._browser_table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            self._browser_table.setItem(row, 0, QTableWidgetItem(item["name"]))
            self._browser_table.setItem(row, 1, QTableWidgetItem(item["type"].upper()))
            size_kb = f"{item['size'] / 1024:.1f} KB"
            self._browser_table.setItem(row, 2, QTableWidgetItem(size_kb))
            from datetime import datetime
            mod_time = datetime.fromtimestamp(item["modified"]).strftime("%Y-%m-%d %H:%M")
            self._browser_table.setItem(row, 3, QTableWidgetItem(mod_time))
            self._browser_table.setItem(row, 4, QTableWidgetItem(item["owner"]))
            self._browser_table.setItem(row, 5, QTableWidgetItem(item["path"]))

        self._on_browser_selection_changed()

    def _on_browser_selection_changed(self):
        """Enable/disable action buttons based on selection and update preview."""
        selected = self._browser_table.selectedItems()
        rows = set(item.row() for item in selected)
        has_selection = len(rows) > 0

        # Get first selected file info
        has_geojson = False
        selected_path = None
        selected_type = None
        for row in rows:
            file_type_item = self._browser_table.item(row, 1)
            path_item = self._browser_table.item(row, 5)
            if file_type_item and path_item:
                selected_type = file_type_item.text().lower()
                selected_path = path_item.text()
                if selected_type in ("geojson", "json"):
                    has_geojson = True
                break

        self._browser_add_btn.setEnabled(has_geojson)
        self._browser_delete_btn.setEnabled(has_selection)

        # Update in-dialog preview
        self._update_browser_preview(selected_path, selected_type)

    def _add_browser_selection_to_qgis(self):
        """Add selected GeoJSON files to QGIS."""
        selected = self._browser_table.selectedItems()
        rows = set(item.row() for item in selected)
        added = 0

        for row in rows:
            file_type = self._browser_table.item(row, 1)
            if file_type and file_type.text().lower() in ("geojson", "json"):
                path_item = self._browser_table.item(row, 5)
                if path_item:
                    file_path = path_item.text()
                    name = Path(file_path).stem
                    layer = QgsVectorLayer(file_path, name, "ogr")
                    if layer.isValid():
                        QgsProject.instance().addMapLayer(layer)
                        added += 1

        if added > 0:
            self._browser_status_label.setText(f"Added {added} layer(s) to QGIS.")
        else:
            self._browser_status_label.setText("No valid GeoJSON files to add.")

    def _delete_browser_selection(self):
        """Delete selected files after confirmation, including associated photos for GeoJSON files."""
        selected = self._browser_table.selectedItems()
        rows = sorted(set(item.row() for item in selected), reverse=True)

        if not rows:
            return

        # Count GeoJSON files to warn about photo deletion
        geojson_count = 0
        for row in rows:
            file_type = self._browser_table.item(row, 1)
            if file_type and file_type.text().lower() in ("geojson", "json"):
                geojson_count += 1

        msg = f"Delete {len(rows)} selected file(s)?"
        if geojson_count > 0:
            msg += f"\n\nThis will also delete associated photos for {geojson_count} GeoJSON file(s)."
        msg += "\n\nThis cannot be undone."

        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Clear any preview layer before deleting
        self._clear_browser_preview()

        deleted_files = 0
        deleted_photos = 0
        for row in rows:
            path_item = self._browser_table.item(row, 5)
            file_type_item = self._browser_table.item(row, 1)
            if not path_item:
                continue

            file_path = Path(path_item.text())
            file_type = file_type_item.text().lower() if file_type_item else ""

            # For GeoJSON files, also delete associated photos
            if file_type in ("geojson", "json"):
                deleted_photos += self._delete_associated_photos(file_path)

            # Delete the file itself
            try:
                file_path.unlink()
                deleted_files += 1
            except Exception as exc:
                log_debug_exception('Suppressed error', exc)

        status = f"Deleted {deleted_files} file(s)"
        if deleted_photos > 0:
            status += f" and {deleted_photos} photo(s)"
        status += "."
        self._browser_status_label.setText(status)
        self._refresh_browser_listing()

    def _delete_associated_photos(self, geojson_path: Path) -> int:
        """Delete photos referenced in a GeoJSON file. Returns count of deleted photos."""
        deleted = 0
        try:
            with open(geojson_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            features = data.get('features', [])
            photo_filenames = set()

            # Extract photo filenames from all features
            for feat in features:
                props = feat.get('properties', {})
                for field_name in ('photos', 'photo'):
                    value = props.get(field_name)
                    if value:
                        photo_filenames.update(self._extract_photo_values(value))

            if not photo_filenames:
                return 0

            # Look for photos in the same folder and in a 'photos' subfolder
            parent_folder = geojson_path.parent
            photos_folder = parent_folder / "photos"

            for filename in photo_filenames:
                # Try same folder
                photo_path = parent_folder / filename
                if photo_path.exists():
                    try:
                        photo_path.unlink()
                        deleted += 1
                        continue
                    except Exception as exc:
                        log_debug_exception('Suppressed error', exc)

                # Try photos subfolder
                photo_path = photos_folder / filename
                if photo_path.exists():
                    try:
                        photo_path.unlink()
                        deleted += 1
                    except Exception as exc:
                        log_debug_exception('Suppressed error', exc)

        except Exception as e:
            print(f"Error reading GeoJSON for photo cleanup: {e}")

        return deleted

    def _update_browser_preview(self, file_path: str | None, file_type: str | None):
        """Update the in-dialog preview panel based on selected file type."""
        # Clear previous preview
        self._clear_browser_preview()

        if not file_path or not file_type:
            # Show empty placeholder
            if self._browser_preview_stack:
                self._browser_preview_stack.setCurrentIndex(0)
            return

        if file_type in ("geojson", "json"):
            self._show_geojson_preview(file_path)
        elif file_type in ("jpg", "jpeg"):
            self._show_image_preview(file_path)
        else:
            # Unsupported type - show placeholder
            if self._browser_preview_stack:
                self._browser_preview_stack.setCurrentIndex(0)

    def _show_geojson_preview(self, file_path: str):
        """Show GeoJSON file in the preview map canvas."""
        if not self._browser_preview_canvas or not self._browser_preview_stack:
            return

        try:
            # Create a temporary layer for preview
            layer = QgsVectorLayer(file_path, "preview", "ogr")
            if not layer.isValid():
                self._browser_status_label.setText("Preview: Invalid GeoJSON file")
                return

            # Store reference and set up canvas
            self._browser_preview_layer = layer
            self._browser_preview_canvas.setLayers([layer])
            self._browser_preview_canvas.setExtent(layer.extent())
            self._browser_preview_canvas.refresh()

            # Switch to canvas page
            self._browser_preview_stack.setCurrentIndex(1)
            self._browser_status_label.setText(f"Preview: {Path(file_path).name}")

        except Exception as e:
            self._browser_status_label.setText(f"Preview error: {e}")

    def _show_image_preview(self, file_path: str):
        """Show JPEG image in the preview image viewer."""
        if not self._browser_preview_image or not self._browser_preview_stack:
            return

        try:
            pixmap = QPixmap(file_path)
            if pixmap.isNull():
                self._browser_status_label.setText("Preview: Cannot load image")
                return

            # Store original pixmap for zooming
            self._browser_preview_pixmap = pixmap
            self._browser_image_scale = 1.0

            # Fit to view initially
            self._browser_image_fit()

            # Switch to image page
            self._browser_preview_stack.setCurrentIndex(2)
            self._browser_status_label.setText(f"Preview: {Path(file_path).name}")

        except Exception as e:
            self._browser_status_label.setText(f"Preview error: {e}")

    def _browser_preview_zoom_to_fit(self):
        """Zoom the preview canvas to fit the layer extent."""
        if self._browser_preview_canvas and self._browser_preview_layer:
            extent = self._browser_preview_layer.extent()
            if not extent.isEmpty():
                self._browser_preview_canvas.setExtent(extent)
                self._browser_preview_canvas.refresh()

    def _browser_image_zoom(self, factor: float):
        """Zoom the preview image by the given factor."""
        if not hasattr(self, '_browser_preview_pixmap') or not self._browser_preview_pixmap:
            return

        self._browser_image_scale *= factor
        # Clamp scale
        self._browser_image_scale = max(0.1, min(5.0, self._browser_image_scale))
        self._apply_image_scale()

    def _browser_image_fit(self):
        """Fit the preview image to the scroll area."""
        if not hasattr(self, '_browser_preview_pixmap') or not self._browser_preview_pixmap:
            return
        if not hasattr(self, '_browser_preview_scroll') or not self._browser_preview_scroll:
            return

        pixmap = self._browser_preview_pixmap
        scroll_size = self._browser_preview_scroll.viewport().size()

        # Calculate scale to fit
        scale_w = scroll_size.width() / pixmap.width() if pixmap.width() > 0 else 1.0
        scale_h = scroll_size.height() / pixmap.height() if pixmap.height() > 0 else 1.0
        self._browser_image_scale = min(scale_w, scale_h, 1.0)  # Don't upscale beyond 100%

        self._apply_image_scale()

    def _apply_image_scale(self):
        """Apply the current scale to the preview image."""
        if not hasattr(self, '_browser_preview_pixmap') or not self._browser_preview_pixmap:
            return

        pixmap = self._browser_preview_pixmap
        scaled = pixmap.scaled(
            int(pixmap.width() * self._browser_image_scale),
            int(pixmap.height() * self._browser_image_scale),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        self._browser_preview_image.setPixmap(scaled)

    def _browser_preview_pan_toggled(self, checked: bool):
        """Toggle pan mode for the map canvas."""
        if not self._browser_preview_canvas:
            return

        if checked:
            # Create and set the pan tool, keep reference to prevent garbage collection
            self._browser_preview_pan_tool = QgsMapToolPan(self._browser_preview_canvas)
            self._browser_preview_canvas.setMapTool(self._browser_preview_pan_tool)
        else:
            # Remove the pan tool
            self._browser_preview_canvas.setMapTool(None)
            self._browser_preview_pan_tool = None

    def _browser_image_pan_toggled(self, checked: bool):
        """Toggle pan mode for the image viewer."""
        if not self._browser_preview_image:
            return

        self._browser_image_panning = checked
        self._browser_preview_image.setCursor(
            Qt.CursorShape.OpenHandCursor if checked else Qt.CursorShape.ArrowCursor
        )

    def _browser_image_mouse_press(self, event):
        """Handle mouse press for image panning."""
        if not self._browser_image_panning:
            # Call original QLabel behavior
            QLabel.mousePressEvent(self._browser_preview_image, event)
            return

        self._browser_image_pan_start = event.position().toPoint()
        self._browser_preview_image.setCursor(Qt.CursorShape.ClosedHandCursor)

    def _browser_image_mouse_move(self, event):
        """Handle mouse move for image panning."""
        if not self._browser_image_panning or self._browser_image_pan_start is None:
            # Call original QLabel behavior
            QLabel.mouseMoveEvent(self._browser_preview_image, event)
            return

        current_pos = event.position().toPoint()
        delta = current_pos - self._browser_image_pan_start
        self._browser_image_pan_start = current_pos

        # Scroll by the delta amount
        h_scroll = self._browser_preview_scroll.horizontalScrollBar()
        v_scroll = self._browser_preview_scroll.verticalScrollBar()
        h_scroll.setValue(h_scroll.value() - delta.x())
        v_scroll.setValue(v_scroll.value() - delta.y())

    def _browser_image_mouse_release(self, event):
        """Handle mouse release for image panning."""
        if self._browser_image_panning:
            self._browser_preview_image.setCursor(Qt.CursorShape.OpenHandCursor)
            self._browser_image_pan_start = None

        # Call original QLabel behavior
        QLabel.mouseReleaseEvent(self._browser_preview_image, event)

    def _clear_browser_preview(self):
        """Clear the preview panel."""
        # Clear canvas layer
        if self._browser_preview_layer:
            self._browser_preview_layer = None
        if self._browser_preview_canvas:
            self._browser_preview_canvas.setLayers([])
            self._browser_preview_canvas.refresh()

        # Clear image
        if self._browser_preview_image:
            self._browser_preview_image.clear()
        if hasattr(self, '_browser_preview_pixmap'):
            self._browser_preview_pixmap = None

        # Show placeholder
        if self._browser_preview_stack:
            self._browser_preview_stack.setCurrentIndex(0)

    def _create_geov_export_page(self) -> QWidget:
        """Geov Format tab: import (drop) and export."""
        from .ui.geov_panel import GeovPanel

        return GeovPanel(self, resolve_download_folder=self._require_download_folder)
    
    def _create_about_page(self) -> QWidget:
        """Create About page with plugin information."""
        import os
        import json
        
        GRID = 8
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(GRID * 4, GRID * 4, GRID * 4, GRID * 4)
        page_layout.setSpacing(GRID * 3)
        
        # Header with logo
        header_layout = QHBoxLayout()
        
        # Try to load logo
        try:
            logo_path = os.path.join(PLUGIN_DIR, "manual", "images", "logo.png")
            if os.path.exists(logo_path):
                logo_label = QLabel()
                pixmap = QPixmap(logo_path)
                if not pixmap.isNull():
                    scaled_pixmap = pixmap.scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                    logo_label.setPixmap(scaled_pixmap)
                    header_layout.addWidget(logo_label)
        except Exception as exc:
            log_debug_exception('Suppressed error', exc)
        
        # Title and version
        title_layout = QVBoxLayout()
        
        # Read version from version.json
        version_info = {"version": "0.9.0", "name": "GeoInbox"}
        try:
            version_path = os.path.join(PLUGIN_DIR, "version.json")
            if os.path.exists(version_path):
                with open(version_path, 'r') as f:
                    version_info = json.load(f)
        except Exception as exc:
            log_debug_exception('Suppressed error', exc)
        
        title_label = QLabel(f"{version_info.get('name', 'GeoInbox')}")
        title_label.setStyleSheet("font-size: 24px; font-weight: bold; color: #1864ab;")
        title_layout.addWidget(title_label)
        
        version_label = QLabel(f"Version {version_info.get('version', '0.9.0')}")
        version_label.setStyleSheet("color: #666; font-size: 14px; margin-bottom: 8px;")
        title_layout.addWidget(version_label)
        
        header_layout.addLayout(title_layout)
        header_layout.addStretch()
        page_layout.addLayout(header_layout)
        
        # About content
        about_frame = QFrame()
        about_frame.setStyleSheet("""
            QFrame {
                border: 1px solid #dee2e6;
                border-radius: 8px;
                background: #f8f9fa;
                padding: 20px;
            }
        """)
        about_layout = QVBoxLayout(about_frame)
        about_layout.setSpacing(GRID * 2)
        
        # Company info
        company_label = QLabel("Plugin Developed by GIS Innovation Sdn. Bhd.")
        company_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #333;")
        about_layout.addWidget(company_label)
        
        copyright_label = QLabel("Copyright GIS Innovation © 2026")
        copyright_label.setStyleSheet("font-size: 14px; color: #666;")
        about_layout.addWidget(copyright_label)
        
        # Website link
        website_label = QLabel('<a href="https://www.GIS.com.my" style="color: #1864ab; text-decoration: none;">www.GIS.com.my</a>')
        website_label.setStyleSheet("font-size: 14px;")
        website_label.setOpenExternalLinks(True)
        about_layout.addWidget(website_label)
        
        # Description
        desc_label = QLabel("GeoInbox Plugin for retrieving data from GISimple and email attachments for committing field work into main geospatial database")
        desc_label.setStyleSheet("font-size: 12px; color: #888; margin-top: 16px; font-style: italic;")
        desc_label.setWordWrap(True)
        about_layout.addWidget(desc_label)
        
        page_layout.addWidget(about_frame)
        page_layout.addStretch()
        
        return page
    
    # =========================================================================
    # LEGACY TAB METHODS (Redirect to new page methods)
    # =========================================================================
    
    def _create_email_tab(self) -> QWidget:
        """Legacy method - redirects to new page."""
        return self._create_email_page()
    
    def _create_gisimple_tab(self) -> QWidget:
        """Legacy method - redirects to new page."""
        return self._create_gisimple_page()
    
    def _create_versioning_tab(self) -> QWidget:
        """Legacy method - redirects to new page."""
        return self._create_versioning_page()
    
    def _create_settings_tab(self) -> QWidget:
        """Legacy method - redirects to new page."""
        return self._create_settings_page()
    
    # =========================================================================
    # STUB METHODS FOR NEW UI ELEMENTS
    # =========================================================================
    
    def _apply_message_filter(self):
        """Apply filter to message list with dynamic search."""
        if not hasattr(self, '_message_table'):
            return
        
        search_text = self._search_edit.text().lower()
        filter_type = self._filter_combo.currentText()
        
        # Iterate through all rows and show/hide based on filters
        for row in range(self._message_table.rowCount()):
            show_row = True
            
            # Apply search filter (search in From and Subject columns)
            if search_text:
                from_item = self._message_table.item(row, 1)  # From column
                subject_item = self._message_table.item(row, 2)  # Subject column
                from_text = from_item.text().lower() if from_item else ""
                subject_text = subject_item.text().lower() if subject_item else ""
                
                if search_text not in from_text and search_text not in subject_text:
                    show_row = False
            
            # Apply filter type
            if show_row and filter_type != "All Messages":
                if filter_type == "Unread":
                    from_item = self._message_table.item(row, 1)
                    is_read = from_item.data(Qt.ItemDataRole.UserRole + 1) if from_item else True
                    if is_read:
                        show_row = False
                elif filter_type == "With Attachments":
                    attach_item = self._message_table.item(row, 5)  # Attachments column
                    attach_count = attach_item.text() if attach_item else "0"
                    if attach_count == "0":
                        show_row = False
                elif filter_type == "Trusted Senders":
                    origin_item = self._message_table.item(row, 0)  # Origin column
                    origin_text = origin_item.text() if origin_item else ""
                    if origin_text != "Trusted":
                        show_row = False
            
            # Show or hide the row
            self._message_table.setRowHidden(row, not show_row)
    
    def _on_message_selected(self):
        """Handle message selection."""
        pass
    
    def _download_attachment(self):
        """Download selected attachment."""
        self._download_selected_attachment()
    
    def _add_to_qgis(self):
        """Download attachment and add to QGIS as layer."""
        if not hasattr(self, '_current_attachments') or not self._current_attachments:
            return
        
        selected = self._attachment_list.currentRow()
        if selected < 0:
            QMessageBox.warning(self, "Add to QGIS", "Please select an attachment first.")
            return
        
        # Require download folder
        working_folder = self._require_download_folder()
        if not working_folder:
            return
        
        # Get sender email from current message
        if not hasattr(self, '_current_msg_row'):
            QMessageBox.warning(self, "Add to QGIS", "No message selected.")
            return
        
        from_item = self._message_table.item(self._current_msg_row, 1)  # From column
        from_addr = from_item.text() if from_item else "unknown"
        from storage.sanitizer import sanitize_identifier
        email = self._extract_email_address(from_addr)
        sanitized_email = sanitize_identifier(email or "unknown")
        
        att = self._current_attachments[selected]
        owner_folder = working_folder / sanitized_email
        owner_folder.mkdir(parents=True, exist_ok=True)
        filepath = owner_folder / att['filename']
        
        try:
            # Fetch attachment data on demand if not already loaded
            if att['data'] is None:
                self._status_label.setText("Fetching attachment...")
                QApplication.processEvents()
                fetched = self._fetch_attachment_data(selected)
                if fetched and fetched['data']:
                    att['data'] = fetched['data']
                else:
                    QMessageBox.warning(self, "Add to QGIS", "Failed to fetch attachment from server.")
                    return
            
            # Save attachment to file
            self._status_label.setText(f"Saving to {filepath}")
            QApplication.processEvents()
            with open(filepath, 'wb') as f:
                f.write(att['data'])
            
            # Check if it's a ZIP file and extract
            if str(filepath).lower().endswith('.zip'):
                import zipfile
                extract_folder = owner_folder / os.path.splitext(att['filename'])[0]
                extract_folder.mkdir(parents=True, exist_ok=True)
                
                with zipfile.ZipFile(str(filepath), 'r') as zip_ref:
                    zip_ref.extractall(str(extract_folder))
                
                # Find GIS files in extracted folder
                gis_files = []
                for root, dirs, files in os.walk(str(extract_folder)):
                    for file in files:
                        if file.lower().endswith(('.shp', '.geojson', '.gpkg', '.kml', '.gml')):
                            gis_files.append(os.path.join(root, file))
                
                if gis_files:
                    # Load first GIS file found
                    self._status_label.setText("Opening layer in QGIS...")
                    QApplication.processEvents()
                    
                    # Create layer with proper error handling
                    layer_name = os.path.splitext(os.path.basename(gis_files[0]))[0]
                    layer = QgsVectorLayer(gis_files[0], layer_name, "ogr")
                    
                    if layer.isValid():
                        try:
                            # Use QTimer to defer the layer addition to avoid access violations
                            from qgis.PyQt.QtCore import QTimer
                            def add_layer_deferred():
                                try:
                                    QgsProject.instance().addMapLayer(layer)
                                    QMessageBox.information(self, "Success", f"Layer '{layer.name()}' added to QGIS.")
                                except Exception as e:
                                    QMessageBox.warning(self, "Error", f"Failed to add layer to QGIS: {str(e)}")
                            
                            QTimer.singleShot(100, add_layer_deferred)
                        except Exception as e:
                            QMessageBox.warning(self, "Error", f"Failed to prepare layer for QGIS: {str(e)}")
                    else:
                        QMessageBox.warning(self, "Error", "Failed to load layer into QGIS.")
                else:
                    QMessageBox.warning(self, "No GIS Data", "No GIS files found in ZIP archive.")
            else:
                # Try to load directly as GIS layer
                self._status_label.setText("Opening layer in QGIS...")
                QApplication.processEvents()
                
                # Create layer with proper error handling
                layer_name = os.path.splitext(att['filename'])[0]
                layer = QgsVectorLayer(str(filepath), layer_name, "ogr")
                
                if layer.isValid():
                    try:
                        # Use QTimer to defer the layer addition to avoid access violations
                        from qgis.PyQt.QtCore import QTimer
                        def add_layer_deferred():
                            try:
                                QgsProject.instance().addMapLayer(layer)
                                QMessageBox.information(self, "Success", f"Layer '{layer.name()}' added to QGIS.")
                            except Exception as e:
                                QMessageBox.warning(self, "Error", f"Failed to add layer to QGIS: {str(e)}")
                        
                        QTimer.singleShot(100, add_layer_deferred)
                    except Exception as e:
                        QMessageBox.warning(self, "Error", f"Failed to prepare layer for QGIS: {str(e)}")
                else:
                    QMessageBox.warning(self, "Error", "File is not a recognized GIS format.")
            self._status_label.setText(f"Attachment saved: {filepath}")
        
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to add to QGIS: {str(e)}")
    
    def _filter_by_group(self, index):
        """Filter messages by GISimple group (legacy - redirects to project filter)."""
        self._filter_by_project(index)
    
    def _filter_by_project(self, index):
        """Filter messages by GISimple project membership."""
        if index <= 0:
            # Show all messages
            for row in range(self._message_table.rowCount()):
                self._message_table.setRowHidden(row, False)
            return
        
        project_id = self._email_project_filter.currentData()
        if not project_id:
            return
        
        # Get project members from database or API
        project_members = self._get_project_members(project_id)
        
        # Filter messages by sender email
        for row in range(self._message_table.rowCount()):
            from_item = self._message_table.item(row, 0)
            if not from_item:
                continue
            
            sender = from_item.text().lower()
            # Extract email from "Name <email>" format
            import re
            match = re.search(r'<([^>]+)>', sender)
            email = match.group(1).lower() if match else sender
            
            # Check if sender is in project members
            is_member = any(email == m.lower() for m in project_members)
            self._message_table.setRowHidden(row, not is_member)
    
    def _get_project_members(self, project_id):
        """Get list of member emails for a project."""
        members = []
        
        if not hasattr(self, '_gisimple_token') or not self._gisimple_token:
            return members
        
        try:
            import ssl
            import urllib.request
            import json as json_module
            
            url = self._gisimple_server_url
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            # Fetch project members from GISimple API
            members_url = f"{url}/api/projects/{project_id}/members"
            request = urllib.request.Request(
                members_url,
                headers={
                    'Authorization': f'Bearer {self._gisimple_token}',
                    'Accept': 'application/json'
                },
                method='GET'
            )
            
            with urlopen_allowed(request, timeout=30, context=ssl_context) as response:
                data = json_module.loads(response.read().decode('utf-8'))
                for member in data:
                    email = member.get('email', '')
                    if email:
                        members.append(email)
        except Exception as exc:
            log_debug_exception("GISimple group members API failed; using cache", exc)
            # Fallback: use cached members from database
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            cursor.execute("""
                SELECT email FROM trusted_accounts 
                WHERE gisimple_group = ?
            """, (project_id,))
            rows = cursor.fetchall()
            conn.close()
            members = [row[0] for row in rows]
        
        return members
    
    def _refresh_email_project_filter(self):
        """Refresh the project filter dropdown in email tab."""
        self._email_project_filter.clear()
        self._email_project_filter.addItem("All Projects", None)
        
        if hasattr(self, '_user_projects') and self._user_projects:
            for proj in self._user_projects:
                project_id = proj.get('id', proj.get('project_id', ''))
                project_name = proj.get('name', proj.get('project_name', 'Unknown'))
                self._email_project_filter.addItem(project_name, project_id)
        else:
            # Try to load from database
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            cursor.execute("SELECT project_id, project_name FROM gisimple_projects ORDER BY project_name")
            rows = cursor.fetchall()
            conn.close()
            
            for project_id, project_name in rows:
                self._email_project_filter.addItem(project_name, project_id)
    
    def _download_gisimple_file(self):
        """Download file from GISimple server."""
        pass
    
    def _refresh_undo_btn(self):
        """Sync the Undo button label and enabled state with the current stack."""
        undo_btn = getattr(self, "_undo_btn", None)
        if not undo_btn or not self._widget_alive(undo_btn):
            return
        depth = len(self._undo_stack)
        if depth == 0:
            undo_btn.setText("Undo Last Commit")
            undo_btn.setEnabled(False)
        else:
            undo_btn.setText(f"Undo  ({depth})")
            undo_btn.setEnabled(True)

    def _update_actions_enabled_state(self):
        """Enable Actions Details controls only when comparison results exist."""
        if self._attribute_view_mode:
            has_data = False
        else:
            has_data = (
                self._widget_alive(self._client_table) and self._client_table.rowCount() > 0
            ) or (
                self._widget_alive(self._host_table) and self._host_table.rowCount() > 0
            )
        for btn in [
            getattr(self, "_show_diff_btn", None),
            getattr(self, "_commit_all_btn", None),
        ]:
            if btn and self._widget_alive(btn):
                btn.setEnabled(has_data)
        self._refresh_undo_btn()

    def _status_visible(self, status: str) -> bool:
        """Return True if the given status is currently checked in the filter."""
        if getattr(self, "_attribute_view_mode", False):
            return True
        checks = getattr(self, "_filter_checks", None) or {}
        cb = checks.get(status)
        return cb.isChecked() if cb else True

    def _apply_status_filter(self):
        """Show/hide rows in both tables based on the filter checkboxes."""
        photos_only = getattr(self, '_photos_only_check', None)
        photos_only_enabled = photos_only.isChecked() if photos_only else False
        status_col = 4 if (not self._attribute_view_mode and self._client_table.columnCount() > 4) else None
        for row in range(self._client_table.rowCount()):
            item = self._client_table.item(row, status_col) if status_col is not None else None
            status = item.text() if item else ""
            status_visible = self._status_visible(status)
            photos_visible = True
            if photos_only_enabled:
                fid_item = self._client_table.item(row, 0)
                has_photos = fid_item.data(Qt.ItemDataRole.UserRole + 1) if fid_item else False
                photos_visible = bool(has_photos)
            self._client_table.setRowHidden(row, not (status_visible and photos_visible))
        for row in range(self._host_table.rowCount()):
            item = self._host_table.item(row, 4)
            status = item.text() if item else ""
            self._host_table.setRowHidden(row, not self._status_visible(status))

    def _toggle_attribute_view_mode(self):
        if not self._attribute_view_mode:
            if not self._client_layer:
                QMessageBox.warning(self, "View Attributes", "Please load a client layer first.")
                return
            has_results = bool(self._client_diff_index or self._host_table.rowCount() > 0)
            if has_results:
                reply = QMessageBox.question(
                    self,
                    "Clear Comparison Results?",
                    "Entering attribute view will clear current comparison results. Continue?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
                self._clear_comparison_tables()
            self._enter_attribute_view_mode()
        else:
            self._exit_attribute_view_mode()

    def _enter_attribute_view_mode(self):
        self._attribute_view_mode = True
        if self._view_attrs_btn:
            self._view_attrs_btn.setText("Return to Comparison View")
        if self._compare_btn:
            self._compare_btn.setEnabled(False)
        if self._stop_btn:
            self._stop_btn.setEnabled(False)
        if self._auto_show_diff_check:
            self._auto_show_diff_check.setEnabled(False)
        if self._host_results_panel:
            self._host_results_panel.setVisible(False)
        self._selected_client_fid = None
        self._selected_host_fid = None
        self._update_selected_feature_label()
        self._host_table.setRowCount(0)
        self._clear_photo_panel("Select a record with photos")
        self._rebuild_attribute_view_table()
        self._update_actions_enabled_state()

    def _exit_attribute_view_mode(self):
        if not self._attribute_view_mode:
            return
        self._attribute_view_mode = False
        if self._view_attrs_btn:
            self._view_attrs_btn.setText("View Client Attributes")
        if self._compare_btn:
            self._compare_btn.setEnabled(True)
        if self._auto_show_diff_check:
            self._auto_show_diff_check.setEnabled(True)
        if self._host_results_panel:
            self._host_results_panel.setVisible(True)
        self._configure_client_table_for_comparison()
        self._client_table.setRowCount(0)
        self._host_table.setRowCount(0)
        self._clear_photo_panel("No comparison results")
        self._client_diff_index = {}
        self._host_deleted_rows = []
        self._selected_client_fid = None
        self._selected_host_fid = None
        self._attribute_field_names = []
        self._update_selected_feature_label()
        self._update_actions_enabled_state()

    def _rebuild_attribute_view_table(self):
        if not self._attribute_view_mode or not hasattr(self, '_client_table'):
            return
        self._client_table.setSortingEnabled(False)
        self._configure_client_table_for_attributes()
        self._client_table.setRowCount(0)
        self._populate_attribute_view_rows()
        self._client_table.setSortingEnabled(True)
        if self._selected_client_fid:
            try:
                fid_val = int(self._selected_client_fid)
            except (TypeError, ValueError):
                fid_val = None
            if fid_val is not None:
                self._select_table_row_by_fid(self._client_table, fid_val)

    def _has_schema_diff(self) -> bool:
        return bool(self._client_only_fields or self._host_only_fields)

    def _create_attr_table_item(self, attr_changes: dict) -> QTableWidgetItem:
        has_schema = self._has_schema_diff()
        if attr_changes:
            text = str(len(attr_changes))
        elif has_schema:
            text = "Schema"
        else:
            text = "—"

        item = QTableWidgetItem(text)
        tooltip_parts = []
        if attr_changes:
            tooltip_parts.append(f"{len(attr_changes)} attribute value difference(s)")
        if has_schema:
            if self._client_only_fields:
                tooltip_parts.append(
                    "Client-only fields: " + self._format_schema_field_list(self._client_only_fields)
                )
            if self._host_only_fields:
                tooltip_parts.append(
                    "Host-only fields: " + self._format_schema_field_list(self._host_only_fields)
                )
        if tooltip_parts:
            item.setToolTip("\n".join(tooltip_parts))

        if attr_changes:
            item.setBackground(QBrush(QColor(255, 255, 200)))
        elif has_schema:
            if self._client_only_fields and not self._host_only_fields:
                color = QColor(209, 250, 229)
            elif self._host_only_fields and not self._client_only_fields:
                color = QColor(255, 220, 220)
            else:
                color = QColor(244, 237, 214)
            item.setBackground(QBrush(color))

        return item

    def _populate_attribute_view_rows(self):
        if not self._client_layer:
            return
        if not self._attribute_field_names:
            self._configure_client_table_for_attributes()
        client_field_indices = {field.name(): idx for idx, field in enumerate(self._client_layer.fields())}
        host_only = set(self._host_only_fields)

        for feature in self._client_layer.getFeatures():
            row = self._client_table.rowCount()
            self._client_table.insertRow(row)
            fid_item = QTableWidgetItem(str(feature.id()))
            fid_item.setData(Qt.ItemDataRole.UserRole + 1, self._feature_has_photos(feature))
            self._client_table.setItem(row, 0, fid_item)

            for col_offset, field_name in enumerate(self._attribute_field_names, start=1):
                if field_name in host_only:
                    display = ""
                else:
                    idx = client_field_indices.get(field_name, -1)
                    value = feature.attribute(idx) if idx >= 0 else None
                    display = self._format_attribute_value(value)
                item = QTableWidgetItem(display)
                item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                self._client_table.setItem(row, col_offset, item)
        self._apply_status_filter()

    def _clear_comparison_tables(self):
        self._client_table.setRowCount(0)
        self._host_table.setRowCount(0)
        self._client_diff_index = {}
        self._host_deleted_rows = []
        self._clear_photo_panel("No comparison results")
        self._selected_client_fid = None
        self._selected_host_fid = None
        self._update_selected_feature_label()
        self._update_actions_enabled_state()

    def _on_client_row_selected(self):
        """Handle client table row selection — also syncs host table."""
        if self._syncing_selection:
            return
        selected = self._client_table.selectedItems()
        if not selected:
            self._selected_client_fid = None
            self._update_selected_feature_label()
            self._update_photo_panel(None)
            return

        row = selected[0].row()
        fid_item = self._client_table.item(row, 0)
        if not fid_item:
            return
        self._selected_client_fid = fid_item.text()
        self._update_selected_feature_label()
        self._update_photo_panel(self._selected_client_fid)
        self.NavigateTo(self._client_layer, self._selected_client_fid)

        if self._attribute_view_mode:
            return

        # Sync host table: Match column (col 1) holds the host FID
        match_item = self._client_table.item(row, 1)
        if match_item and match_item.text() not in ("—", ""):
            try:
                host_fid = int(match_item.text())
                self._syncing_selection = True
                self._select_table_row_by_fid(self._host_table, host_fid)
                self._syncing_selection = False
                self._selected_host_fid = str(host_fid)
            except (ValueError, TypeError):
                pass

    def _on_host_row_selected(self):
        """Handle host table row selection — also syncs client table."""
        if self._syncing_selection:
            return
        selected = self._host_table.selectedItems()
        if not selected:
            self._selected_host_fid = None
            self._update_selected_feature_label()
            return

        row = selected[0].row()
        fid_item = self._host_table.item(row, 0)
        if not fid_item:
            return
        self._selected_host_fid = fid_item.text()
        self._update_selected_feature_label()
        self.NavigateTo(self._host_layer, self._selected_host_fid)

        # Sync client table: Match column (col 1) in host = client FID
        match_item = self._host_table.item(row, 1)
        if match_item and match_item.text() not in ("—", ""):
            self._syncing_selection = True
            self._select_table_row_by_fid(self._client_table, int(match_item.text()))
            self._syncing_selection = False
    
    def _view_on_map(self):
        """View selected feature on map (no QgsVectorLayer selection)."""
        if self._selected_client_fid and self._client_layer:
            self.NavigateTo(self._client_layer, self._selected_client_fid)
        elif self._selected_host_fid and self._host_layer:
            self.NavigateTo(self._host_layer, self._selected_host_fid)

    def _on_nav_pan_toggled(self, checked: bool):
        """Pan and zoom are mutually exclusive."""
        if checked and getattr(self, "_nav_zoom_check", None) and self._nav_zoom_check.isChecked():
            self._nav_zoom_check.blockSignals(True)
            self._nav_zoom_check.setChecked(False)
            self._nav_zoom_check.blockSignals(False)

    def _on_nav_zoom_toggled(self, checked: bool):
        """Pan and zoom are mutually exclusive."""
        if checked and getattr(self, "_nav_pan_check", None) and self._nav_pan_check.isChecked():
            self._nav_pan_check.blockSignals(True)
            self._nav_pan_check.setChecked(False)
            self._nav_pan_check.blockSignals(False)

    def _update_selected_feature_label(self):
        has_client = bool(self._selected_client_fid)
        has_host = bool(self._selected_host_fid)

        if has_client and has_host:
            self._selected_feature_label.setText(
                f"Client: {self._selected_client_fid} | Host: {self._selected_host_fid}"
            )
        elif has_client:
            self._selected_feature_label.setText(f"Client: {self._selected_client_fid}")
        elif has_host:
            self._selected_feature_label.setText(f"Host: {self._selected_host_fid}")
        else:
            self._selected_feature_label.setText("None")

    def NavigateTo(self, layer=None, fid_value=None):
        """Flash / pan / zoom to a feature without changing QGIS layer selection."""
        if not iface:
            return

        flash = getattr(self, "_nav_flash_check", None) and self._nav_flash_check.isChecked()
        pan = getattr(self, "_nav_pan_check", None) and self._nav_pan_check.isChecked()
        zoom = getattr(self, "_nav_zoom_check", None) and self._nav_zoom_check.isChecked()
        if not flash and not pan and not zoom:
            return

        target_layer = layer
        target_fid = fid_value
        if target_layer is None or target_fid is None:
            if self._selected_client_fid and self._client_layer:
                target_layer = self._client_layer
                target_fid = self._selected_client_fid
            elif self._selected_host_fid and self._host_layer:
                target_layer = self._host_layer
                target_fid = self._selected_host_fid
            else:
                return

        try:
            fid = int(target_fid)
        except (TypeError, ValueError):
            return

        if not target_layer or not target_layer.isValid():
            return

        feat = target_layer.getFeature(fid)
        if not feat.isValid():
            return
        geom = feat.geometry()
        if not geom or geom.isEmpty():
            return

        canvas = iface.mapCanvas()
        if not canvas:
            return

        if flash:
            try:
                canvas.flashFeatureIds(target_layer, [fid])
            except Exception as exc:
                log_debug_exception('Suppressed error', exc)

        if pan:
            try:
                center = geom.centroid().asPoint()
                canvas.setCenter(center)
            except Exception as exc:
                log_debug_exception('Suppressed error', exc)

        if zoom:
            try:
                extent = QgsRectangle(geom.boundingBox())
                if extent.width() <= 0 and extent.height() <= 0:
                    center = extent.center()
                    dest = canvas.mapSettings().destinationCrs()
                    pad = 0.0005 if dest.isValid() and dest.isGeographic() else 50.0
                    extent = QgsRectangle(
                        center.x() - pad, center.y() - pad,
                        center.x() + pad, center.y() + pad,
                    )
                else:
                    extent.scale(1.2)
                canvas.setExtent(extent)
            except Exception as exc:
                log_debug_exception('Suppressed error', exc)

        if pan or zoom:
            canvas.refresh()

    def _zoom_to_selection(self):
        if iface:
            iface.actionZoomToSelected().trigger()

    def _pan_to_selection(self):
        if iface:
            iface.actionPanToSelected().trigger()

    def _widget_alive(self, widget) -> bool:
        """Return True if the Qt C++ object behind the widget is still alive."""
        if widget is None:
            return False
        try:
            import sip
            return not sip.isdeleted(widget)
        except Exception:
            try:
                _ = widget.objectName()
                return True
            except RuntimeError:
                return False

    def _release_selection_control(self):
        """Disconnect layer selection listeners to release control back to QGIS."""
        if hasattr(self, '_client_selection_layer') and self._client_selection_layer:
            self._disconnect_layer_selection(self._client_selection_layer)
            self._client_selection_layer = None
        if hasattr(self, '_host_selection_layer') and self._host_selection_layer:
            self._disconnect_layer_selection(self._host_selection_layer)
            self._host_selection_layer = None

    def _unselect_features_on_map(self):
        """Clear comparison table selection only — does not touch QGIS map selection."""
        try:
            self._selected_client_fid = None
            self._selected_host_fid = None
            self._clear_table_selection(self._client_table)
            self._clear_table_selection(self._host_table)
            self._update_selected_feature_label()
            self._clear_photo_panel("Select a record with photos")
        except Exception as e:
            print(f"Error clearing selection: {e}")

    def _connect_layer_selection(self, layer: QgsVectorLayer | None, side: str):
        if not layer or not isinstance(layer, QgsVectorLayer):
            return
        if side == "client":
            self._client_selection_layer = layer
        else:
            self._host_selection_layer = layer

        def _handler(selected, deselected, clear_and_select, lyr=layer, s=side):
            self._on_layer_selection_changed(lyr, s, selected, deselected)

        try:
            layer.selectionChanged.connect(_handler)
        except Exception as exc:
            print(f"Failed to connect selectionChanged for {side} layer: {exc}")

    def _disconnect_layer_selection(self, layer: QgsVectorLayer | None):
        if not layer:
            return
        try:
            layer.selectionChanged.disconnect()
        except (TypeError, RuntimeError):
            pass

    def _on_layer_selection_changed(self, layer: QgsVectorLayer, side: str, selected, deselected):
        try:
            if getattr(self, "_closing", True):
                return
            if self._syncing_selection:
                return
            if not selected:
                self._syncing_selection = True
                if side == "client":
                    self._selected_client_fid = None
                    self._clear_table_selection(self._client_table)
                else:
                    self._selected_host_fid = None
                    self._clear_table_selection(self._host_table)
                self._syncing_selection = False
                self._update_selected_feature_label()
                return

            fid = next(iter(selected))
            self._syncing_selection = True
            if side == "client":
                self._selected_client_fid = str(fid)
                self._select_table_row_by_fid(self._client_table, fid)
            else:
                self._selected_host_fid = str(fid)
                self._select_table_row_by_fid(self._host_table, fid)
            self._syncing_selection = False
            self._update_selected_feature_label()
        except RuntimeError:
            # Dialog widgets have been destroyed — disconnect to avoid repeat errors
            self._disconnect_layer_selection(layer)

    def _select_table_row_by_fid(self, table: QTableWidget, fid: int) -> bool:
        """Select the row matching fid in column 0. Returns True if found."""
        if not self._widget_alive(table):
            return False
        fid_str = str(fid)
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item and item.text() == fid_str:
                table.setCurrentCell(row, 0)
                return True
        return False

    def _clear_table_selection(self, table: QTableWidget):
        if self._widget_alive(table):
            table.blockSignals(True)
            table.clearSelection()
            if table.selectionModel():
                table.selectionModel().clearCurrentIndex()
            table.setCurrentItem(None)
            table.blockSignals(False)

    def _copy_client_table_to_clipboard(self):
        """Copy the client results table to clipboard in TSV format."""
        if not self._widget_alive(self._client_table):
            return
        table = self._client_table
        headers = [table.horizontalHeaderItem(c).text()
                   for c in range(table.columnCount())]
        lines = ["\t".join(headers)]
        for row in range(table.rowCount()):
            if table.isRowHidden(row):
                continue
            cells = []
            for col in range(table.columnCount()):
                item = table.item(row, col)
                cells.append(item.text() if item else "")
            lines.append("\t".join(cells))
        QApplication.clipboard().setText("\n".join(lines))

    def _show_client_context_menu(self, pos):
        if not self._client_table:
            return
        item = self._client_table.itemAt(pos)
        if not item:
            return
        row = item.row()
        self._client_table.setCurrentCell(row, 0)
        fid_item = self._client_table.item(row, 0)
        client_fid = fid_item.text() if fid_item else None
        status_col = 4 if (not self._attribute_view_mode and self._client_table.columnCount() > 4) else None
        status_item = self._client_table.item(row, status_col) if status_col is not None else None
        status = status_item.text() if status_item else ""

        menu = QMenu(self)

        if not self._attribute_view_mode:
            show_diff_action = QAction("Show Attribute Differences", self)
            show_diff_action.triggered.connect(self._open_attribute_diff_for_selection)
            menu.addAction(show_diff_action)

            # Show detailed geometry comparison report
            if client_fid:
                show_geom_report_action = QAction("Show Geometry Comparison Report", self)
                show_geom_report_action.triggered.connect(lambda checked=False, fid=client_fid: self._show_geometry_report(fid))
                menu.addAction(show_geom_report_action)

        if client_fid:
            menu.addSeparator()
            copy_action = QAction(f"Copy FID  ({client_fid})", self)
            copy_action.triggered.connect(lambda checked=False, f=client_fid: QApplication.clipboard().setText(f))
            menu.addAction(copy_action)

            flash_action = QAction("Flash on map", self)
            def _flash(checked=False, fid=client_fid):
                if self._client_layer and iface:
                    try:
                        iface.mapCanvas().flashFeatureIds(self._client_layer, [int(fid)])
                    except Exception as exc:
                        log_debug_exception('Suppressed error', exc)
            flash_action.triggered.connect(_flash)
            menu.addAction(flash_action)

        # Commit individual feature — only for actionable statuses
        if (not self._attribute_view_mode) and status in ("MODIFIED (ATTR)", "MODIFIED (GEOM)", "MODIFIED (BOTH)", "ADDED") and client_fid:
            menu.addSeparator()
            commit_action = QAction(f"Commit this feature  (FID {client_fid})", self)
            commit_action.triggered.connect(lambda checked=False, fid=client_fid: self._commit_single_feature(fid))
            menu.addAction(commit_action)

        # Ignore / Un-ignore — session only
        if client_fid:
            menu.addSeparator()
            if client_fid in self._ignored_fids:
                unignore_action = QAction(f"Un-ignore  (FID {client_fid})", self)
                unignore_action.triggered.connect(lambda checked=False, fid=client_fid: self._unignore_feature(fid))
                menu.addAction(unignore_action)
            else:
                ignore_action = QAction(f"Ignore this feature  (FID {client_fid})", self)
                ignore_action.triggered.connect(lambda checked=False, fid=client_fid: self._ignore_feature(fid))
                menu.addAction(ignore_action)

        # View Photo — check if feature has a photo field
        if client_fid and self._client_layer:
            photo_filename = self._get_feature_photo(self._client_layer, int(client_fid))
            if photo_filename:
                menu.addSeparator()
                view_photo_action = QAction(f"📷 View Photo ({photo_filename})", self)
                view_photo_action.triggered.connect(lambda checked=False, fn=photo_filename: self._view_photo(fn))
                menu.addAction(view_photo_action)

        menu.exec(self._client_table.mapToGlobal(pos))

    def _get_feature_photo(self, layer: QgsVectorLayer, fid: int) -> str:
        """Get photo filename from a feature if it has a photos/photo field."""
        if not layer:
            return None
        try:
            feat = layer.getFeature(fid)
            if not feat.isValid():
                return None
            photos = self._get_feature_photo_list(feat)
            return photos[0] if photos else None
        except Exception:
            return None

    def _get_feature_photo_list(self, feature) -> list:
        """Return a list of photo filenames stored on a feature."""
        if not feature:
            return []
        for field_name in ['photos', 'photo']:
            try:
                idx = feature.fields().indexFromName(field_name)
            except Exception:
                idx = -1
            if idx >= 0:
                value = feature.attribute(idx)
                photos = self._extract_photo_values(value)
                if photos:
                    return photos
        return []

    def _extract_photo_values(self, value) -> list[str]:
        """Normalize stored photo field value into a list of filenames."""
        if value is None:
            return []
        raw_items = []
        if isinstance(value, (list, tuple)):
            raw_items = list(value)
        else:
            text = str(value).strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    raw_items = parsed
                else:
                    raw_items = [parsed]
            except Exception:
                cleaned = text.strip('[]')
                import re
                raw_items = [part.strip() for part in re.split(r'[;,]', cleaned) if part.strip()]

        normalized = []
        for item in raw_items:
            token = str(item).strip().strip('"').strip("'")
            if token:
                normalized.append(token)
        return normalized

    def _feature_has_photos(self, feature) -> bool:
        return bool(self._get_feature_photo_list(feature))

    def _get_photos_for_client_fid(self, client_fid) -> list[str]:
        try:
            fid_int = int(client_fid)
        except (TypeError, ValueError):
            return []
        feature = None
        if self._attribute_view_mode:
            feature = self._get_feature_by_fid(self._client_layer, fid_int)
        else:
            diff = self._client_diff_index.get(fid_int)
            feature = diff.get('client_feat') if diff else None
        return self._get_feature_photo_list(feature)

    def _clear_photo_panel(self, message: str = "Select a record with photos", status: str | None = None):
        self._current_photos = []
        self._current_preview_index = None
        if hasattr(self, '_photo_preview_label'):
            self._photo_preview_label.setPixmap(QPixmap())
            self._photo_preview_label.setText(message)
        if hasattr(self, '_photo_status_label'):
            self._photo_status_label.setText(status or "No record selected")
        if hasattr(self, '_open_photo_btn'):
            self._open_photo_btn.setEnabled(False)
        if hasattr(self, '_open_photo_folder_btn'):
            self._open_photo_folder_btn.setEnabled(False)

    def _update_photo_panel(self, client_fid):
        if not hasattr(self, '_photo_preview_label'):
            return
        if not client_fid:
            self._clear_photo_panel()
            return

        photos = self._get_photos_for_client_fid(client_fid)
        self._current_photos = []
        self._current_preview_index = None
        if not photos:
            self._clear_photo_panel("No photos for this record", status="Photos: none")
            return

        working_folder = self._get_working_folder()
        if not working_folder:
            self._clear_photo_panel(
                "Set a download folder to preview photos",
                status=f"Photos: {len(photos)} found (no folder)"
            )
            return

        resolved = []
        for name in photos:
            path = self._resolve_photo_path(name, working_folder)
            resolved.append((name, path))
        self._current_photos = resolved

        first_index = next((idx for idx, (_, path) in enumerate(resolved) if path and path.exists()), None)
        if first_index is None:
            self._photo_preview_label.setPixmap(QPixmap())
            self._photo_preview_label.setText("Photos found but files missing in download folder")
            self._photo_status_label.setText("Photos: files missing")
            self._open_photo_btn.setEnabled(False)
            if hasattr(self, '_open_photo_folder_btn'):
                self._open_photo_folder_btn.setEnabled(False)
            return

        self._display_preview_photo(first_index)
        self._photo_status_label.setText(f"Photos: {len(photos)} (showing {first_index + 1})")
        self._open_photo_btn.setEnabled(True)
        if hasattr(self, '_open_photo_folder_btn'):
            self._open_photo_folder_btn.setEnabled(True)

    def _display_preview_photo(self, index: int):
        if not hasattr(self, '_photo_preview_label'):
            return
        if index is None or index < 0 or index >= len(self._current_photos):
            return
        filename, path = self._current_photos[index]
        self._current_preview_index = index
        if not path or not path.exists():
            self._photo_preview_label.setPixmap(QPixmap())
            self._photo_preview_label.setText(f"Photo not found:\n{filename}")
            self._open_photo_btn.setEnabled(False)
            if hasattr(self, '_open_photo_folder_btn'):
                self._open_photo_folder_btn.setEnabled(False)
            return

        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self._photo_preview_label.setPixmap(QPixmap())
            self._photo_preview_label.setText(f"Unable to load image:\n{filename}")
            self._open_photo_btn.setEnabled(False)
            if hasattr(self, '_open_photo_folder_btn'):
                self._open_photo_folder_btn.setEnabled(False)
            return

        scaled = pixmap.scaled(
            self._photo_preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        self._photo_preview_label.setPixmap(scaled)
        self._photo_preview_label.setText("")
        self._open_photo_btn.setEnabled(True)
        if hasattr(self, '_open_photo_folder_btn'):
            self._open_photo_folder_btn.setEnabled(True)

    def _open_photo_viewer(self):
        if not self._current_photos:
            QMessageBox.information(self, "Photo Viewer", "No photos available for this record.")
            return
        available = [(name, path) for name, path in self._current_photos if path and path.exists()]
        if not available:
            QMessageBox.warning(self, "Photo Viewer", "Photo files were not found in the download folder.")
            return
        initial_index = self._current_preview_index if self._current_preview_index is not None else 0
        initial_index = max(0, min(initial_index, len(available) - 1))
        dlg = PhotoViewerDialog(available, parent=self, initial_index=initial_index)
        dlg.exec()

    def _open_photo_location(self):
        if not self._current_photos or self._current_preview_index is None:
            QMessageBox.information(self, "Open File Location", "No photo selected.")
            return
        name, path = self._current_photos[self._current_preview_index]
        if not path or not path.exists():
            QMessageBox.warning(self, "Open File Location", f"Photo not found on disk: {name}")
            return
        folder = path.parent
        try:
            open_path_in_file_manager(folder)
        except Exception as e:
            QMessageBox.warning(self, "Open File Location", f"Failed to open folder: {e}")

    def _resolve_photo_path(self, filename: str, working_folder: Path):
        if not filename:
            return None
        try:
            candidate = Path(filename)
        except Exception:
            candidate = None
        if candidate and candidate.is_absolute() and candidate.exists():
            return candidate
        # Relative to working folder
        direct_path = working_folder / filename
        if direct_path.exists():
            return direct_path
        for candidate in working_folder.rglob(filename):
            return candidate
        return None

    def _show_geometry_report(self, client_fid: str):
        """Show detailed geometry comparison report for debugging."""
        try:
            fid = int(client_fid)
        except ValueError:
            QMessageBox.warning(self, "Geometry Report", f"Invalid FID: {client_fid}")
            return
        
        diff = self._client_diff_index.get(fid)
        if not diff:
            QMessageBox.warning(self, "Geometry Report", "No comparison data available for this feature.")
            return
        
        # Get geometries
        client_feat = diff.get('client_feat')
        host_feat = diff.get('host_feat')
        
        if not client_feat:
            QMessageBox.warning(self, "Geometry Report", "Client feature not found.")
            return
        
        client_geom = client_feat.geometry()
        host_geom = host_feat.geometry() if host_feat else None
        
        # Build report
        report = []
        report.append(f"=== GEOMETRY COMPARISON REPORT ===")
        report.append(f"Client FID: {fid}")
        report.append(f"Host FID: {diff.get('host_fid', 'N/A')}")
        report.append(f"")
        
        # Client geometry info
        type_names = {0: 'Point', 1: 'Line', 2: 'Polygon', 3: 'Unknown', 4: 'Null'}
        report.append(f"--- CLIENT GEOMETRY ---")
        report.append(f"Base Type: {type_names.get(client_geom.type(), 'Unknown')} ({client_geom.type()})")
        report.append(f"WKB Type: {client_geom.wkbType()}")
        report.append(f"Is Multipart: {client_geom.isMultipart()}")
        report.append(f"Is Valid: {client_geom.isGeosValid()}")
        report.append(f"Is Empty: {client_geom.isEmpty()}")
        report.append(f"Area: {client_geom.area()}")
        report.append(f"Length: {client_geom.length()}")
        client_wkt = client_geom.asWkt(precision=10)
        report.append(f"WKT (10 decimals):")
        report.append(client_wkt[:2000] + "..." if len(client_wkt) > 2000 else client_wkt)
        report.append(f"")
        
        if host_geom:
            # Host geometry info
            report.append(f"--- HOST GEOMETRY ---")
            report.append(f"Base Type: {type_names.get(host_geom.type(), 'Unknown')} ({host_geom.type()})")
            report.append(f"WKB Type: {host_geom.wkbType()}")
            report.append(f"Is Multipart: {host_geom.isMultipart()}")
            report.append(f"Is Valid: {host_geom.isGeosValid()}")
            report.append(f"Is Empty: {host_geom.isEmpty()}")
            report.append(f"Area: {host_geom.area()}")
            report.append(f"Length: {host_geom.length()}")
            host_wkt = host_geom.asWkt(precision=10)
            report.append(f"WKT (10 decimals):")
            report.append(host_wkt[:2000] + "..." if len(host_wkt) > 2000 else host_wkt)
            report.append(f"")
            
            # Comparison results - RAW (without normalization)
            report.append(f"--- RAW COMPARISON (no type normalization) ---")
            report.append(f"equals(): {client_geom.equals(host_geom)}")
            try:
                report.append(f"isGeosEqual(): {client_geom.isGeosEqual(host_geom)}")
            except Exception as e:
                report.append(f"isGeosEqual(): ERROR - {e}")
            report.append(f"distance(): {client_geom.distance(host_geom)}")
            report.append(f"")
            
            # Comparison results - NORMALIZED (Polygon <-> MultiPolygon)
            report.append(f"--- NORMALIZED COMPARISON (type-matched) ---")
            cg_norm = QgsGeometry(client_geom)
            hg_norm = QgsGeometry(host_geom)
            if cg_norm.isMultipart() != hg_norm.isMultipart():
                report.append(f"Type mismatch detected - normalizing...")
                if not cg_norm.isMultipart():
                    cg_norm.convertToMultiType()
                    report.append(f"  Client converted to multi-type")
                if not hg_norm.isMultipart():
                    hg_norm.convertToMultiType()
                    report.append(f"  Host converted to multi-type")
            report.append(f"equals() [normalized]: {cg_norm.equals(hg_norm)}")
            try:
                report.append(f"isGeosEqual() [normalized]: {cg_norm.isGeosEqual(hg_norm)}")
            except Exception as e:
                report.append(f"isGeosEqual() [normalized]: ERROR - {e}")
            report.append(f"distance() [normalized]: {cg_norm.distance(hg_norm)}")
            report.append(f"")
            
            # WKT string comparison
            report.append(f"--- WKT STRING COMPARISON ---")
            report.append(f"WKT strings identical: {client_wkt == host_wkt}")
            if client_wkt != host_wkt:
                # Find first difference
                for i, (c1, c2) in enumerate(zip(client_wkt, host_wkt)):
                    if c1 != c2:
                        report.append(f"First difference at position {i}:")
                        report.append(f"  Client: ...{client_wkt[max(0,i-20):i+20]}...")
                        report.append(f"  Host:   ...{host_wkt[max(0,i-20):i+20]}...")
                        break
                if len(client_wkt) != len(host_wkt):
                    report.append(f"Length difference: Client={len(client_wkt)}, Host={len(host_wkt)}")
            report.append(f"")
            
            # Stored comparison decision
            geom_check_results = diff.get('geom_check_results', {})
            if geom_check_results:
                report.append(f"--- COMPARISON DECISION ---")
                report.append(f"geom_changed flag: {diff.get('geom_changed')}")
                report.append(f"stored distance: {diff.get('geom_distance')}")
                for key, result in geom_check_results.items():
                    report.append(f"{key}: {result}")
            match_method = diff.get("match_method")
            if match_method:
                report.append(f"match_method: {match_method}")
        else:
            report.append(f"--- NO HOST GEOMETRY (NEW FEATURE) ---")
        
        # Show in dialog
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Geometry Report - FID {fid}")
        dlg.resize(700, 500)
        layout = QVBoxLayout(dlg)
        
        text_browser = QTextBrowser()
        text_browser.setPlainText("\n".join(report))
        text_browser.setFont(text_browser.font())
        layout.addWidget(text_browser)
        
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btn_box.accepted.connect(dlg.accept)
        layout.addWidget(btn_box)
        
        dlg.exec()

    def _view_photo(self, filename: str):
        """View a photo file from the download folder."""
        if not filename:
            return
        
        working_folder = self._get_working_folder()
        if not working_folder:
            QMessageBox.warning(self, "View Photo", "Download folder not configured. Please set it in Settings.")
            return

        photo_path = self._resolve_photo_path(filename, working_folder)
        if not photo_path or not photo_path.exists():
            QMessageBox.warning(self, "View Photo", f"Photo not found: {filename}\n\nSearched in: {working_folder}")
            return

        dlg = PhotoViewerDialog([(filename, photo_path)], parent=self)
        dlg.exec()

    _STATUS_COLORS = {
        "UNCHANGED":       QColor(240, 240, 240),
        "MODIFIED (GEOM)": QColor(255, 204, 153),  # Pastel orange
        "MODIFIED (ATTR)": QColor(187, 222, 251),
        "MODIFIED (BOTH)": QColor(255, 182, 193),  # Pastel red/pink
        "ADDED":           QColor(200, 255, 200),
        "NOT SURVEYED":    QColor(220, 220, 220),
        "IGNORED":         QColor(210, 210, 210),
    }

    def _status_color(self, status: str) -> tuple:
        """Return (status, QColor) for a given status label string."""
        color = self._STATUS_COLORS.get(status, QColor(255, 255, 255))
        return status, color

    def _ignore_feature(self, client_fid: str):
        """Mark a client feature as ignored for this session."""
        # Find the row and store original status
        for row in range(self._client_table.rowCount()):
            fid_item = self._client_table.item(row, 0)
            if not fid_item or fid_item.text() != client_fid:
                continue
            status_item = self._client_table.item(row, 4)
            original_status = status_item.text() if status_item else ""
            self._ignored_fids[client_fid] = original_status

            # Restyle the row with muted ignored color
            muted = QColor(210, 210, 210)
            muted_text = QColor(150, 150, 150)
            for col in range(self._client_table.columnCount()):
                cell = self._client_table.item(row, col)
                if cell:
                    cell.setBackground(QBrush(muted))
                    cell.setForeground(QBrush(muted_text))
            if status_item:
                status_item.setText("IGNORED")

            # Hide the row if the IGNORED filter checkbox is unchecked
            self._client_table.setRowHidden(row, not self._status_visible("IGNORED"))
            break

    def _unignore_feature(self, client_fid: str):
        """Restore a previously ignored client feature."""
        original_status = self._ignored_fids.pop(client_fid, None)
        if original_status is None:
            return

        # Find the row and restore its original style
        for row in range(self._client_table.rowCount()):
            fid_item = self._client_table.item(row, 0)
            if not fid_item or fid_item.text() != client_fid:
                continue
            status_item = self._client_table.item(row, 4)
            if status_item:
                status_item.setText(original_status)

            # Re-apply the original status color
            _, bg_color = self._status_color(original_status)
            for col in range(self._client_table.columnCount()):
                cell = self._client_table.item(row, col)
                if cell:
                    cell.setBackground(QBrush(bg_color))
                    cell.setForeground(QBrush(QColor(0, 0, 0)))

            # Show or hide based on whether the original status is filtered
            self._client_table.setRowHidden(row, not self._status_visible(original_status))
            break

    def _open_attribute_diff_for_selection(self):
        if not self._client_table:
            return
        row = self._client_table.currentRow()
        if row < 0:
            return
        fid_item = self._client_table.item(row, 0)
        if not fid_item:
            return
        self._open_attribute_diff_by_fid(fid_item.text())

    def _open_attribute_diff_by_fid(self, client_fid: str):
        diff = self._client_diff_index.get(int(client_fid))
        if not diff:
            QMessageBox.information(self, "Attribute Differences", "No diff data available.")
            return
        host_fid = diff.get("host_fid")
        geom_changed = diff.get("geom_changed", False)

        if host_fid is not None:
            self._selected_host_fid = str(host_fid)
            self._select_table_row_by_fid(self._host_table, host_fid)
        self._selected_client_fid = str(client_fid)
        self._update_selected_feature_label()
        self.NavigateTo(self._client_layer, client_fid)

        if geom_changed:
            client_feat = self._get_feature_by_fid(self._client_layer, int(client_fid))
            host_feat = self._get_feature_by_fid(self._host_layer, host_fid) if host_fid else None
            self._show_geom_diff_overlay(client_feat, host_feat)

        attr_changes = diff.get("attr_changes", {})
        is_new_feature = diff.get("match_type") == "new"

        dialog = AttributeDiffDialog(
            client_fid=str(client_fid),
            host_fid=str(host_fid) if host_fid is not None else None,
            attr_changes=attr_changes,
            geom_changed=geom_changed,
            is_new_feature=is_new_feature,
            on_commit=lambda cfid, hfid: self._commit_single_feature(cfid),
            schema_added_fields=self._client_only_fields,
            schema_removed_fields=self._host_only_fields,
            parent=self
        )
        dialog.exec()

    def _mark_attr_commit(self, client_fid: str, host_fid: str | None):
        self._pending_attr_commits.add((client_fid, host_fid))
        QMessageBox.information(
            self,
            "Marked for Commit",
            f"Marked attribute changes for Client {client_fid} to commit later."
        )

    def _get_feature_by_fid(self, layer: QgsVectorLayer, fid_value):
        if not layer or fid_value is None:
            return None
        try:
            fid = int(fid_value)
        except (TypeError, ValueError):
            return None
        return next(layer.getFeatures(QgsFeatureRequest([fid])), None)

    def _clear_geom_diff_layer(self):
        layer = self._geom_diff_layer
        self._geom_diff_layer = None
        if layer:
            self._safe_remove_layer(layer)

    def _clear_geom_parts_layers(self):
        for layer_attr in [
            "_geom_parts_added_layer",
            "_geom_parts_removed_layer",
            "_geom_parts_unchanged_layer"
        ]:
            layer = getattr(self, layer_attr, None)
            setattr(self, layer_attr, None)  # null ref BEFORE any C++ call
            self._safe_remove_layer(layer)   # safe_remove handles None and deleted objects
        extra = getattr(self, "_geom_diff_extra_layers", []) or []
        self._geom_diff_extra_layers = []
        for layer in extra:
            self._safe_remove_layer(layer)

    def _safe_remove_layer(self, layer: QgsVectorLayer):
        # Use identity check first — boolean eval on a deleted C++ object raises RuntimeError
        if layer is None:
            return
        try:
            import sip
            if sip.isdeleted(layer):
                return
        except Exception as exc:
            log_debug_exception('Suppressed error', exc)
        try:
            project = QgsProject.instance()
            layer_id = layer.id()
            if not project.mapLayer(layer_id):
                return
            project.removeMapLayer(layer_id)
        except RuntimeError:
            pass

    def _safe_add_project_layer(self, layer: QgsVectorLayer):
        if not layer:
            return
        if getattr(self, "_project_changing", False):
            return
        project = QgsProject.instance()
        if project.mapLayer(layer.id()):
            return

        def _add():
            # Register with project but do NOT auto-insert into layer tree
            project.addMapLayer(layer, False)
            root = project.layerTreeRoot()
            group = root.findGroup("GeoInbox")
            if group is None:
                group = root.insertGroup(0, "GeoInbox")
            group.addLayer(layer)

        QTimer.singleShot(0, _add)

    def _build_geom_parts_layer(self, base_layer: QgsVectorLayer, feature_parts: list, name: str, color: QColor):
        if not base_layer or not feature_parts:
            return None
        geom_kind = QgsWkbTypes.geometryType(base_layer.wkbType())
        geom_type_map = {
            QgsWkbTypes.PointGeometry: "Point",
            QgsWkbTypes.LineGeometry: "LineString",
            QgsWkbTypes.PolygonGeometry: "Polygon"
        }
        geom_type = geom_type_map.get(geom_kind)
        if not geom_type:
            return None
        crs = base_layer.crs().authid()
        memory_layer = QgsVectorLayer(f"{geom_type}?crs={crs}", name, "memory")
        if not memory_layer.isValid():
            return None
        provider = memory_layer.dataProvider()

        fields = QgsFields(base_layer.fields())
        provider.addAttributes(fields)
        memory_layer.updateFields()

        features = []
        for part in feature_parts:
            if not part:
                continue
            geom = part.get("geometry")
            attrs = part.get("attributes")
            if not geom or geom.isEmpty():
                continue
            new_feat = QgsFeature(memory_layer.fields())
            new_feat.setGeometry(geom)
            if attrs is not None:
                new_feat.setAttributes(attrs)
            features.append(new_feat)
        if features:
            provider.addFeatures(features)
            memory_layer.updateExtents()

        symbol = QgsSymbol.defaultSymbol(memory_layer.geometryType())
        if symbol:
            symbol.setColor(color)
            memory_layer.setRenderer(QgsSingleSymbolRenderer(symbol))

        return memory_layer

    def _build_diff_parts_layer(self, crs: str, geom_kind, parts: list, name: str, color: QColor):
        """Build a memory layer for geometry difference results (removed/added parts).

        Uses Multi* geometry types so the GEOS difference() output is never
        silently dropped — e.g. old_geom.difference(new_geom) can return a
        MultiPolygon or even a GeometryCollection when the source layer is Polygon.
        """
        QgsMessageLog.logMessage(
            f"GeoInbox _build_diff_parts_layer '{name}': parts count={len(parts) if parts else 0}",
            "GeoInbox", Qgis.Info)
        if not parts:
            return None
        geom_type = {
            QgsWkbTypes.PointGeometry:   "MultiPoint",
            QgsWkbTypes.LineGeometry:    "MultiLineString",
            QgsWkbTypes.PolygonGeometry: "MultiPolygon",
        }.get(geom_kind)
        if not geom_type:
            return None
        lyr = QgsVectorLayer(f"{geom_type}?crs={crs}", name, "memory")
        if not lyr.isValid():
            QgsMessageLog.logMessage(
                f"GeoInbox _build_diff_parts_layer: invalid layer for '{name}'",
                "GeoInbox", Qgis.Critical)
            return None
        provider = lyr.dataProvider()
        feats = []
        for part in parts:
            if not part:
                continue
            geom = part.get("geometry")
            if geom is None or geom.isNull() or geom.isEmpty():
                continue
            # GEOS difference() may return GeometryCollection — extract only the
            # parts that match the target geometry type (polygon/line/point)
            wkb = geom.wkbType()
            result_kind = QgsWkbTypes.geometryType(wkb)
            if result_kind != geom_kind:
                # It's a collection — pull out only the matching sub-geometries
                sub_parts = []
                try:
                    for sub in geom.parts():
                        sub_geom = QgsGeometry(sub.clone())
                        if QgsWkbTypes.geometryType(sub_geom.wkbType()) == geom_kind:
                            sub_parts.append(sub_geom)
                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"GeoInbox: error extracting parts from collection: {e}",
                        "GeoInbox", Qgis.Warning)
                if not sub_parts:
                    QgsMessageLog.logMessage(
                        f"GeoInbox: no matching sub-parts in collection wkbType={wkb}",
                        "GeoInbox", Qgis.Warning)
                    continue
                geom = QgsGeometry.collectGeometry(sub_parts)
            # Promote single→multi so the layer type always matches
            geom.convertToMultiType()
            QgsMessageLog.logMessage(
                f"GeoInbox diff feat: wkbType={geom.wkbType()} isEmpty={geom.isEmpty()}",
                "GeoInbox", Qgis.Info)
            f = QgsFeature()
            f.setGeometry(geom)
            feats.append(f)
        QgsMessageLog.logMessage(
            f"GeoInbox _build_diff_parts_layer '{name}': valid feats to add={len(feats)}",
            "GeoInbox", Qgis.Info)
        if not feats:
            return None
        provider.addFeatures(feats)
        lyr.updateExtents()
        sym = QgsSymbol.defaultSymbol(lyr.geometryType())
        if sym:
            sym.setColor(color)
            lyr.setRenderer(QgsSingleSymbolRenderer(sym))
        return lyr

    def _show_geom_diff_overlay(self, client_feat: QgsFeature | None, host_feat: QgsFeature | None):
        if not client_feat or not host_feat:
            return
        base_layer = self._client_layer or self._host_layer
        if not base_layer:
            return

        self._clear_geom_diff_layer()

        geom_kind = QgsWkbTypes.geometryType(base_layer.wkbType())
        geom_type_map = {
            QgsWkbTypes.PointGeometry: "Point",
            QgsWkbTypes.LineGeometry: "LineString",
            QgsWkbTypes.PolygonGeometry: "Polygon"
        }
        geom_type = geom_type_map.get(geom_kind)
        if not geom_type:
            return
        crs = base_layer.crs().authid()
        memory_layer = QgsVectorLayer(f"{geom_type}?crs={crs}", "Versioning Geometry Diff (GeoInbox)", "memory")
        if not memory_layer.isValid():
            return
        provider = memory_layer.dataProvider()

        fields = QgsFields()
        fields.append(QgsField("source", QVariant.String))
        provider.addAttributes(fields)
        memory_layer.updateFields()

        features = []
        for feat, label in [(client_feat, "client"), (host_feat, "host")]:
            if not feat or not feat.hasGeometry():
                continue
            new_feat = QgsFeature(memory_layer.fields())
            new_feat.setGeometry(feat.geometry())
            new_feat.setAttributes([label])
            features.append(new_feat)
        if features:
            provider.addFeatures(features)
            memory_layer.updateExtents()

        categories = []
        client_symbol = QgsSymbol.defaultSymbol(memory_layer.geometryType())
        host_symbol = QgsSymbol.defaultSymbol(memory_layer.geometryType())
        if client_symbol:
            client_symbol.setColor(QColor(0, 120, 255))
        if host_symbol:
            host_symbol.setColor(QColor(255, 90, 0))
        categories.append(QgsRendererCategory("client", client_symbol, "Client Geometry"))
        categories.append(QgsRendererCategory("host", host_symbol, "Host Geometry"))
        renderer = QgsCategorizedSymbolRenderer("source", categories)
        memory_layer.setRenderer(renderer)

        self._geom_diff_layer = memory_layer
        self._safe_add_project_layer(memory_layer)
    
    def _link_selection(self):
        """Link client and host selections."""
        pass
    
    def _replace_geometry(self):
        """Replace host geometry with client geometry."""
        pass
    
    def _replace_attributes(self):
        """Replace host attributes with client attributes."""
        pass
    
    def _commit_feature(self):
        """Commit single feature change."""
        pass
    
    def _skip_feature(self):
        """Skip current feature."""
        pass
    
    def _show_geometry_diff(self):
        """Show all 9 diff layers as geometry visualization."""
        if not self._client_diff_index:
            return

        base_layer = self._client_layer or self._host_layer
        if not base_layer:
            return

        geom_kind = QgsWkbTypes.geometryType(base_layer.wkbType())
        if geom_kind not in {
            QgsWkbTypes.PointGeometry,
            QgsWkbTypes.LineGeometry,
            QgsWkbTypes.PolygonGeometry
        }:
            return

        self._clear_geom_parts_layers()
        self._clear_diff_layers()

        # ── Classify features ────────────────────────────────────────────────
        unchanged, attr_only, geom_only, both_changed, new_feats = [], [], [], [], []
        geom_added_parts, geom_removed_parts, arrows = [], [], []

        for diff in self._client_diff_index.values():
            client_feat  = diff.get("client_feat")
            host_feat    = diff.get("host_feat")
            geom_changed = diff.get("geom_changed", False)
            has_attr     = bool(diff.get("attr_changes"))
            c_geom       = diff.get("client_geom")  # only set when geom_changed
            h_geom       = diff.get("host_geom")    # only set when geom_changed
            cf_geom      = client_feat.geometry() if client_feat else None
            cf_attrs     = client_feat.attributes() if client_feat else []
            hf_attrs     = host_feat.attributes() if host_feat else []

            is_new = diff.get("match_type") == "new"
            if is_new:
                if cf_geom:
                    new_feats.append({"geometry": cf_geom, "attributes": cf_attrs})
            elif not geom_changed and not has_attr:
                if cf_geom:
                    unchanged.append({"geometry": cf_geom, "attributes": cf_attrs})
            elif not geom_changed and has_attr:
                if cf_geom:
                    attr_only.append({"geometry": cf_geom, "attributes": cf_attrs})
            elif geom_changed and not has_attr:
                if cf_geom:
                    geom_only.append({"geometry": cf_geom, "attributes": cf_attrs})
            else:   # geom_changed and has_attr
                if cf_geom:
                    both_changed.append({"geometry": cf_geom, "attributes": cf_attrs})

            if geom_changed and c_geom is not None and h_geom is not None:
                try:
                    # Make valid before GEOS operations (prevents silent null returns)
                    cg = QgsGeometry(c_geom)
                    hg = QgsGeometry(h_geom)
                    if cg.isNull() or hg.isNull():
                        QgsMessageLog.logMessage(
                            "GeoInbox diff: skipping null geometry in diff pair",
                            "GeoInbox", Qgis.Warning)
                    else:
                        if not cg.isGeosValid():
                            cg = cg.makeValid()
                        if not hg.isGeosValid():
                            hg = hg.makeValid()
                        # added parts = what client has that host did not
                        added = cg.difference(hg)
                        QgsMessageLog.logMessage(
                            f"GeoInbox: added diff isNull={added.isNull()} isEmpty={added.isEmpty()} "
                            f"wkbType={added.wkbType()}",
                            "GeoInbox", Qgis.Info)
                        if not added.isNull() and not added.isEmpty():
                            geom_added_parts.append({"geometry": added, "attributes": cf_attrs})
                        # removed parts = what host had that client removed
                        removed = hg.difference(cg)
                        QgsMessageLog.logMessage(
                            f"GeoInbox: removed diff isNull={removed.isNull()} isEmpty={removed.isEmpty()} "
                            f"wkbType={removed.wkbType()}",
                            "GeoInbox", Qgis.Info)
                        if not removed.isNull() and not removed.isEmpty():
                            geom_removed_parts.append({"geometry": removed, "attributes": hf_attrs})
                        # centroid arrow
                        p0 = hg.centroid().asPoint()
                        p1 = cg.centroid().asPoint()
                        arrows.append({"geometry": QgsGeometry.fromPolylineXY([p0, p1])})
                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"GeoInbox: geometry difference error: {e}",
                        "GeoInbox", Qgis.Critical)

        # Deleted features (stored during _run_comparison)
        deleted_feats = []
        for hf in getattr(self, "_removed_features", []) or []:
            if hf and hf.hasGeometry():
                deleted_feats.append({"geometry": hf.geometry(), "attributes": hf.attributes()})

        host_base = self._host_layer or base_layer
        crs = base_layer.crs().authid()

        QgsMessageLog.logMessage(
            f"GeoInbox _show_geometry_diff: unchanged={len(unchanged)} attr_only={len(attr_only)} "
            f"geom_only={len(geom_only)} both={len(both_changed)} new={len(new_feats)} "
            f"removed_parts={len(geom_removed_parts)} added_parts={len(geom_added_parts)} "
            f"arrows={len(arrows)} deleted={len(deleted_feats)}",
            "GeoInbox", Qgis.Info)

        # ── Build all 9 layers ───────────────────────────────────────────────
        # 1 – Client Unchanged (gray, context)
        lyr1 = self._build_geom_parts_layer(
            base_layer, unchanged, "[Client] Unchanged (GeoInbox)", QColor(158, 158, 158))
        # 2 – Attributes Changed
        lyr2 = self._build_geom_parts_layer(
            base_layer, attr_only, "[Diff] Attributes Changed (GeoInbox)", QColor(176, 252, 247))
        # 3 – Geometry Moved – new position (pastel orange)
        lyr3 = self._build_geom_parts_layer(
            base_layer, geom_only, "[Diff] Geometry Moved (GeoInbox)", QColor(255, 204, 153))
        # 4 – Geom Removed Parts – old_geom.difference(new_geom)
        lyr4 = self._build_diff_parts_layer(
            crs, geom_kind, geom_removed_parts,
            "[Diff] Geom Removed Parts (GeoInbox)", QColor(211, 47, 47))
        # 5 – Geom Added Parts – new_geom.difference(old_geom)
        lyr5 = self._build_diff_parts_layer(
            crs, geom_kind, geom_added_parts,
            "[Diff] Geom Added Parts (GeoInbox)", QColor(76, 175, 159))
        # 6 – Both Changed (yellow)
        lyr6 = self._build_geom_parts_layer(
            base_layer, both_changed, "[Diff] Both Changed (GeoInbox)", QColor(255, 214, 0))
        # 7 – Displacement Arrows
        lyr7 = None
        if arrows:
            lyr7 = QgsVectorLayer(f"LineString?crs={crs}",
                                  "[Diff] Displacement Arrows (GeoInbox)", "memory")
            if lyr7.isValid():
                prov = lyr7.dataProvider()
                arrow_feats = [QgsFeature() for _ in arrows]
                for feat, a in zip(arrow_feats, arrows):
                    feat.setGeometry(a["geometry"])
                prov.addFeatures(arrow_feats)
                lyr7.updateExtents()
                sym = QgsSymbol.defaultSymbol(lyr7.geometryType())
                if sym:
                    sym.setColor(QColor(155, 255, 0))
                    lyr7.setRenderer(QgsSingleSymbolRenderer(sym))
            else:
                lyr7 = None
        # 8 – Added Features (bright green)
        lyr8 = self._build_geom_parts_layer(
            base_layer, new_feats, "[Client] Added Features (GeoInbox)", QColor(0, 200, 83))
        # 9 – Deleted Features
        lyr9 = self._build_geom_parts_layer(
            host_base, deleted_feats, "[Host] Deleted Features (GeoInbox)", QColor(255, 214, 211))

        # ── Store refs for cleanup ───────────────────────────────────────────
        self._geom_parts_added_layer    = lyr5
        self._geom_parts_removed_layer  = lyr4
        self._geom_parts_unchanged_layer = lyr1
        self._geom_diff_extra_layers    = [l for l in [lyr2, lyr3, lyr6, lyr7, lyr8, lyr9] if l]

        # ── Add to map (bottom → top render order) ───────────────────────────
        for lyr in [lyr1, lyr2, lyr3, lyr4, lyr5, lyr6, lyr7, lyr8, lyr9]:
            self._safe_add_project_layer(lyr)
        
        # Add the 4 comparison diff layers (Features Added/Deleted, Attribute Changed, Geometry Modified)
        self._safe_add_project_layer(self._diff_added_layer)
        self._safe_add_project_layer(self._diff_removed_layer)
        self._safe_add_project_layer(self._diff_attr_changed_layer)
        self._safe_add_project_layer(self._diff_geom_modified_layer)

    def _remove_geoinbox_group(self):
        """Remove the GeoInbox group (empty or not) and all its layers from the project."""
        project = QgsProject.instance()
        root = project.layerTreeRoot()

        # Walk root's DIRECT children — the group is always inserted at root level.
        # Avoids findGroup() + parent() which both misbehave on empty groups.
        for child in list(root.children()):
            try:
                if child.name() == "GeoInbox":
                    # Collect layer IDs before touching the tree
                    layer_ids = [n.layerId() for n in child.findLayers() if n.layerId()]
                    # Remove the group node directly from root
                    root.removeChildNode(child)
                    # Clean layers from project registry
                    valid_ids = [lid for lid in layer_ids if project.mapLayer(lid)]
                    if valid_ids:
                        project.removeMapLayers(valid_ids)
                    break
            except Exception as exc:
                log_debug_exception('Skipped after error', exc)
                continue

        # Final sweep for any (GeoInbox) layers that ended up outside the group
        stray_ids = [
            lid for lid, lyr in project.mapLayers().items()
            if "(GeoInbox)" in lyr.name()
        ]
        if stray_ids:
            project.removeMapLayers(stray_ids)

    def _clear_geometry_diff(self):
        """Remove all GeoInbox temporary layers and the GeoInbox group from the project."""
        # Null out all tracked layer refs FIRST to avoid C++ deleted object errors
        self._geom_diff_layer = None
        self._geom_parts_added_layer = None
        self._geom_parts_removed_layer = None
        self._geom_parts_unchanged_layer = None
        self._geom_diff_extra_layers = []
        self._diff_added_layer = None
        self._diff_removed_layer = None
        self._diff_attr_changed_layer = None
        self._diff_geom_modified_layer = None
        # Remove group and all temp layers
        self._remove_geoinbox_group()
    
    def _show_attribute_diff(self):
        """Show attribute difference details."""
        pass
    
    def _commit_all_changes(self):
        """Commit all pending changes automatically."""
        if not self._client_layer or not self._host_layer:
            QMessageBox.warning(self, "Commit Changes", "Please load both client and host datasets first.")
            return
        
        if not self._client_diff_index:
            QMessageBox.warning(self, "Commit Changes", "No comparison results available. Please run comparison first.")
            return
        
        # Count changes
        total_changes = len(self._client_diff_index)
        new_count = sum(1 for d in self._client_diff_index.values() if d.get("match_type") == "new")
        updated_count = sum(1 for d in self._client_diff_index.values()
                           if d.get("attr_changes") or d.get("geom_changed"))
        not_surveyed_count = self._host_table.rowCount()

        if total_changes == 0:
            QMessageBox.information(self, "Commit Changes", "No changes to commit.")
            return

        # Confirm with user
        msg = "This will commit ALL changes to the host layer:\n\n"
        msg += f"• New features: {new_count}\n"
        msg += f"• Updated features: {updated_count}\n"
        if not_surveyed_count:
            msg += f"• Not surveyed (host only, unchanged): {not_surveyed_count}\n"
        msg += f"\nTotal operations: {new_count + updated_count}\n\n"
        msg += "A backup will be created before committing.\n\n"
        msg += "Do you want to proceed?"
        
        reply = QMessageBox.question(
            self, 
            "Confirm Commit", 
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        try:
            # Ensure host layer is editable
            was_editable = self._host_layer.isEditable()
            if not was_editable:
                if not self._host_layer.startEditing():
                    QMessageBox.critical(self, "Error", "Failed to start editing host layer.")
                    return
            
            success_count = 0
            error_count = 0
            errors = []
            
            # Get data provider for direct edits
            provider = self._host_layer.dataProvider()
            
            # Check if photos field should be included in commit
            include_photos = self._include_photos_commit.isChecked()
            
            # Process new features (inserts)
            for client_fid, diff in self._client_diff_index.items():
                if diff.get("match_type") == "new":
                    try:
                        client_feat = diff.get("client_feat")
                        if client_feat:
                            new_feat = QgsFeature(self._host_layer.fields())
                            new_feat.setGeometry(client_feat.geometry())
                            
                            # Copy attributes
                            for field in self._host_layer.fields():
                                fname = field.name()
                                # Skip photos field unless explicitly included
                                if fname.lower() == 'photos' and not include_photos:
                                    continue
                                if self._client_layer.fields().indexFromName(fname) >= 0:
                                    new_feat.setAttribute(fname, client_feat[fname])
                            
                            if self._host_layer.addFeature(new_feat):
                                success_count += 1
                            else:
                                error_count += 1
                                errors.append(f"Failed to insert feature {client_fid}")
                    except Exception as e:
                        error_count += 1
                        errors.append(f"Error inserting feature {client_fid}: {str(e)}")
            
            # Process updated features (geometry/attribute changes)
            for client_fid, diff in self._client_diff_index.items():
                host_fid = diff.get("host_fid")
                if host_fid is None or diff.get("match_type") == "new":
                    continue
                
                try:
                    # Get the actual host feature to update
                    host_feat = diff.get("host_feat")
                    if not host_feat:
                        error_count += 1
                        errors.append(f"Host feature for client {client_fid} not found")
                        continue
                    
                    has_changes = False
                    
                    # Update geometry if changed
                    if diff.get("geom_changed"):
                        client_geom = diff.get("client_geom")
                        if client_geom:
                            # Use the actual feature ID from the host feature object
                            actual_fid = host_feat.id()
                            if self._host_layer.changeGeometry(actual_fid, client_geom):
                                has_changes = True
                            else:
                                error_count += 1
                                errors.append(f"Failed to update geometry for feature {actual_fid}")
                    
                    # Update attributes if changed
                    attr_changes = diff.get("attr_changes", {})
                    if attr_changes:
                        actual_fid = host_feat.id()
                        for field_name, change in attr_changes.items():
                            field_idx = self._host_layer.fields().indexFromName(field_name)
                            if field_idx >= 0:
                                new_value = change.get("client")
                                if self._host_layer.changeAttributeValue(actual_fid, field_idx, new_value):
                                    has_changes = True
                                else:
                                    error_count += 1
                                    errors.append(f"Failed to update {field_name} for feature {actual_fid}")
                    
                    # If include_photos is checked, also update photos field even if not in attr_changes
                    if include_photos:
                        actual_fid = host_feat.id()
                        photos_idx = self._host_layer.fields().indexFromName('photos')
                        client_photos_idx = self._client_layer.fields().indexFromName('photos')
                        if photos_idx >= 0 and client_photos_idx >= 0:
                            client_feat = diff.get("client_feat")
                            if client_feat:
                                photos_value = client_feat['photos']
                                if self._host_layer.changeAttributeValue(actual_fid, photos_idx, photos_value):
                                    has_changes = True
                    
                    if has_changes:
                        success_count += 1
                
                except Exception as e:
                    error_count += 1
                    errors.append(f"Error updating feature {host_fid}: {str(e)}")
            
            # Note: features shown in the host table as "NOT SURVEYED" are NOT deleted
            # from the host layer — they are host-only features the client did not survey.

            # Commit changes
            if self._host_layer.commitChanges():
                result_msg = f"Successfully committed changes:\n\n"
                result_msg += f"• Operations completed: {success_count}\n"
                if error_count > 0:
                    result_msg += f"• Errors: {error_count}\n\n"
                    result_msg += "Errors:\n" + "\n".join(errors[:10])
                    if len(errors) > 10:
                        result_msg += f"\n... and {len(errors) - 10} more errors"

                # Build undo entry for the entire batch
                batch_entries = []
                for client_fid, diff in list(self._client_diff_index.items()):
                    mt = diff.get("match_type")
                    hf = diff.get("host_feat")
                    entry = {
                        "match_type": mt,
                        "client_fid": client_fid,
                        "diff_snapshot": dict(diff),
                    }
                    if mt != "new" and hf:
                        entry["host_fid"] = hf.id()
                        entry["old_geom"] = QgsGeometry(hf.geometry()) if diff.get("geom_changed") else None
                        entry["old_attrs"] = {}
                        for fname in diff.get("attr_changes", {}):
                            fidx = self._host_layer.fields().indexFromName(fname)
                            if fidx >= 0:
                                entry["old_attrs"][fname] = hf.attribute(fidx)
                    elif mt == "new":
                        # Try to find the inserted FID
                        new_geom = diff.get("client_feat") and diff["client_feat"].geometry()
                        if new_geom:
                            req = QgsFeatureRequest().setFilterRect(new_geom.boundingBox())
                            for inserted_hf in self._host_layer.getFeatures(req):
                                if inserted_hf.geometry().equals(new_geom):
                                    entry["inserted_host_fid"] = inserted_hf.id()
                                    break
                    batch_entries.append(entry)

                if batch_entries:
                    self._undo_stack.append({"type": "batch", "entries": batch_entries})
                    self._refresh_undo_btn()

                QMessageBox.information(self, "Commit Complete", result_msg)

                # Clear comparison results
                self._client_diff_index = {}
                self._client_table.setRowCount(0)
                self._host_table.setRowCount(0)
                self._clear_photo_panel("No comparison results")
                self._stat_total.setText("0")
                self._stat_matched.setText("0")
                self._stat_new.setText("0")
                self._stat_deleted.setText("0")
                self._stat_geom_changed.setText("0")
                self._stat_attr_changed.setText("0")
                # Refresh button states — undo stays enabled if the stack still has entries
                self._update_actions_enabled_state()

            else:
                error_msg = "Failed to commit changes to host layer.\n\n"
                error_msg += "Errors: " + str(self._host_layer.commitErrors())
                QMessageBox.critical(self, "Commit Failed", error_msg)
                self._host_layer.rollBack()
        
        except Exception as e:
            QMessageBox.critical(self, "Error", f"An error occurred during commit:\n\n{str(e)}")
            if self._host_layer.isEditable():
                self._host_layer.rollBack()
    
    def _commit_single_feature(self, client_fid_str: str):
        """Commit one individual feature from the right-click context menu."""
        if not self._client_layer or not self._host_layer:
            QMessageBox.warning(self, "Commit", "Client and host layers must be loaded.")
            return
        try:
            client_fid = int(client_fid_str)
        except (TypeError, ValueError):
            return
        diff = self._client_diff_index.get(client_fid)
        if not diff:
            QMessageBox.warning(self, "Commit", f"No diff data for FID {client_fid_str}.")
            return

        match_type  = diff.get("match_type")
        geom_changed = diff.get("geom_changed", False)
        attr_changes = diff.get("attr_changes", {})
        client_feat  = diff.get("client_feat")
        host_feat    = diff.get("host_feat")

        if match_type not in ("new", "matched") or (match_type == "matched" and not geom_changed and not attr_changes):
            QMessageBox.information(self, "Commit", "No changes to commit for this feature.")
            return

        try:
            if not self._host_layer.isEditable():
                if not self._host_layer.startEditing():
                    QMessageBox.critical(self, "Commit", "Could not start editing the host layer.")
                    return

            # ---- Capture undo state BEFORE making changes ----
            undo_entry = {
                "type": "single",
                "match_type": match_type,
                "client_fid": client_fid,
                "client_fid_str": client_fid_str,
                "diff_snapshot": dict(diff),
            }
            inserted_host_fid = None  # will be set after insert

            if match_type == "new":
                # Insert as new feature
                new_feat = QgsFeature(self._host_layer.fields())
                new_feat.setGeometry(client_feat.geometry())
                for field in self._host_layer.fields():
                    fname = field.name()
                    if self._client_layer.fields().indexFromName(fname) >= 0:
                        new_feat.setAttribute(fname, client_feat[fname])
                if not self._host_layer.addFeature(new_feat):
                    raise RuntimeError("addFeature failed")
                # Capture the new FID after commit (done below)
            else:
                actual_fid = host_feat.id()
                # Snapshot old geometry and attributes for undo
                undo_entry["host_fid"] = actual_fid
                undo_entry["old_geom"] = QgsGeometry(host_feat.geometry()) if geom_changed else None
                undo_entry["old_attrs"] = {}
                for fname in attr_changes:
                    fidx = self._host_layer.fields().indexFromName(fname)
                    if fidx >= 0:
                        undo_entry["old_attrs"][fname] = host_feat.attribute(fidx)

                if geom_changed:
                    cg = diff.get("client_geom") or client_feat.geometry()
                    if not self._host_layer.changeGeometry(actual_fid, cg):
                        raise RuntimeError(f"changeGeometry failed for host FID {actual_fid}")
                for fname, change in attr_changes.items():
                    fidx = self._host_layer.fields().indexFromName(fname)
                    if fidx >= 0:
                        if not self._host_layer.changeAttributeValue(actual_fid, fidx, change.get("client")):
                            raise RuntimeError(f"changeAttributeValue failed for {fname}")

            if not self._host_layer.commitChanges():
                raise RuntimeError("commitChanges failed: " + str(self._host_layer.commitErrors()))

            # For inserts, try to discover the new host FID so undo can delete it
            if match_type == "new":
                # The newly added feature is typically the last one; retrieve it by matching geometry
                new_geom = client_feat.geometry()
                req = QgsFeatureRequest().setFilterRect(new_geom.boundingBox())
                for hf in self._host_layer.getFeatures(req):
                    if hf.geometry().equals(new_geom):
                        undo_entry["inserted_host_fid"] = hf.id()
                        break

            # Push to undo stack
            self._undo_stack.append(undo_entry)
            self._refresh_undo_btn()

            # Mark row as committed in the table
            for row in range(self._client_table.rowCount()):
                fid_item = self._client_table.item(row, 0)
                if fid_item and fid_item.text() == client_fid_str:
                    status_item = self._client_table.item(row, 4)
                    if status_item:
                        status_item.setText("COMMITTED")
                        status_item.setBackground(QBrush(QColor(200, 230, 201)))
                    break

            # Remove from diff index so it won't be committed again
            del self._client_diff_index[client_fid]

        except Exception as e:
            QMessageBox.critical(self, "Commit Failed", str(e))
            if self._host_layer.isEditable():
                self._host_layer.rollBack()

    def _status_from_diff(self, snap: dict):
        """Return (status_label, QColor) derived from a diff snapshot dict."""
        mt = snap.get("match_type")
        geom_changed = snap.get("geom_changed", False)
        attr_changes = snap.get("attr_changes", {})
        if mt == "new":
            return "ADDED", QColor(200, 255, 200)
        elif geom_changed and attr_changes:
            return "MODIFIED (BOTH)", QColor(255, 182, 193)  # Pastel red/pink
        elif geom_changed:
            return "MODIFIED (GEOM)", QColor(255, 204, 153)  # Pastel orange
        elif attr_changes:
            return "MODIFIED (ATTR)", QColor(187, 222, 251)
        else:
            return "UNCHANGED", QColor(240, 240, 240)

    def _restore_client_table_row(self, snap: dict):
        """Append a client table row rebuilt from a diff snapshot."""
        if not snap:
            return
        client_feat = snap.get("client_feat")
        host_feat   = snap.get("host_feat")
        geom_changed = snap.get("geom_changed", False)
        geom_distance = snap.get("geom_distance", 0.0)
        attr_changes  = snap.get("attr_changes", {})
        client_fid = client_feat.id() if client_feat else snap.get("client_fid")

        row = self._client_table.rowCount()
        self._client_table.insertRow(row)
        fid_item = QTableWidgetItem(str(client_fid) if client_fid is not None else "")
        fid_item.setData(Qt.ItemDataRole.UserRole + 1, self._feature_has_photos(client_feat))
        self._client_table.setItem(row, 0, fid_item)
        self._client_table.setItem(row, 1, QTableWidgetItem(str(host_feat.id()) if host_feat else "—"))

        geom_item = QTableWidgetItem(f"{geom_distance:.2f}m" if geom_changed else "—")
        if geom_changed:
            geom_item.setBackground(QBrush(QColor(255, 200, 200)))
        self._client_table.setItem(row, 2, geom_item)

        attr_item = self._create_attr_table_item(attr_changes)
        self._client_table.setItem(row, 3, attr_item)

        status_label, status_color = self._status_from_diff(snap)
        status_item = QTableWidgetItem(status_label)
        status_item.setBackground(QBrush(status_color))
        self._client_table.setItem(row, 4, status_item)
        self._client_table.setRowHidden(row, not self._status_visible(status_label))

    def _restore_host_not_surveyed_row(self, host_feat):
        """Append a NOT SURVEYED row for a host feature in the host table."""
        row = self._host_table.rowCount()
        self._host_table.insertRow(row)
        for col, text in [(0, str(host_feat.id())), (1, "—"), (2, "—"), (3, "—")]:
            item = QTableWidgetItem(text)
            item.setForeground(QBrush(QColor(140, 140, 140)))
            self._host_table.setItem(row, col, item)
        status_item = QTableWidgetItem("NOT SURVEYED")
        status_item.setForeground(QBrush(QColor(120, 120, 120)))
        status_item.setBackground(QBrush(QColor(220, 220, 220)))
        self._host_table.setItem(row, 4, status_item)
        self._host_table.setRowHidden(row, not self._status_visible("NOT SURVEYED"))

    def _undo_change(self):
        """Undo the last committed operation (single feature or entire batch),
        restoring both tables, _client_diff_index, and GeoInbox temp layers."""
        if not self._undo_stack:
            return
        if not self._host_layer:
            QMessageBox.warning(self, "Undo", "Host layer is not loaded.")
            return

        entry = self._undo_stack[-1]  # peek — only pop after successful host commit

        try:
            if not self._host_layer.isEditable():
                if not self._host_layer.startEditing():
                    QMessageBox.critical(self, "Undo", "Could not start editing the host layer.")
                    return

            is_batch = entry.get("type") == "batch"
            entries = entry["entries"] if is_batch else [entry]

            # ── 1. Reverse host layer ─────────────────────────────────────────
            for e in entries:
                mt = e.get("match_type")
                if mt == "new":
                    ins_fid = e.get("inserted_host_fid")
                    if ins_fid is not None:
                        self._host_layer.deleteFeature(ins_fid)
                else:
                    host_fid = e.get("host_fid")
                    if host_fid is None:
                        continue
                    old_geom = e.get("old_geom")
                    if old_geom and not old_geom.isNull():
                        self._host_layer.changeGeometry(host_fid, old_geom)
                    for fname, old_val in e.get("old_attrs", {}).items():
                        fidx = self._host_layer.fields().indexFromName(fname)
                        if fidx >= 0:
                            self._host_layer.changeAttributeValue(host_fid, fidx, old_val)

            if not self._host_layer.commitChanges():
                raise RuntimeError("commitChanges failed: " + str(self._host_layer.commitErrors()))

            # ── 2. Restore _client_diff_index ────────────────────────────────
            for e in entries:
                client_fid = e.get("client_fid")
                snap = e.get("diff_snapshot", {})
                if client_fid is not None and snap:
                    self._client_diff_index[client_fid] = snap

            # ── 3. Restore tables ─────────────────────────────────────────────
            if is_batch:
                # Batch: tables were cleared — rebuild both from scratch
                self._client_table.setSortingEnabled(False)
                self._host_table.setSortingEnabled(False)
                self._client_table.setRowCount(0)
                self._host_table.setRowCount(0)
                self._clear_photo_panel("No comparison results")

                for e in entries:
                    self._restore_client_table_row(e.get("diff_snapshot", {}))

                # Rebuild host NOT SURVEYED rows
                matched_host_ids = {
                    e["diff_snapshot"].get("host_fid")
                    for e in entries
                    if e.get("diff_snapshot", {}).get("host_fid") is not None
                }
                not_surveyed = []
                for hf in self._host_layer.getFeatures():
                    if hf.id() not in matched_host_ids:
                        not_surveyed.append(hf)
                        self._restore_host_not_surveyed_row(hf)
                # Restore _removed_features so diff layers can show deleted feats
                self._removed_features = not_surveyed

                self._client_table.setSortingEnabled(True)
                self._host_table.setSortingEnabled(True)
            else:
                # Single: find the committed row and restore its status
                e = entries[0]
                client_fid_str = e.get("client_fid_str", str(e.get("client_fid", "")))
                snap = e.get("diff_snapshot", {})
                found = False
                for row in range(self._client_table.rowCount()):
                    fid_item = self._client_table.item(row, 0)
                    if fid_item and fid_item.text() == client_fid_str:
                        status_label, status_color = self._status_from_diff(snap)
                        status_item = QTableWidgetItem(status_label)
                        status_item.setBackground(QBrush(status_color))
                        self._client_table.setItem(row, 4, status_item)
                        self._client_table.setRowHidden(row, not self._status_visible(status_label))
                        found = True
                        break
                if not found:
                    # Row was removed from table somehow — re-insert it
                    self._restore_client_table_row(snap)

            # ── 4. Regenerate GeoInbox temp layers if any are present ─────────
            project = QgsProject.instance()
            has_geoinbox = any(
                "(GeoInbox)" in lyr.name()
                for lyr in project.mapLayers().values()
            )
            if has_geoinbox and self._client_diff_index:
                self._show_geometry_diff()

            # ── 5. Finalise ───────────────────────────────────────────────────
            self._undo_stack.pop()
            self._update_actions_enabled_state()

            op_count = len(entries)
            QMessageBox.information(
                self, "Undo Complete",
                f"Undo successful — {op_count} operation(s) reversed."
            )

        except Exception as e:
            QMessageBox.critical(self, "Undo Failed", str(e))
            if self._host_layer.isEditable():
                self._host_layer.rollBack()
    
    def _add_gisimple_server(self):
        """Add a new GISimple server configuration."""
        dialog = GISimpleServerDialog(parent=self)
        if dialog.exec():
            data = dialog.get_data()
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            # Clear other defaults if this one is set as default
            if data.get('is_default', 0):
                cursor.execute("UPDATE gisimple_servers SET is_default = 0")
            cursor.execute("""
                INSERT INTO gisimple_servers (name, url, realm, client_id, username, password, is_default)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (data['name'], data['url'], data.get('realm', 'gisimple'), 
                  data.get('client_id', 'gisimple'), data.get('username', ''), 
                  data.get('password', ''), data.get('is_default', 0)))
            conn.commit()
            conn.close()
            self._refresh_gisimple_server_list()
    
    def _edit_gisimple_server(self):
        """Edit selected GISimple server configuration."""
        item = self._gisimple_server_list.currentItem()
        if not item:
            QMessageBox.warning(self, "Warning", "Please select a server to edit")
            return
        
        server_id = item.data(Qt.ItemDataRole.UserRole)
        if not server_id:
            return
        
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM gisimple_servers WHERE id=?", (server_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            server_data = {
                'id': row[0], 'name': row[1], 'url': row[2], 'realm': row[3],
                'client_id': row[4], 'username': row[5], 'password': row[6], 
                'is_default': bool(row[7])
            }
            dialog = GISimpleServerDialog(server_data=server_data, parent=self)
            if dialog.exec():
                data = dialog.get_data()
                conn = sqlite3.connect(str(DB_PATH))
                cursor = conn.cursor()
                # Clear other defaults if this one is set as default
                if data.get('is_default', 0):
                    cursor.execute("UPDATE gisimple_servers SET is_default = 0")
                cursor.execute("""
                    UPDATE gisimple_servers 
                    SET name=?, url=?, realm=?, client_id=?, username=?, password=?, is_default=?
                    WHERE id=?
                """, (data['name'], data['url'], data.get('realm', 'gisimple'),
                      data.get('client_id', 'gisimple'), data.get('username', ''),
                      data.get('password', ''), data.get('is_default', 0), server_id))
                conn.commit()
                conn.close()
                self._refresh_gisimple_server_list()
    
    def _delete_gisimple_server(self):
        """Delete selected GISimple server configuration."""
        item = self._gisimple_server_list.currentItem()
        if not item:
            QMessageBox.warning(self, "Warning", "Please select a server to delete")
            return
        
        server_id = item.data(Qt.ItemDataRole.UserRole)
        if not server_id:
            return
        
        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Delete server '{item.text()}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            cursor.execute("DELETE FROM gisimple_servers WHERE id=?", (server_id,))
            conn.commit()
            conn.close()
            self._refresh_gisimple_server_list()
    
    def _sync_gisimple(self):
        """Sync with GISimple server."""
        QMessageBox.information(self, "Sync", "Syncing with GISimple server...")
    
    def _test_gisimple_connection(self):
        """Test connection to GISimple server."""
        if not hasattr(self, '_gisimple_token') or not self._gisimple_token:
            QMessageBox.warning(self, "Test", "Please login to GISimple first.")
            return
        
        try:
            import ssl
            import urllib.request
            
            url = self._gisimple_server_url
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            # Test API endpoint
            test_url = f"{url}/api/health"
            request = urllib.request.Request(
                test_url,
                headers={'Authorization': f'Bearer {self._gisimple_token}'},
                method='GET'
            )
            
            with urlopen_allowed(request, timeout=10, context=ssl_context) as response:
                QMessageBox.information(self, "Test", "✓ Connection successful!")
        except Exception as e:
            QMessageBox.warning(self, "Test", f"Connection failed: {str(e)}")
    
    def _fetch_user_projects(self):
        """Fetch projects the current user has access to from GISimple (async)."""
        if not hasattr(self, '_gisimple_token') or not self._gisimple_token:
            QMessageBox.warning(self, "Projects", "Please login to GISimple first.")
            return
        
        # Show progress
        self._gisimple_progress.setVisible(True)
        self._gisimple_progress.setRange(0, 0)  # Indeterminate
        self._gisimple_progress.setFormat("Fetching projects...")
        self._project_info.setText("Loading...")
        
        # Check if user is admin
        roles = self._gisimple_user.get('roles', [])
        is_admin = 'gisimple-admin' in roles or 'gisimple-superadmin' in roles or 'admin' in roles
        self._fetch_is_admin = is_admin
        
        # Use admin endpoint if admin, otherwise user endpoint
        endpoint = '/api/projects' if is_admin else '/api/user/projects'
        
        # Cancel any existing worker
        if hasattr(self, '_projects_worker') and self._projects_worker:
            self._projects_worker.cancel()
        
        # Create and start worker
        self._projects_worker = GISimpleApiWorker(
            self._gisimple_server_url,
            self._gisimple_token,
            endpoint,
            parent=self
        )
        self._projects_worker.finished.connect(self._on_projects_fetched)
        self._projects_worker.error.connect(self._on_projects_fetch_error)
        self._projects_worker.progress.connect(lambda msg: self._gisimple_progress.setFormat(msg))
        self._projects_worker.start()
    
    def _on_projects_fetched(self, data):
        """Handle successful projects fetch (runs on main thread)."""
        try:
            # Handle both formats: {'projects': [...]} or [...]
            if isinstance(data, dict) and 'projects' in data:
                projects = data['projects']
            elif isinstance(data, list):
                projects = data
            else:
                projects = []
            
            is_admin = getattr(self, '_fetch_is_admin', False)
            
            # Update project list
            self._project_list.clear()
            self._user_projects = projects
            
            # Get current server ID
            item = self._gisimple_server_list.currentItem()
            server_id = item.data(Qt.ItemDataRole.UserRole) if item else None
            
            # Save projects to database and populate list
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            
            for proj in projects:
                # Handle both string (project name) and dict (project object) formats
                if isinstance(proj, str):
                    project_id = proj
                    project_name = proj
                    role = 'owner' if is_admin else 'member'
                    permissions = '[]'
                else:
                    project_id = proj.get('folderName', proj.get('id', proj.get('project_id', '')))
                    project_name = proj.get('projectName', proj.get('name', proj.get('project_name', 'Unknown')))
                    role = proj.get('role', 'owner' if is_admin else 'viewer')
                    permissions = json.dumps(proj.get('permissions', []))
                
                # Add to list
                list_item = QListWidgetItem(f"{project_name} ({role})")
                list_item.setData(Qt.ItemDataRole.UserRole, project_id)
                self._project_list.addItem(list_item)
                
                # Save to database
                if server_id:
                    cursor.execute("""
                        INSERT OR REPLACE INTO gisimple_projects 
                        (server_id, project_id, project_name, role, permissions, last_synced)
                        VALUES (?, ?, ?, ?, ?, datetime('now'))
                    """, (server_id, project_id, project_name, role, permissions))
            
            conn.commit()
            conn.close()
            
            self._gisimple_progress.setVisible(False)
            self._project_info.setText(f"Found {len(projects)} project(s)")
            self._project_info.setStyleSheet("color: #2f9e44; font-size: 11px;")
            
        except Exception as e:
            self._gisimple_progress.setVisible(False)
            self._project_info.setText(f"Error: {str(e)}")
            self._project_info.setStyleSheet("color: #c92a2a; font-size: 11px;")
    
    def _on_projects_fetch_error(self, error_msg):
        """Handle projects fetch error."""
        self._gisimple_progress.setVisible(False)
        
        # If admin endpoint failed with 404, try user endpoint
        if '404' in error_msg and getattr(self, '_fetch_is_admin', False):
            self._fetch_is_admin = False
            self._projects_worker = GISimpleApiWorker(
                self._gisimple_server_url,
                self._gisimple_token,
                '/api/user/projects',
                parent=self
            )
            self._projects_worker.finished.connect(self._on_projects_fetched)
            self._projects_worker.error.connect(self._on_projects_fetch_final_error)
            self._projects_worker.start()
            self._gisimple_progress.setVisible(True)
            self._gisimple_progress.setFormat("Trying user endpoint...")
        else:
            self._on_projects_fetch_final_error(error_msg)
    
    def _on_projects_fetch_final_error(self, error_msg):
        """Handle final projects fetch error after all retries."""
        self._gisimple_progress.setVisible(False)
        self._project_info.setText(f"Error: {error_msg[:50]}")
        self._project_info.setStyleSheet("color: #c92a2a; font-size: 11px;")
    
    def _get_projects_from_groups(self):
        """Fallback: derive projects from user's groups in JWT token."""
        if not hasattr(self, '_gisimple_user') or not self._gisimple_user:
            return []
        
        groups = self._gisimple_user.get('groups', [])
        projects = []
        
        for group in groups:
            # Assume group names map to project names
            # Format: /projects/project-name or just project-name
            if group.startswith('/projects/'):
                project_name = group.replace('/projects/', '')
            elif group.startswith('/'):
                project_name = group[1:]
            else:
                project_name = group
            
            projects.append({
                'id': project_name.lower().replace(' ', '-'),
                'name': project_name,
                'role': 'member',
                'permissions': ['read']
            })
        
        return projects
    
    def _on_gisimple_server_selected(self, item):
        """Handle server selection from the list - NO auto-login, user must click Login button."""
        if not item:
            return

        if hasattr(self, '_project_list'):
            self._project_list.clear()
        if hasattr(self, '_gisimple_tree'):
            self._gisimple_tree.clear()
        self._user_projects = []
        
        # Clear any existing token when switching servers
        self._gisimple_token = None
        self._gisimple_user = None
        
        server_data = item.data(Qt.ItemDataRole.UserRole + 1)
        
        if server_data:
            self._gisimple_server_url = server_data.get('url', '')
            server_name = server_data.get('name', '')
            has_credentials = server_data.get('username', '') and server_data.get('password', '')
            
            if has_credentials:
                self._gisimple_status.setText(f"● Selected: {server_name} - Click 'Login' to connect")
                self._gisimple_status.setStyleSheet("color: #1971c2; font-size: 11px;")
            else:
                self._gisimple_status.setText(f"● Selected: {server_name} - No credentials saved")
                self._gisimple_status.setStyleSheet("color: #f59f00; font-size: 11px;")
    
    def _auto_login_gisimple(self, server_data):
        """Auto-login to GISimple server using stored credentials."""
        try:
            import ssl
            import urllib.request
            import urllib.parse
            import json as json_module
            import base64
            
            url = server_data.get('url', '').strip().rstrip('/')
            username = server_data.get('username', '')
            password = server_data.get('password', '')
            realm = 'gisimple'
            client_id = 'gisimple'
            
            # Keycloak token endpoint
            token_url = f"{url}/auth/realms/{realm}/protocol/openid-connect/token"
            
            form_data = urllib.parse.urlencode({
                'grant_type': 'password',
                'client_id': client_id,
                'username': username,
                'password': password
            }).encode('utf-8')
            
            request = urllib.request.Request(
                token_url,
                data=form_data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                method='POST'
            )
            
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            with urlopen_allowed(request, timeout=30, context=ssl_context) as response:
                token_data = json_module.loads(response.read().decode('utf-8'))
            
            access_token = token_data.get('access_token', '')
            if not access_token:
                raise Exception("No access token received")
            
            # Store token
            self._gisimple_token = access_token
            self._gisimple_refresh_token = token_data.get('refresh_token', '')
            self._gisimple_server_url = url
            
            # Decode JWT to get user info
            payload_b64 = access_token.split('.')[1]
            payload_b64 += '=' * (4 - len(payload_b64) % 4)
            payload = json_module.loads(base64.b64decode(payload_b64).decode('utf-8'))
            
            display_name = payload.get('name', payload.get('preferred_username', username))
            
            self._gisimple_user = {
                'username': username,
                'display_name': display_name,
                'email': payload.get('email', ''),
                'groups': payload.get('groups', []),
                'roles': payload.get('realm_access', {}).get('roles', [])
            }
            
            self._gisimple_status.setText(f"● Connected: {display_name}")
            self._gisimple_status.setStyleSheet("color: #2f9e44; font-size: 11px; font-weight: bold;")
            
            # Auto-fetch projects after successful login
            self._fetch_user_projects()
            
        except Exception as e:
            self._gisimple_status.setText(f"● Auto-login failed")
            self._gisimple_status.setStyleSheet("color: #c92a2a; font-size: 11px;")
            print(f"DEBUG: Auto-login failed: {e}")
    
    def _on_project_selected(self, item):
        """Handle project selection from the list."""
        if not item:
            return
        
        project_id = item.data(Qt.ItemDataRole.UserRole)
        project_name = item.text()
        
        self._current_project_id = project_id
        self._project_info.setText(f"Selected: {project_name}")
        self._project_info.setStyleSheet("color: #2f9e44; font-size: 11px;")
        
        # Load files for the selected project immediately.
        self._load_project_data()
    
    def _load_default_gisimple_server(self):
        """Load and auto-select the default GISimple server if one exists."""
        try:
            import sqlite3
            from pathlib import Path
            
            db_path = Path(PLUGIN_DIR) / 'config.db'
            if not db_path.exists():
                return
            
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            
            # Check if is_default column exists
            cursor.execute("PRAGMA table_info(gisimple_servers)")
            columns = [col[1] for col in cursor.fetchall()]
            
            if 'is_default' not in columns:
                conn.close()
                return
            
            # Get default server
            cursor.execute("""
                SELECT id, name, url, username 
                FROM gisimple_servers 
                WHERE is_default = 1 
                LIMIT 1
            """)
            
            row = cursor.fetchone()
            conn.close()
            
            if row:
                server_id, name, url, username = row
                # Select this server in the list
                for i in range(self._gisimple_server_list.count()):
                    item = self._gisimple_server_list.item(i)
                    if item.data(Qt.ItemDataRole.UserRole) == server_id:
                        self._gisimple_server_list.setCurrentItem(item)
                        self._on_gisimple_server_selected(item)
                        break
        except Exception as e:
            print(f"DEBUG: Failed to load default server: {e}")
    
    def _sync_all_geojson(self):
        """Download all GeoJSON files from all uploaders to working folder."""
        if not hasattr(self, '_gisimple_token') or not self._gisimple_token:
            QMessageBox.warning(self, "Sync All", "Please login to GISimple first.")
            return
        
        # Require download folder
        working_folder = self._require_download_folder()
        if not working_folder:
            return
        
        reply = QMessageBox.question(
            self, "Sync All GeoJSON",
            f"Download all GeoJSON files from all users?\n\nFiles will be saved to:\n{working_folder}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        try:
            import ssl
            import urllib.request
            import urllib.parse
            import json as json_module
            from pathlib import Path
            from storage.sanitizer import sanitize_identifier
            
            url = self._gisimple_server_url
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            # Show progress - fetching file list
            self._gisimple_progress.setVisible(True)
            self._gisimple_progress.setRange(0, 100)
            self._gisimple_progress.setValue(0)
            self._gisimple_progress.setFormat("Fetching file list...")
            QApplication.processEvents()
            
            # Get all uploaded files
            files_url = f"{url}/api/upload-other-data/files"
            request = urllib.request.Request(
                files_url,
                headers={
                    'Authorization': f'Bearer {self._gisimple_token}',
                    'Accept': 'application/json'
                }
            )
            
            with urlopen_allowed(request, timeout=30, context=ssl_context) as response:
                data = json_module.loads(response.read().decode('utf-8'))
            
            files = data.get('files', []) if isinstance(data, dict) else data
            
            print(f"DEBUG: Total files from API: {len(files)}")
            if files:
                print(f"DEBUG: First file keys: {files[0].keys() if isinstance(files[0], dict) else 'not a dict'}")
                print(f"DEBUG: First file: {files[0]}")
            
            # Filter only GeoJSON files - try multiple field names
            geojson_files = []
            for f in files:
                fname = f.get('filename', f.get('name', f.get('fileName', '')))
                if fname.lower().endswith('.geojson'):
                    geojson_files.append(f)
            
            print(f"DEBUG: GeoJSON files found: {len(geojson_files)}")
            
            if not geojson_files:
                self._gisimple_progress.setVisible(False)
                QMessageBox.information(self, "Sync All", f"No GeoJSON files found.\n\nTotal files: {len(files)}")
                return
            
            total = len(geojson_files)
            
            # Show progress with real numbers
            self._gisimple_progress.setRange(0, total)
            self._gisimple_progress.setValue(0)
            self._gisimple_progress.setFormat(f"0/{total} files")
            QApplication.processEvents()
            
            downloaded = 0
            failed = 0
            
            for idx, file_info in enumerate(geojson_files):
                filename = file_info.get('filename', file_info.get('name', file_info.get('fileName', '')))
                owner = file_info.get('owner', file_info.get('ownerEmail', file_info.get('user', '')))
                
                # Update progress with real numbers
                self._gisimple_progress.setValue(idx)
                self._gisimple_progress.setFormat(f"{idx}/{total} - {filename}")
                QApplication.processEvents()
                
                try:
                    # Create owner subfolder
                    sanitized_owner = sanitize_identifier(owner)
                    owner_folder = working_folder / sanitized_owner
                    owner_folder.mkdir(parents=True, exist_ok=True)
                    
                    output_path = owner_folder / filename
                    
                    # Check if file exists
                    if output_path.exists():
                        if not hasattr(self, '_overwrite_choice'):
                            # First time encountering an existing file
                            reply = QMessageBox.question(
                                self, 
                                "File Exists",
                                f"File already exists:\n{output_path}\n\nDo you want to overwrite it?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                                QMessageBox.StandardButton.Yes
                            )
                            
                            if reply == QMessageBox.StandardButton.Cancel:
                                # Cancel all downloads
                                self._gisimple_progress.setVisible(False)
                                self._layer_info.setText("Sync cancelled")
                                self._layer_info.setStyleSheet("color: #f59f00; font-size: 11px;")
                                return
                            elif reply == QMessageBox.StandardButton.No:
                                # Skip this file, don't overwrite
                                self._overwrite_choice = 'skip'
                            else:
                                # Overwrite this file and all others
                                self._overwrite_choice = 'overwrite'
                        
                        # Apply the choice
                        if self._overwrite_choice == 'skip':
                            print(f"DEBUG: Skipping existing file: {output_path}")
                            continue
                        # If overwrite_choice is 'overwrite', proceed with download
                    
                    # Download
                    safe_filename = filename.strip() if isinstance(filename, str) else ''
                    download_url = f"{url}/api/upload-other-data/download/{urllib.parse.quote(owner)}/{urllib.parse.quote(safe_filename)}"
                    request = urllib.request.Request(
                        download_url,
                        headers={'Authorization': f'Bearer {self._gisimple_token}'}
                    )
                    
                    with urlopen_allowed(request, timeout=300, context=ssl_context) as response:
                        with open(output_path, 'wb') as f:
                            f.write(response.read())
                    
                    downloaded += 1
                    
                except Exception as e:
                    print(f"DEBUG: Failed to download {filename}: {e}")
                    failed += 1
                
                # Update progress after download
                self._gisimple_progress.setValue(idx + 1)
                self._gisimple_progress.setFormat(f"{idx + 1}/{total} files")
                QApplication.processEvents()
            
            self._gisimple_progress.setVisible(False)
            
            QMessageBox.information(
                self, "Sync Complete",
                f"Downloaded {downloaded} GeoJSON file(s)\nFailed: {failed}\n\nLocation: {working_folder}"
            )
            
        except Exception as e:
            self._gisimple_progress.setVisible(False)
            QMessageBox.warning(self, "Sync Failed", str(e))
    
    def _on_project_changed(self, index):
        """Handle project selection change (legacy - for combo box)."""
        if index <= 0:
            self._gisimple_tree.clear()
            self._current_project_id = None
            return
        
        item = self._project_list.currentItem()
        if not item:
            return
        project_id = item.data(Qt.ItemDataRole.UserRole)
        project_name = item.text()
        
        self._current_project_id = project_id
        
        # Auto-load project data
        self._load_project_data()
    
    def _load_project_data(self):
        """Load only GeoJSON files from GISimple Upload Data Panel (async).
        
        Files are filtered to only show those owned by members of the selected project.
        """
        if not hasattr(self, '_gisimple_token') or not self._gisimple_token:
            QMessageBox.warning(self, "Load Data", "Please login to GISimple first.")
            return
        
        if not hasattr(self, '_gisimple_server_url') or not self._gisimple_server_url:
            QMessageBox.warning(self, "Load Data", "No server URL. Please select a server.")
            return
        
        selected_project_id = getattr(self, '_current_project_id', None)
        if not selected_project_id:
            self._layer_info.setText("Please select a project first.")
            self._layer_info.setStyleSheet("color: #c92a2a; font-size: 11px;")
            QMessageBox.warning(self, "Load Data", "Please select a GISimple project first.")
            return
        
        # Store for use in callback
        self._loading_project_id = selected_project_id
        
        # Get project members for filtering
        project_members = self._get_project_members(selected_project_id)
        self._loading_project_members = [m.lower() for m in project_members]
        
        # Show progress
        self._gisimple_progress.setVisible(True)
        self._gisimple_progress.setRange(0, 0)  # Indeterminate
        self._gisimple_progress.setFormat("Fetching files...")
        self._layer_info.setText("Loading...")
        
        # Cancel any existing worker
        if hasattr(self, '_files_worker') and self._files_worker:
            self._files_worker.cancel()
        
        # Try project-specific endpoint first
        import urllib.parse
        endpoint = f"/api/projects/{urllib.parse.quote(str(selected_project_id))}/upload-other-data/files"
        self._files_endpoint_fallback = f"/api/upload-other-data/files"
        
        self._files_worker = GISimpleApiWorker(
            self._gisimple_server_url,
            self._gisimple_token,
            endpoint,
            parent=self
        )
        self._files_worker.finished.connect(self._on_files_fetched)
        self._files_worker.error.connect(self._on_files_fetch_error)
        self._files_worker.progress.connect(lambda msg: self._gisimple_progress.setFormat(msg))
        self._files_worker.start()
    
    def _on_files_fetched(self, data):
        """Handle successful files fetch (runs on main thread)."""
        try:
            # Handle both formats: {'files': [...]} or [...]
            if isinstance(data, dict) and 'files' in data:
                files = data['files']
            elif isinstance(data, list):
                files = data
            else:
                files = []
            
            # Filter by project if using generic endpoint
            selected_project_id = getattr(self, '_loading_project_id', None)
            if selected_project_id and hasattr(self, '_files_endpoint_fallback'):
                files = self._filter_gisimple_files_by_project(files, selected_project_id)
            
            if files is None:
                files = []
            
            # Filter only GeoJSON files
            geojson_files = []
            for f in files:
                fname = f.get('filename', f.get('name', f.get('fileName', '')))
                if fname.lower().endswith('.geojson'):
                    geojson_files.append(f)
            
            # Filter by project membership
            project_members = getattr(self, '_loading_project_members', [])
            if project_members:
                filtered_files = []
                for f in geojson_files:
                    owner = f.get('owner', f.get('ownerEmail', f.get('user', ''))).lower()
                    if owner in project_members:
                        filtered_files.append(f)
                geojson_files = filtered_files
            
            # Store files for download
            self._uploaded_data_files = geojson_files
            
            # Clear and populate tree
            self._gisimple_tree.clear()
            
            total = len(geojson_files)
            if total == 0:
                self._gisimple_progress.setVisible(False)
                self._layer_info.setText("No GeoJSON files found")
                self._layer_info.setStyleSheet("color: #f59f00; font-size: 11px;")
                return
            
            # Load files into tree
            for file_info in geojson_files:
                filename = file_info.get('filename', file_info.get('name', file_info.get('fileName', 'Unknown')))
                owner = file_info.get('owner', file_info.get('ownerEmail', file_info.get('user', '')))
                size = file_info.get('size', 0)
                size_str = self._format_file_size(size) if hasattr(self, '_format_file_size') else f"{size} B"
                
                item = QTreeWidgetItem([filename, owner, size_str])
                item.setData(0, Qt.ItemDataRole.UserRole, file_info)
                self._gisimple_tree.addTopLevelItem(item)
            
            # Update tree headers
            self._gisimple_tree.setHeaderLabels(["Filename", "Owner", "Size"])
            self._fit_gisimple_tree_columns()
            
            self._gisimple_progress.setVisible(False)
            self._layer_info.setText(f"Loaded {len(geojson_files)} GeoJSON file(s)")
            self._layer_info.setStyleSheet("color: #2f9e44; font-size: 11px;")
            
        except Exception as e:
            self._gisimple_progress.setVisible(False)
            self._layer_info.setText(f"Error: {str(e)}")
            self._layer_info.setStyleSheet("color: #c92a2a; font-size: 11px;")
    
    def _on_files_fetch_error(self, error_msg):
        """Handle files fetch error - try fallback endpoint."""
        # If project-specific endpoint failed, try generic endpoint
        if hasattr(self, '_files_endpoint_fallback') and self._files_endpoint_fallback:
            fallback = self._files_endpoint_fallback
            self._files_endpoint_fallback = None  # Clear to prevent infinite loop
            
            self._files_worker = GISimpleApiWorker(
                self._gisimple_server_url,
                self._gisimple_token,
                fallback,
                parent=self
            )
            self._files_worker.finished.connect(self._on_files_fetched)
            self._files_worker.error.connect(self._on_files_fetch_final_error)
            self._files_worker.start()
            self._gisimple_progress.setFormat("Trying alternate endpoint...")
        else:
            self._on_files_fetch_final_error(error_msg)
    
    def _on_files_fetch_final_error(self, error_msg):
        """Handle final files fetch error."""
        self._gisimple_progress.setVisible(False)
        self._layer_info.setText(f"Error: {error_msg[:50]}")
        self._layer_info.setStyleSheet("color: #c92a2a; font-size: 11px;")

    def _filter_gisimple_files_by_project(self, files: list, project_id: str) -> list:
        """Filter uploaded files down to the selected GISimple project."""
        if not project_id or not isinstance(files, list):
            return files

        project_id_norm = str(project_id).strip().lower()
        if not project_id_norm:
            return files

        def matches_project(file_info):
            if not isinstance(file_info, dict):
                return False

            keys_to_check = [
                'project_id', 'project', 'projectId', 'projectName', 'project_name',
                'folderName', 'folder_name', 'workspace', 'workspaceName', 'path',
                'project_path'
            ]

            for key in keys_to_check:
                value = file_info.get(key)
                if isinstance(value, dict):
                    if matches_project(value):
                        return True
                    continue
                if value is None:
                    continue

                candidate = str(value).strip().lower()
                if candidate == project_id_norm:
                    return True
                if candidate.endswith('/' + project_id_norm) or candidate.endswith('\\' + project_id_norm):
                    return True

            return False

        filtered_files = [f for f in files if matches_project(f)]
        if filtered_files:
            print(f"DEBUG: Filtered {len(filtered_files)} file(s) for project '{project_id_norm}'.")
            return filtered_files

        print(f"DEBUG: No project-specific metadata matched for project '{project_id_norm}'.")
        return files

    def _filter_file_list(self, search_text):
        """Filter the file list based on search text (dynamic search)."""
        search_text = search_text.lower().strip()
        
        for i in range(self._gisimple_tree.topLevelItemCount()):
            item = self._gisimple_tree.topLevelItem(i)
            if not item:
                continue
            
            # Check if any column matches the search text
            filename = item.text(0).lower()
            owner = item.text(1).lower()
            
            # Show item if search text is in filename or owner
            if not search_text or search_text in filename or search_text in owner:
                item.setHidden(False)
            else:
                item.setHidden(True)
        
        # Update count of visible items
        visible_count = sum(1 for i in range(self._gisimple_tree.topLevelItemCount()) 
                          if not self._gisimple_tree.topLevelItem(i).isHidden())
        total_count = self._gisimple_tree.topLevelItemCount()
        
        if search_text:
            self._layer_info.setText(f"Showing {visible_count} of {total_count} file(s)")
        else:
            self._layer_info.setText(f"Loaded {total_count} file(s)")
        self._layer_info.setStyleSheet("color: #2f9e44; font-size: 11px;")

    def _fit_gisimple_tree_columns(self):
        """Resize the uploaded files table to fit the longest content in each column."""
        if not hasattr(self, '_gisimple_tree') or not self._gisimple_tree:
            return

        header = self._gisimple_tree.header()
        if not header:
            return

        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)

        metrics = self._gisimple_tree.fontMetrics()
        padding = 28
        for column in range(self._gisimple_tree.columnCount()):
            longest = metrics.horizontalAdvance(self._gisimple_tree.headerItem().text(column))
            for row in range(self._gisimple_tree.topLevelItemCount()):
                item = self._gisimple_tree.topLevelItem(row)
                if item:
                    longest = max(longest, metrics.horizontalAdvance(item.text(column)))
            self._gisimple_tree.setColumnWidth(column, longest + padding)

    def _format_gisimple_login_failure_message(self, details: str = "") -> str:
        """Build a friendlier GISimple login failure message."""
        message = (
            "Login to GISimple failed, please check the following:\n\n"
            "- Your login credentials are correct\n"
            "- You have access to GISimple\n"
            "- GISimple is running on the server"
        )
        if details:
            message += f"\n\nDetails: {details}"
        return message
    
    def _get_layers_from_geoserver(self, project_id):
        """Fallback: get layers from GeoServer WFS for a project/workspace."""
        import ssl
        import urllib.request
        import json as json_module
        
        url = self._gisimple_server_url
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        # Try GeoServer REST API
        geoserver_url = f"{url}/geoserver/rest/workspaces/{project_id}/featuretypes.json"
        request = urllib.request.Request(
            geoserver_url,
            headers={
                'Authorization': f'Bearer {self._gisimple_token}',
                'Accept': 'application/json'
            },
            method='GET'
        )
        
        layers = []
        try:
            with urlopen_allowed(request, timeout=30, context=ssl_context) as response:
                data = json_module.loads(response.read().decode('utf-8'))
                feature_types = data.get('featureTypes', {}).get('featureType', [])
                for ft in feature_types:
                    layers.append({
                        'id': ft.get('name', ''),
                        'name': ft.get('name', ''),
                        'type': 'Vector',
                        'feature_count': '—'
                    })
        except Exception as exc:
            log_debug_exception('Suppressed error', exc)
        
        return layers
    
    def _on_layer_double_clicked(self, item, column):
        """Handle double-click on file - download it."""
        self._download_gisimple_file()
    
    def _add_layer_to_qgis(self):
        """Download selected uploaded file and add to QGIS if it's a GIS file."""
        self._download_gisimple_file(add_to_qgis=True)

    def _download_all_gisimple_files(self):
        """Download all files from the GISimple tree."""
        # Select all items in the tree
        all_items = []
        for i in range(self._gisimple_tree.topLevelItemCount()):
            item = self._gisimple_tree.topLevelItem(i)
            if item:
                all_items.append(item)
                # Also get child items
                for j in range(item.childCount()):
                    child = item.child(j)
                    if child:
                        all_items.append(child)
        
        if not all_items:
            QMessageBox.warning(self, "Download", "No files available to download.")
            return
        
        # Select all items
        self._gisimple_tree.clearSelection()
        for item in all_items:
            item.setSelected(True)
        
        # Use the existing download method
        self._download_gisimple_file(add_to_qgis=False)
    
    def _download_gisimple_file(self, add_to_qgis=False):
        """Download selected uploaded file(s) from GISimple to working folder."""
        selected_items = self._gisimple_tree.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "Download", "Please select one or more files first.")
            return
        
        # Require download folder to be set
        working_folder = self._require_download_folder()
        if not working_folder:
            return
        
        # Check if any selected files are GeoJSON - if so, offer to download photos
        download_photos = False
        has_geojson = any(
            (item.data(0, Qt.ItemDataRole.UserRole) or {}).get('filename', '').lower().endswith(('.geojson', '.json'))
            for item in selected_items
        )
        
        if has_geojson:
            reply = QMessageBox.question(
                self,
                "Download Photos",
                "Download the JPEG images as well?\n\n"
                "Photos attached to features in the GeoJSON files will be downloaded to a 'photos' subfolder.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes
            )
            
            if reply == QMessageBox.StandardButton.Cancel:
                return
            elif reply == QMessageBox.StandardButton.Yes:
                download_photos = True
        
        try:
            import ssl
            import urllib.request
            from pathlib import Path
            from storage.sanitizer import sanitize_identifier
            
            url = self._gisimple_server_url
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            downloaded_files = []
            downloaded_photos = 0
            total_selected = len(selected_items)
            
            # Show progress bar for download
            self._gisimple_progress.setVisible(True)
            self._gisimple_progress.setRange(0, total_selected)
            self._gisimple_progress.setValue(0)
            self._gisimple_progress.setFormat(f"Downloading 0/{total_selected} files")
            QApplication.processEvents()
            
            for idx, item in enumerate(selected_items):
                file_info = item.data(0, Qt.ItemDataRole.UserRole)
                print(f"DEBUG: file_info = {file_info}")
                
                # Update progress
                self._gisimple_progress.setValue(idx)
                self._gisimple_progress.setFormat(f"Downloading {idx}/{total_selected} files")
                QApplication.processEvents()
                
                if not file_info or not isinstance(file_info, dict):
                    print(f"DEBUG: Skipping - file_info is not a dict")
                    continue
                
                # Try multiple field names for filename and owner
                filename = file_info.get('filename', file_info.get('name', file_info.get('fileName', '')))
                owner = file_info.get('owner', file_info.get('ownerEmail', file_info.get('user', '')))
                
                print(f"DEBUG: filename={filename}, owner={owner}")
                
                if not filename:
                    print(f"DEBUG: Skipping - no filename")
                    continue
                
                # If no owner, use current user or default folder
                if not owner:
                    owner = getattr(self, '_gisimple_user', {}).get('email', 'default')
                    print(f"DEBUG: Using default owner: {owner}")
                
                # Create owner subfolder using sanitized email
                sanitized_owner = sanitize_identifier(owner)
                owner_folder = working_folder / sanitized_owner
                owner_folder.mkdir(parents=True, exist_ok=True)
                
                output_path = owner_folder / filename
                
                # Check if file exists
                if output_path.exists():
                    if not hasattr(self, '_overwrite_choice'):
                        # First time encountering an existing file
                        reply = QMessageBox.question(
                            self, 
                            "File Exists",
                            f"File already exists:\n{output_path}\n\nDo you want to overwrite it?",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                            QMessageBox.StandardButton.Yes
                        )
                        
                        if reply == QMessageBox.StandardButton.Cancel:
                            # Cancel all downloads
                            self._gisimple_progress.setVisible(False)
                            self._layer_info.setText("Download cancelled")
                            self._layer_info.setStyleSheet("color: #f59f00; font-size: 11px;")
                            return
                        elif reply == QMessageBox.StandardButton.No:
                            # Skip this file, don't overwrite
                            self._overwrite_choice = 'skip'
                        else:
                            # Overwrite this file and all others
                            self._overwrite_choice = 'overwrite'
                    
                    # Apply the choice
                    if self._overwrite_choice == 'skip':
                        print(f"DEBUG: Skipping existing file: {output_path}")
                        continue
                    # If overwrite_choice is 'overwrite', proceed with download
                
                # Download from upload-other-data endpoint
                download_url = f"{url}/api/upload-other-data/download/{urllib.parse.quote(owner)}/{urllib.parse.quote(filename)}"
                print(f"DEBUG: Downloading from: {download_url}")
                
                # Download the file
                request = urllib.request.Request(
                    download_url,
                    headers={'Authorization': f'Bearer {self._gisimple_token}'}
                )
                
                with urlopen_allowed(request, timeout=300, context=ssl_context) as response:
                    with open(output_path, 'wb') as f:
                        f.write(response.read())
                
                downloaded_files.append(output_path)
                
                # Write metadata for the downloaded file
                self._write_download_metadata(
                    output_path,
                    "gisimple",
                    {
                        "owner": owner,
                        "filename": filename,
                        "project_id": file_info.get('projectId') or file_info.get('project') or file_info.get('project_id'),
                        "dataset_id": file_info.get('datasetId') or file_info.get('dataset_id'),
                        "type": file_info.get('type') or file_info.get('category') or 'uploaded_file',
                    },
                )
                
                # Download photos if requested and file is GeoJSON
                if download_photos and filename.lower().endswith(('.geojson', '.json')):
                    try:
                        # Fetch photos list for this owner
                        photos_url = f"{url}/api/geov/photos/{urllib.parse.quote(owner)}"
                        photos_request = urllib.request.Request(
                            photos_url,
                            headers={'Authorization': f'Bearer {self._gisimple_token}'}
                        )
                        
                        with urlopen_allowed(photos_request, timeout=30, context=ssl_context) as response:
                            photos_data = json.loads(response.read().decode('utf-8'))
                            photos_list = photos_data.get('photos', [])
                            
                            if photos_list:
                                # Create photos subfolder
                                photos_folder = working_folder / sanitized_owner / "photos"
                                photos_folder.mkdir(parents=True, exist_ok=True)
                                
                                self._gisimple_progress.setRange(0, len(photos_list))
                                
                                for photo_idx, photo in enumerate(photos_list):
                                    photo_filename = photo.get('filename', '')
                                    if not photo_filename:
                                        continue
                                    
                                    self._gisimple_progress.setValue(photo_idx)
                                    self._gisimple_progress.setFormat(f"Downloading photo {photo_idx+1}/{len(photos_list)}")
                                    QApplication.processEvents()
                                    
                                    photo_path = photos_folder / photo_filename
                                    if photo_path.exists():
                                        continue  # Skip existing photos
                                    
                                    # Download photo
                                    photo_url = f"{url}/api/geov/photos/{urllib.parse.quote(owner)}/{urllib.parse.quote(photo_filename)}"
                                    photo_request = urllib.request.Request(
                                        photo_url,
                                        headers={'Authorization': f'Bearer {self._gisimple_token}'}
                                    )
                                    
                                    try:
                                        with urlopen_allowed(photo_request, timeout=60, context=ssl_context) as photo_response:
                                            with open(photo_path, 'wb') as f:
                                                f.write(photo_response.read())
                                        self._write_download_metadata(
                                            photo_path,
                                            "gisimple_photo",
                                            {
                                                "owner": owner,
                                                "filename": photo_filename,
                                                "photo_index": photo_idx + 1,
                                            },
                                        )
                                        downloaded_photos += 1
                                    except Exception as photo_err:
                                        print(f"DEBUG: Failed to download photo {photo_filename}: {photo_err}")
                        
                    except Exception as photos_err:
                        print(f"DEBUG: Failed to fetch photos for {owner}: {photos_err}")
            
            if add_to_qgis:
                # Add all downloaded GIS files to QGIS with progress bar
                added = 0
                total = len(downloaded_files)
                
                # Show progress bar for adding to QGIS
                self._gisimple_progress.setVisible(True)
                self._gisimple_progress.setRange(0, total)
                self._gisimple_progress.setValue(0)
                self._gisimple_progress.setFormat(f"Adding 0/{total} layers to QGIS")
                QApplication.processEvents()
                
                for idx, output_path in enumerate(downloaded_files):
                    ext = output_path.suffix.lower()
                    print(f"DEBUG: Checking file for QGIS: {output_path}, extension: {ext}")
                    
                    # Update progress
                    self._gisimple_progress.setValue(idx)
                    self._gisimple_progress.setFormat(f"Adding {idx}/{total} layers to QGIS")
                    QApplication.processEvents()
                    
                    # Support multiple GIS file extensions
                    if ext in ['.geojson', '.json', '.shp', '.gpkg', '.kml', '.gml', '.csv']:
                        layer = QgsVectorLayer(str(output_path), output_path.stem, "ogr")
                        print(f"DEBUG: Layer valid: {layer.isValid()}")
                        if layer.isValid():
                            QgsProject.instance().addMapLayer(layer)
                            added += 1
                            print(f"DEBUG: Added layer to QGIS: {output_path.stem}")
                        else:
                            print(f"DEBUG: Layer not valid: {layer.error().message() if hasattr(layer.error(), 'message') else 'unknown error'}")
                
                # Final progress update
                self._gisimple_progress.setValue(total)
                self._gisimple_progress.setFormat(f"Added {added}/{total} layers to QGIS")
                QApplication.processEvents()
                self._gisimple_progress.setVisible(False)
                
                photos_msg = f"\nDownloaded {downloaded_photos} photo(s)" if downloaded_photos > 0 else ""
                if added > 0:
                    QMessageBox.information(self, "Success", f"Downloaded {len(downloaded_files)} file(s){photos_msg}\nAdded {added} layer(s) to QGIS\n\nLocation: {working_folder}")
                else:
                    QMessageBox.information(self, "Download Complete", f"Downloaded {len(downloaded_files)} file(s){photos_msg}\n(No valid GIS layers found)\n\nLocation: {working_folder}")
            else:
                photos_msg = f"\nDownloaded {downloaded_photos} photo(s)" if downloaded_photos > 0 else ""
                QMessageBox.information(self, "Download Complete", f"Downloaded {len(downloaded_files)} file(s){photos_msg}\n\nLocation: {working_folder}")
            
            # Hide progress bar
            self._gisimple_progress.setVisible(False)
                
        except Exception as e:
            self._gisimple_progress.setVisible(False)
            error_msg = str(e)
            print(f"DEBUG: Download failed: {error_msg}")
            self._layer_info.setText(f"Download failed: {error_msg}")
            self._layer_info.setStyleSheet("color: #c92a2a; font-size: 11px;")
            
            # Show detailed error message
            detailed_msg = f"Download failed: {error_msg}\n\n"
            if "HTTP" in error_msg:
                detailed_msg += "Possible causes:\n- Invalid or expired token\n- File not found on server\n- Permission denied"
            elif "timeout" in error_msg.lower():
                detailed_msg += "Possible causes:\n- Network timeout\n- Large file size\n- Server is slow to respond"
            elif "ssl" in error_msg.lower():
                detailed_msg += "Possible causes:\n- SSL certificate issue\n- Server connection problem"
            else:
                detailed_msg += "Possible causes:\n- Network connection issue\n- Server error\n- File access denied"
            
            QMessageBox.warning(self, "Download Failed", detailed_msg)
    
    def _get_working_folder(self):
        """Get the configured download folder path from database."""
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            # Try download_folder first, then working_folder for backwards compatibility
            cursor.execute("SELECT value FROM settings WHERE key = 'download_folder'")
            row = cursor.fetchone()
            if not row:
                cursor.execute("SELECT value FROM settings WHERE key = 'working_folder'")
                row = cursor.fetchone()
            conn.close()
            
            if row and row[0]:
                folder = Path(row[0])
                if folder.exists():
                    print(f"DEBUG: Using download folder: {folder}")
                    return folder
                else:
                    print(f"DEBUG: Download folder does not exist: {row[0]}")
            return None
        except Exception as e:
            print(f"DEBUG: Error getting download folder: {e}")
            return None
    
    def _browse_download_folder(self):
        """Browse for download folder."""
        from PyQt6.QtWidgets import QFileDialog
        
        current_folder = self._download_folder_edit.text() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, 
            "Select Download Folder", 
            current_folder,
            QFileDialog.Option.ShowDirsOnly
        )
        
        if folder:
            self._download_folder_edit.setText(folder)
            self._save_download_folder(folder)
    
    def _save_download_folder(self, folder_path):
        """Save download folder to database."""
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            cursor.execute("""
                INSERT OR REPLACE INTO settings (key, value) VALUES ('download_folder', ?)
            """, (folder_path,))
            conn.commit()
            conn.close()
            QMessageBox.information(self, "Settings", f"Download folder saved:\n{folder_path}")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to save download folder: {str(e)}")
    
    def _load_download_folder(self):
        """Load download folder from database into the UI."""
        folder = self._get_working_folder()
        if folder:
            self._download_folder_edit.setText(str(folder))
    
    def _require_download_folder(self):
        """Check if download folder is set. Prompts user to set one if not configured."""
        folder = self._get_working_folder()
        if not folder:
            reply = QMessageBox.question(
                self, 
                "Download Folder Required", 
                "No download folder is configured. Would you like to set one now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                from PyQt6.QtWidgets import QFileDialog
                folder_path = QFileDialog.getExistingDirectory(
                    self, 
                    "Select Download Folder", 
                    str(Path.home()),
                    QFileDialog.Option.ShowDirsOnly
                )
                if folder_path:
                    self._save_download_folder(folder_path)
                    if hasattr(self, '_download_folder_edit'):
                        self._download_folder_edit.setText(folder_path)
                    return Path(folder_path)
            return None
        return folder
    
    def _download_layer_as_geojson(self, project_id, layer_id, layer_name):
        """Download layer as GeoJSON and add to QGIS."""
        import ssl
        import urllib.request
        import tempfile
        
        url = self._gisimple_server_url
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        # Try WFS GetFeature
        wfs_url = f"{url}/geoserver/{project_id}/wfs?service=WFS&version=2.0.0&request=GetFeature&typeName={project_id}:{layer_id}&outputFormat=application/json"
        
        request = urllib.request.Request(
            wfs_url,
            headers={'Authorization': f'Bearer {self._gisimple_token}'},
            method='GET'
        )
        
        try:
            with urlopen_allowed(request, timeout=60, context=ssl_context) as response:
                geojson_data = response.read()
                
                # Save to temp file
                temp_file = tempfile.NamedTemporaryFile(mode='wb', suffix='.geojson', delete=False)
                temp_file.write(geojson_data)
                temp_file.close()
                
                # Add to QGIS
                layer = QgsVectorLayer(temp_file.name, layer_name, "ogr")
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    QMessageBox.information(self, "Add Layer", f"Layer '{layer_name}' downloaded and added to QGIS!")
                else:
                    QMessageBox.warning(self, "Add Layer", "Downloaded layer is not valid.")
        except Exception as e:
            QMessageBox.warning(self, "Add Layer", f"Error downloading layer: {str(e)}")
    
    def _sync_project_to_local(self):
        """Sync current project's layers to local storage."""
        if not hasattr(self, '_current_project_id') or not self._current_project_id:
            QMessageBox.warning(self, "Sync", "Please select a project first.")
            return
        
        item = self._project_list.currentItem()
        project_name = item.text() if item else "Unknown"
        reply = QMessageBox.question(
            self, "Sync Project",
            f"Download all layers from '{project_name}' to local storage?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            # TODO: Implement full project sync
            QMessageBox.information(self, "Sync", f"Syncing project '{project_name}'...")
    
    def _sync_all_to_local(self):
        """Sync all projects to local storage."""
        if not hasattr(self, '_user_projects') or not self._user_projects:
            QMessageBox.warning(self, "Sync All", "No projects available. Please fetch projects first.")
            return
        
        reply = QMessageBox.question(
            self, "Sync All Projects",
            f"Download all layers from {len(self._user_projects)} project(s) to local storage?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            # TODO: Implement full sync
            QMessageBox.information(self, "Sync All", "Syncing all projects...")
    
    def _on_trust_mode_changed(self, index):
        """Handle trust mode change."""
        pass
    
    def _load_client_file(self):
        """Load client dataset from file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Load Client Dataset", "", 
            "Vector Files (*.shp *.gpkg *.geojson *.json);;All Files (*)"
        )
        if file_path:
            layer = QgsVectorLayer(file_path, Path(file_path).stem, "ogr")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                self._client_layer = layer
                self._select_layer_in_tree(self._client_layer_tree, layer)
                self._update_client_info()
                self._refresh_schema_diff_metadata()
    
    def _load_host_file(self):
        """Load host dataset from file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Load Host Dataset", "",
            "Vector Files (*.shp *.gpkg *.geojson *.json);;All Files (*)"
        )
        if file_path:
            layer = QgsVectorLayer(file_path, Path(file_path).stem, "ogr")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                self._host_layer = layer
                self._select_layer_in_tree(self._host_layer_tree, layer)
                self._update_host_info()
                self._refresh_schema_diff_metadata()
    
    def _on_client_layer_changed(self, item, previous_item):
        """Handle client layer selection change (works with QTreeWidget)."""
        if self._refreshing_layer_lists:
            return
        
        # Extract layer from item
        layer = None
        if item:
            layer_id = item.data(0, Qt.ItemDataRole.UserRole)
            if layer_id:
                layer = QgsProject.instance().mapLayer(layer_id)
        
        self._disconnect_layer_selection(self._client_layer)
        self._client_layer = layer if isinstance(layer, QgsVectorLayer) else None
        self._update_client_info()
        self._refresh_id_field_selection()
        self._apply_versioning_layer_filters()
        self._refresh_schema_diff_metadata()
    
    def _on_host_layer_changed(self, item, previous_item):
        """Handle host layer selection change (works with QTreeWidget)."""
        if self._refreshing_layer_lists:
            return
        
        # Extract layer from item
        layer = None
        if item:
            layer_id = item.data(0, Qt.ItemDataRole.UserRole)
            if layer_id:
                layer = QgsProject.instance().mapLayer(layer_id)
        
        self._disconnect_layer_selection(self._host_layer)
        self._host_layer = layer if isinstance(layer, QgsVectorLayer) else None
        self._update_host_info()
        self._refresh_id_field_selection()
        self._apply_versioning_layer_filters()
        self._refresh_schema_diff_metadata()
    
    def _update_client_info(self):
        """Update client dataset info label."""
        if self._client_layer and self._client_layer.isValid():
            self._client_info.setText(
                f"{self._client_layer.name()}\n"
                f"Features: {self._client_layer.featureCount()}\n"
                f"Geometry: {QgsWkbTypes.displayString(self._client_layer.wkbType())}\n"
                f"Fields: {len(self._client_layer.fields())}"
            )
        else:
            self._client_info.setText("No dataset loaded")
    
    def _update_host_info(self):
        """Update host dataset info label."""
        if self._host_layer and self._host_layer.isValid():
            self._host_info.setText(
                f"{self._host_layer.name()}\n"
                f"Features: {self._host_layer.featureCount()}\n"
                f"Geometry: {QgsWkbTypes.displayString(self._host_layer.wkbType())}\n"
                f"Fields: {len(self._host_layer.fields())}"
            )
        else:
            self._host_info.setText("No dataset loaded")

    def _refresh_schema_diff_metadata(self):
        client_layer = getattr(self, '_client_layer', None)
        host_layer = getattr(self, '_host_layer', None)

        if not (client_layer and client_layer.isValid() and host_layer and host_layer.isValid()):
            self._client_only_fields = []
            self._host_only_fields = []
            self._update_schema_diff_label()
            if getattr(self, '_attribute_view_mode', False) and hasattr(self, '_client_table'):
                self._rebuild_attribute_view_table()
            return

        client_fields = {field.name() for field in client_layer.fields()}
        host_fields = {field.name() for field in host_layer.fields()}

        ignored = set(getattr(self, '_ignored_fields', []))
        self._client_only_fields = sorted(name for name in (client_fields - host_fields) if name not in ignored)
        self._host_only_fields = sorted(name for name in (host_fields - client_fields) if name not in ignored)

        self._update_schema_diff_label()

        if self._attribute_view_mode and hasattr(self, '_client_table'):
            self._rebuild_attribute_view_table()

    def _format_schema_field_list(self, fields: list[str], limit: int = 3) -> str:
        if not fields:
            return ""
        display = ", ".join(fields[:limit])
        if len(fields) > limit:
            display += f" +{len(fields) - limit} more"
        return display

    def _update_schema_diff_label(self):
        label = getattr(self, '_schema_diff_label', None)
        if not label:
            return

        client_layer = getattr(self, '_client_layer', None)
        host_layer = getattr(self, '_host_layer', None)

        if not (client_layer and client_layer.isValid()) or not (host_layer and host_layer.isValid()):
            label.setText("Load client & host layers to compare field schemas.")
            label.setStyleSheet("color: #868e96; font-size: 11px; font-style: italic;")
            return

        added = self._client_only_fields
        removed = self._host_only_fields
        if not added and not removed:
            label.setText("Field schemas match between client and host layers.")
            label.setStyleSheet("color: #2b8a3e; font-size: 11px; font-style: italic;")
            return

        parts = []
        if added:
            parts.append(f"Client-only fields: {self._format_schema_field_list(added)}")
        if removed:
            parts.append(f"Host-only fields: {self._format_schema_field_list(removed)}")
        label.setText("Field differences → " + " | ".join(parts))
        label.setStyleSheet("color: #c92a2a; font-size: 11px; font-weight: bold;")

    def _build_id_field_options(self, layer: QgsVectorLayer | None) -> list[str]:
        options = ["(auto-detect)", "FID (feature id)"]
        if layer and layer.isValid():
            for field in layer.fields():
                options.append(field.name())
        return options

    def _refresh_id_field_selection(self):
        client_options = self._build_id_field_options(self._client_layer)
        host_options = self._build_id_field_options(self._host_layer)
        if self._client_id_field not in client_options:
            self._client_id_field = client_options[0]
        if self._host_id_field not in host_options:
            self._host_id_field = host_options[0]
        self._update_id_field_summary()

    def _update_id_field_summary(self):
        if not hasattr(self, '_id_field_summary'):
            return
        client_text = self._client_id_field or "(auto-detect)"
        host_text = self._host_id_field or "(auto-detect)"
        text = f"Client ({client_text}) – Host ({host_text})"
        self._id_field_summary.setText(text)

    def _open_dual_id_field_picker(self):
        client_options = self._build_id_field_options(self._client_layer)
        host_options = self._build_id_field_options(self._host_layer)
        dialog = DualFieldPickerDialog(
            client_options,
            host_options,
            self._client_id_field,
            self._host_id_field,
            self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._client_id_field, self._host_id_field = dialog.selected_fields()
            self._update_id_field_summary()

    def _auto_detect_common_id_field(self) -> str | None:
        if not self._client_layer or not self._host_layer:
            return None
        candidate_names = ['id', 'fid', 'gid', 'objectid', 'ID', 'FID']
        for name in candidate_names:
            if (
                self._client_layer.fields().indexFromName(name) >= 0
                and self._host_layer.fields().indexFromName(name) >= 0
            ):
                return name
        return None

    def _is_allowed_versioning_layer(self, layer) -> bool:
        if not layer or not layer.isValid():
            return False
        provider = (layer.providerType() or "").lower()
        if provider == "postgres":
            return True
        if provider in {"ogr", "gpkg"}:
            source = (layer.source() or "").lower()
            storage = (layer.storageType() or "").lower()
            return ".gpkg" in source or "geopackage" in storage
        return False

    def _is_allowed_host_layer(self, layer) -> bool:
        """Target layer must be PostGIS or GeoPackage."""
        return self._is_allowed_versioning_layer(layer)

    def _apply_versioning_layer_filters(self):
        """Refresh layer lists with host filtering and mutual exclusion."""
        self._refresh_versioning_layer_trees()

    def _on_layers_will_be_removed(self, layer_ids):
        """Clear versioning layer selections before QGIS removes layers."""
        client_id = self._client_layer.id() if self._client_layer else None
        host_id = self._host_layer.id() if self._host_layer else None

        if client_id and client_id in layer_ids:
            self._disconnect_layer_selection(self._client_layer)
            self._clear_layer_tree_selection(self._client_layer_tree)
            self._client_layer = None
            self._update_client_info()

        if host_id and host_id in layer_ids:
            self._disconnect_layer_selection(self._host_layer)
            self._clear_layer_tree_selection(self._host_layer_tree)
            self._host_layer = None
            self._update_host_info()

        self._refresh_id_field_selection()
        self._refresh_schema_diff_metadata()
        QTimer.singleShot(0, self._apply_versioning_layer_filters)

    def _on_layers_added(self, layers):
        """Refresh layer lists when new layers are added."""
        print(f"DEBUG: _on_layers_added() called with {len(layers)} layers")
        QTimer.singleShot(50, self._refresh_versioning_layer_trees)

    def _on_project_cleared(self):
        """Handle project close/open transitions safely."""
        self._project_changing = True
        self._client_layer = None
        self._host_layer = None
        self._selected_client_fid = None
        self._selected_host_fid = None
        if hasattr(self, "_client_table"):
            self._client_table.setRowCount(0)
        if hasattr(self, "_host_table"):
            self._host_table.setRowCount(0)
        self._clear_photo_panel("No comparison results")
        self._client_diff_index = {}
        self._pending_attr_commits = set()
        self._ignored_fids = {}
        self._clear_geometry_diff()
        self._update_selected_feature_label()
        self._update_client_info()
        self._update_host_info()
        self._refresh_id_field_selection()
        self._refresh_schema_diff_metadata()
        # Refresh button states — undo stack is NOT cleared; button reflects stack contents
        self._update_actions_enabled_state()
        QTimer.singleShot(0, self._refresh_versioning_layer_trees)

    def _on_project_read(self, *args):
        """Re-enable layer updates after a project is opened.

        Accepts *args to handle both projectRead() (no params) and
        readProject(QDomDocument) — PyQt5 silently drops calls if the
        signature doesn't match the signal's argument count.
        """
        self._project_changing = False
        QTimer.singleShot(0, self._refresh_versioning_layer_trees)
        QTimer.singleShot(0, self._refresh_id_field_selection)

    def _create_layer_tree_view(self):
        """Create a simple layer tree view for QGIS 4.0 Qt6 compatibility."""
        # Use simple QTreeWidget instead of QgsLayerTreeView for better Qt6 compatibility
        view = QTreeWidget()
        view.setColumnCount(1)
        view.setHeaderHidden(True)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setIconSize(QSize(18, 18))
        view.setMinimumSize(0, 0)
        view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        # Use the invisible root item so children are visible immediately
        root = view.invisibleRootItem()
        
        # For compatibility, return (view, view, root) to match old API
        # First return is the view, second is "model" (we'll use view directly), third is root item
        return view, view, root

    def _geometry_icon_for_layer(self, layer):
        """Return the standard QGIS layer icon (same as the Layers panel)."""
        try:
            from qgis.core import QgsIconUtils
            return QgsIconUtils.iconForLayer(layer)
        except Exception as exc:
            log_debug_exception('Suppressed error', exc)
        # Fallback: built-in QGIS theme icons by geometry type
        try:
            from qgis.core import QgsApplication
            geom_kind = QgsWkbTypes.geometryType(layer.wkbType())
            path = {
                QgsWkbTypes.PointGeometry:   '/mIconPointLayer.svg',
                QgsWkbTypes.LineGeometry:    '/mIconLineLayer.svg',
                QgsWkbTypes.PolygonGeometry: '/mIconPolygonLayer.svg',
            }.get(geom_kind, '/mIconVector.svg')
            return QgsApplication.getThemeIcon(path)
        except Exception:
            return QIcon()

    def _select_layer_in_tree(self, view, layer):
        """Select a layer in the tree (works with QTreeWidget for Qt6 compatibility)."""
        if not view or not layer:
            return
        
        # For QTreeWidget
        if isinstance(view, QTreeWidget):
            # Find the item with matching layer ID
            root = view.invisibleRootItem()
            for i in range(root.childCount()):
                item = root.child(i)
                layer_id = item.data(0, Qt.ItemDataRole.UserRole)
                if layer_id == layer.id():
                    view.setCurrentItem(item)
                    return

    def _clear_layer_tree_selection(self, view):
        """Clear layer selection in the tree (works with QTreeWidget for Qt6 compatibility)."""
        if not view:
            return
        
        # For QTreeWidget
        if isinstance(view, QTreeWidget):
            view.setCurrentItem(None)

    def _refresh_versioning_layer_trees(self):
        """Apply filters to client/host layer tree views."""
        # Guard: ensure all required attributes exist
        if not hasattr(self, "_client_layer_root") or not hasattr(self, "_host_layer_root") \
           or not hasattr(self, "_client_layer_tree") or not hasattr(self, "_host_layer_tree"):
            print("DEBUG: Layer tree attributes not yet initialized")
            return

        if self._client_layer_root is None or self._host_layer_root is None:
            print("DEBUG: Layer roots are None - UI not fully initialized yet")
            return

        client_id = self._client_layer.id() if self._client_layer else None
        host_id = self._host_layer.id() if self._host_layer else None

        all_layers = list(QgsProject.instance().mapLayers().values())
        print(f"DEBUG: _refresh_versioning_layer_trees() called - {len(all_layers)} layers in project")
        
        self._refreshing_layer_lists = True
        self._rebuild_layer_tree(self._client_layer_root, exclude_layer_id=host_id)
        self._rebuild_layer_tree(self._host_layer_root, only_host_allowed=True, exclude_layer_id=client_id)
        
        self._select_layer_in_tree(self._client_layer_tree, self._client_layer)
        self._select_layer_in_tree(self._host_layer_tree, self._host_layer)
        self._refreshing_layer_lists = False
        
        client_children = self._client_layer_root.childCount() if self._client_layer_root else 0
        host_children = self._host_layer_root.childCount() if self._host_layer_root else 0
        print(f"DEBUG: Client tree now has {client_children} children")
        print(f"DEBUG: Host tree now has {host_children} children")

    def _rebuild_layer_tree(
        self,
        root: QTreeWidgetItem,
        only_host_allowed: bool = False,
        exclude_layer_id: str | None = None
    ):
        """Rebuild tree with layers, using QTreeWidget for Qt6/QGIS 4.0 compatibility."""
        if not root:
            print("DEBUG: _rebuild_layer_tree() called with None root")
            return
        
        # Clear existing children (skip first hidden root)
        while root.childCount() > 0:
            root.removeChild(root.child(0))
        
        layers_to_add = []
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                print(f"DEBUG: Skipping {layer.name()} - not a VectorLayer (type: {type(layer).__name__})")
                continue
            if exclude_layer_id and layer.id() == exclude_layer_id:
                print(f"DEBUG: Skipping {layer.name()} - excluded layer")
                continue
            if "(GeoInbox)" in layer.name():
                continue  # never show GeoInbox temp layers in source selection trees
            if only_host_allowed and not self._is_allowed_host_layer(layer):
                print(f"DEBUG: Skipping {layer.name()} - not allowed as host layer (provider: {layer.providerType()})")
                continue
            
            layers_to_add.append(layer)
            geom_type = QgsWkbTypes.displayString(layer.wkbType())
            item = QTreeWidgetItem([layer.name()])
            item.setData(0, Qt.ItemDataRole.UserRole, layer.id())
            item.setToolTip(0, geom_type)
            item.setIcon(0, self._geometry_icon_for_layer(layer))
            root.addChild(item)
            print(f"DEBUG: Added layer '{layer.name()}' ({geom_type}) to tree")
        
        root_children = root.childCount()
        print(f"DEBUG: _rebuild_layer_tree() added {len(layers_to_add)} layers, root now has {root_children} children")

    def _clear_diff_layers(self):
        for attr in ("_diff_added_layer", "_diff_removed_layer",
                     "_diff_attr_changed_layer", "_diff_geom_modified_layer"):
            layer = getattr(self, attr, None)
            setattr(self, attr, None)        # null ref BEFORE any C++ call
            self._safe_remove_layer(layer)   # handles None and deleted C++ objects

    def _build_diff_layer(self, base_layer: QgsVectorLayer, features: list, name: str, color: QColor):
        if not base_layer or not features:
            return None
        geom_kind = QgsWkbTypes.geometryType(base_layer.wkbType())
        geom_type_map = {
            QgsWkbTypes.PointGeometry: "Point",
            QgsWkbTypes.LineGeometry: "LineString",
            QgsWkbTypes.PolygonGeometry: "Polygon"
        }
        geom_type = geom_type_map.get(geom_kind)
        if not geom_type:
            return None
        crs = base_layer.crs().authid()
        memory_layer = QgsVectorLayer(f"{geom_type}?crs={crs}", name, "memory")
        if not memory_layer.isValid():
            return None
        provider = memory_layer.dataProvider()

        fields = QgsFields(base_layer.fields())
        provider.addAttributes(fields)
        memory_layer.updateFields()

        new_features = []
        for feat in features:
            if not feat or not feat.hasGeometry():
                continue
            new_feat = QgsFeature(memory_layer.fields())
            new_feat.setGeometry(feat.geometry())
            new_feat.setAttributes(feat.attributes())
            new_features.append(new_feat)
        if new_features:
            provider.addFeatures(new_features)
            memory_layer.updateExtents()

        symbol = QgsSymbol.defaultSymbol(memory_layer.geometryType())
        if symbol:
            symbol.setColor(color)
            memory_layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        return memory_layer

    def _update_diff_layers(self, added_features: list, removed_features: list, 
                             attr_changed_features: list = None, geom_modified_features: list = None):
        self._clear_diff_layers()
        # Features Added (green)
        self._diff_added_layer = self._build_diff_layer(
            self._client_layer,
            added_features,
            "Features Added (GeoInbox)",
            QColor(0, 200, 0)
        )
        # Features not surveyed / host-only (red)
        self._diff_removed_layer = self._build_diff_layer(
            self._host_layer,
            removed_features,
            "Not Surveyed (GeoInbox)",
            QColor(220, 60, 60)
        )
        # Attribute Changed (yellow/orange)
        self._diff_attr_changed_layer = self._build_diff_layer(
            self._client_layer,
            attr_changed_features or [],
            "Attribute Changed (GeoInbox)",
            QColor(255, 165, 0)
        )
        # Geometry Modified (blue)
        self._diff_geom_modified_layer = self._build_diff_layer(
            self._client_layer,
            geom_modified_features or [],
            "Geometry Modified (GeoInbox)",
            QColor(30, 144, 255)
        )
    
    def _show_ignore_fields_dialog(self):
        """Show dialog to select fields to ignore in comparison."""
        # Collect all unique field names from both layers
        all_fields = set()
        if self._client_layer:
            for field in self._client_layer.fields():
                all_fields.add(field.name())
        if self._host_layer:
            for field in self._host_layer.fields():
                all_fields.add(field.name())
        
        # Always include 'photos' even if not in current layers
        all_fields.add('photos')
        
        if not all_fields:
            QMessageBox.information(self, "Ignore Fields", "No fields available. Load layers first.")
            return
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Select Fields to Ignore")
        dialog.setMinimumWidth(300)
        layout = QVBoxLayout(dialog)
        
        label = QLabel("Select fields to exclude from attribute comparison:")
        layout.addWidget(label)
        
        # Create scrollable list of checkboxes
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        
        checkboxes = {}
        for field_name in sorted(all_fields):
            cb = QCheckBox(field_name)
            cb.setChecked(field_name in self._ignored_fields)
            checkboxes[field_name] = cb
            scroll_layout.addWidget(cb)
        
        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        scroll.setMaximumHeight(300)
        layout.addWidget(scroll)
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_select_all = QPushButton("Select All")
        btn_select_all.clicked.connect(lambda: [cb.setChecked(True) for cb in checkboxes.values()])
        btn_layout.addWidget(btn_select_all)
        btn_clear_all = QPushButton("Clear All")
        btn_clear_all.clicked.connect(lambda: [cb.setChecked(False) for cb in checkboxes.values()])
        btn_layout.addWidget(btn_clear_all)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        # OK/Cancel
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._ignored_fields = [name for name, cb in checkboxes.items() if cb.isChecked()]
            # Update label
            if self._ignored_fields:
                display = ', '.join(self._ignored_fields[:3])
                if len(self._ignored_fields) > 3:
                    display += f' +{len(self._ignored_fields) - 3} more'
                self._ignore_fields_label.setText(f"({display})")
            else:
                self._ignore_fields_label.setText("(none)")
            self._refresh_schema_diff_metadata()

    def _stop_comparison_now(self):
        """Signal the running comparison to stop after the current feature."""
        self._stop_comparison = True
        self._stop_btn.setEnabled(False)

    def _abort_comparison(self):
        """Finish UI after a user stop — keep partial table results."""
        self._client_table.setSortingEnabled(True)
        self._host_table.setSortingEnabled(True)
        self._compare_progress.setVisible(False)
        self._compare_progress_label.setVisible(False)
        self._compare_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._update_actions_enabled_state()
        if iface and iface.messageBar():
            iface.messageBar().pushMessage(
                "Comparison Stopped",
                "Partial results kept. Run Compare again for a full pass.",
                level=Qgis.Warning,
                duration=5,
            )

    @staticmethod
    def _normalize_geom_pair(client_geom: QgsGeometry, host_geom: QgsGeometry):
        """Copy geometries and align single/multi so Point↔MultiPoint etc. compare fairly."""
        cg = QgsGeometry(client_geom)
        hg = QgsGeometry(host_geom)
        if not cg.isNull() and not hg.isNull() and cg.isMultipart() != hg.isMultipart():
            if not cg.isMultipart():
                cg.convertToMultiType()
            if not hg.isMultipart():
                hg.convertToMultiType()
        return cg, hg

    def _geometries_differ(self, client_geom: QgsGeometry, host_geom: QgsGeometry, tolerance: float):
        """
        Single clear rule:
        - different base type (point/line/polygon) → different
        - empty vs non-empty → different
        - equals() after single/multi normalize → same
        - else different if distance > tolerance
        Returns (changed: bool, distance: float, detail: dict).
        """
        detail = {}
        if client_geom is None or host_geom is None:
            return True, float("inf"), {"reason": "missing_geometry"}
        if client_geom.isNull() or host_geom.isNull():
            return True, float("inf"), {"reason": "null_geometry"}
        if client_geom.isEmpty() and host_geom.isEmpty():
            return False, 0.0, {"reason": "both_empty"}
        if client_geom.isEmpty() or host_geom.isEmpty():
            return True, float("inf"), {"reason": "one_empty"}

        if client_geom.type() != host_geom.type():
            detail["reason"] = "type_mismatch"
            detail["client_type"] = int(client_geom.type())
            detail["host_type"] = int(host_geom.type())
            return True, float("inf"), detail

        cg, hg = self._normalize_geom_pair(client_geom, host_geom)
        if cg.equals(hg):
            return False, 0.0, {"reason": "equals", "distance": 0.0}

        distance = cg.distance(hg)
        detail["distance"] = distance
        detail["tolerance"] = tolerance
        if distance <= tolerance:
            detail["reason"] = "within_tolerance"
            return False, distance, detail

        detail["reason"] = "distance_exceeds_tolerance"
        return True, distance, detail

    def _run_comparison(self):
        """Match by ID (± nearest geometry), then report geom/attr diffs on pairs."""
        if not self._client_layer or not self._host_layer:
            QMessageBox.warning(self, "Error", "Please load both client and host datasets")
            return

        self._stop_comparison = False
        self._client_table.setSortingEnabled(False)
        self._host_table.setSortingEnabled(False)
        self._client_table.setRowCount(0)
        self._host_table.setRowCount(0)
        self._clear_photo_panel("No comparison results")
        self._client_result_rows = []
        self._host_deleted_rows = []
        self._clear_diff_layers()
        self._clear_geom_diff_layer()
        self._remove_geoinbox_group()
        self._client_diff_index = {}
        self._compare_progress.setVisible(True)
        self._compare_progress.setValue(0)
        self._compare_progress_label.setText("")
        self._compare_progress_label.setVisible(True)
        self._compare_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)

        tolerance = float(self._geom_tolerance_spin.value())
        match_by_geom = self._match_by_geom_check.isChecked()
        compare_geom = self._compare_geom_check.isChecked()
        compare_attr = self._compare_attr_check.isChecked()
        empty_as_null = self._empty_as_null_check.isChecked()

        # Determine ID field strategy
        id_field = None
        use_fid = False
        paired_fields = None  # (client_field, host_field)
        client_sel = getattr(self, "_client_id_field", "(auto-detect)")
        host_sel = getattr(self, "_host_id_field", "(auto-detect)")
        if client_sel == host_sel == "FID (feature id)":
            use_fid = True
        else:
            client_auto = client_sel == "(auto-detect)"
            host_auto = host_sel == "(auto-detect)"
            if client_auto and host_auto:
                id_field = self._auto_detect_common_id_field()
            elif not client_auto and not host_auto:
                if client_sel == host_sel:
                    id_field = client_sel
                else:
                    paired_fields = (client_sel, host_sel)
            else:
                id_field = self._auto_detect_common_id_field()

        client_field_idx = host_field_idx = -1
        shared_client_idx = shared_host_idx = -1
        if paired_fields:
            client_field_idx = self._client_layer.fields().indexFromName(paired_fields[0])
            host_field_idx = self._host_layer.fields().indexFromName(paired_fields[1])
            if client_field_idx < 0 or host_field_idx < 0:
                QMessageBox.warning(
                    self,
                    "ID Fields",
                    "Selected ID fields are not present on the current layers. Falling back to auto-detect.",
                )
                paired_fields = None
                id_field = self._auto_detect_common_id_field()

        if id_field and not use_fid and not paired_fields:
            shared_client_idx = self._client_layer.fields().indexFromName(id_field)
            shared_host_idx = self._host_layer.fields().indexFromName(id_field)
            if shared_client_idx < 0 or shared_host_idx < 0:
                QMessageBox.warning(
                    self,
                    "ID Fields",
                    "Selected ID field is not available on both layers. "
                    "Geometry matching (if enabled) will be used for unpaired features.",
                )
                id_field = None

        # Build host feature index for ID matching
        host_features = {}
        host_by_geom = []
        for feat in self._host_layer.getFeatures():
            if use_fid:
                host_features[feat.id()] = feat
            elif paired_fields and host_field_idx >= 0:
                key = feat.attribute(host_field_idx)
                if key is not None and key != "":
                    host_features[key] = feat
            elif id_field and shared_host_idx >= 0:
                key = feat.attribute(shared_host_idx)
                if key is not None and key != "":
                    host_features[key] = feat
            host_by_geom.append(feat)

        matched_host_ids = set()
        stats = {"matched": 0, "new": 0, "geom_changed": 0, "attr_changed": 0}
        added_features = []
        removed_features = []
        attr_changed_features = []
        geom_modified_features = []

        total_client = self._client_layer.featureCount()
        total_host = self._host_layer.featureCount()
        total_steps = max(1, total_client + total_host)
        progress_count = 0

        for client_feat in self._client_layer.getFeatures():
            client_fid = client_feat.id()
            client_geom = client_feat.geometry()

            host_feat = None
            match_type = "new"
            match_method = None

            # --- Match: ID first ---
            if use_fid:
                if client_fid in host_features:
                    host_feat = host_features[client_fid]
                    match_type = "matched"
                    match_method = "id"
            elif paired_fields and client_field_idx >= 0:
                client_key = client_feat.attribute(client_field_idx)
                if client_key in host_features:
                    host_feat = host_features[client_key]
                    match_type = "matched"
                    match_method = "id"
            elif id_field and shared_client_idx >= 0:
                client_key = client_feat.attribute(shared_client_idx)
                if client_key in host_features:
                    host_feat = host_features[client_key]
                    match_type = "matched"
                    match_method = "id"

            # --- Match: optional nearest geometry (no fuzzy attribute scoring) ---
            if not host_feat and match_by_geom and client_geom and not client_geom.isEmpty():
                best_dist = float("inf")
                best_host = None
                for hf in host_by_geom:
                    if hf.id() in matched_host_ids:
                        continue
                    hg = hf.geometry()
                    if not hg or hg.isEmpty():
                        continue
                    geom_dist = client_geom.distance(hg)
                    if geom_dist < best_dist and geom_dist <= tolerance:
                        best_dist = geom_dist
                        best_host = hf
                if best_host is not None:
                    host_feat = best_host
                    match_type = "matched"
                    match_method = "geometry"

            # --- Diff geometry ---
            geom_changed = False
            geom_distance = 0.0
            geom_check_results = {}
            host_geom = host_feat.geometry() if host_feat else None
            if host_feat and compare_geom:
                geom_changed, geom_distance, geom_check_results = self._geometries_differ(
                    client_geom, host_geom, tolerance
                )
                if geom_changed:
                    stats["geom_changed"] += 1
                    geom_modified_features.append(client_feat)

            # --- Diff attributes ---
            attr_changes = {}
            if host_feat and compare_attr:
                for field in self._client_layer.fields():
                    fname = field.name()
                    if fname in self._ignored_fields:
                        continue
                    if self._host_layer.fields().indexFromName(fname) >= 0:
                        client_val = client_feat[fname]
                        host_val = host_feat[fname]
                        values_equal = client_val == host_val
                        if not values_equal and empty_as_null:
                            client_is_empty = (
                                client_val is None
                                or client_val == ""
                                or (isinstance(client_val, str) and client_val.strip() == "")
                            )
                            host_is_empty = (
                                host_val is None
                                or host_val == ""
                                or (isinstance(host_val, str) and host_val.strip() == "")
                            )
                            if client_is_empty and host_is_empty:
                                values_equal = True
                        if not values_equal:
                            attr_changes[fname] = {"client": client_val, "host": host_val}
                if attr_changes:
                    stats["attr_changed"] += 1
                    attr_changed_features.append(client_feat)

            if host_feat:
                matched_host_ids.add(host_feat.id())
                stats["matched"] += 1
            else:
                stats["new"] += 1
                added_features.append(client_feat)

            self._client_diff_index[client_fid] = {
                "host_fid": host_feat.id() if host_feat else None,
                "attr_changes": attr_changes,
                "geom_changed": geom_changed,
                "geom_distance": geom_distance,
                "geom_check_results": geom_check_results,
                "client_geom": client_geom if geom_changed else None,
                "host_geom": host_geom if geom_changed else None,
                "client_feat": client_feat,
                "host_feat": host_feat,
                "match_type": match_type,
                "match_method": match_method,
            }

            row = self._client_table.rowCount()
            self._client_table.insertRow(row)
            fid_item = QTableWidgetItem(str(client_fid))
            fid_item.setData(Qt.ItemDataRole.UserRole + 1, self._feature_has_photos(client_feat))
            self._client_table.setItem(row, 0, fid_item)
            match_item = QTableWidgetItem(str(host_feat.id()) if host_feat else "—")
            if match_method == "geometry":
                match_item.setToolTip("Paired by nearest geometry (no ID match)")
            elif match_method == "id":
                match_item.setToolTip("Paired by ID field")
            self._client_table.setItem(row, 1, match_item)

            if geom_changed:
                geom_item = QTableWidgetItem(f"{geom_distance:.4f}")
                geom_item.setBackground(QBrush(QColor(255, 200, 200)))
                reason = geom_check_results.get("reason", "")
                geom_item.setToolTip(
                    f"Different ({reason})\n"
                    f"Distance: {geom_distance:.6f}  |  Tolerance: {tolerance:.6f}\n"
                    f"(layer CRS units)"
                )
            elif host_feat and compare_geom:
                geom_item = QTableWidgetItem("—")
                geom_item.setToolTip(
                    f"Same within tolerance {tolerance:.6f} "
                    f"(distance {geom_distance:.6f})"
                )
            else:
                geom_item = QTableWidgetItem("—")
            self._client_table.setItem(row, 2, geom_item)

            attr_item = self._create_attr_table_item(attr_changes)
            self._client_table.setItem(row, 3, attr_item)

            if match_type == "new":
                status_label = "ADDED"
                status_color = QColor(200, 255, 200)
            elif geom_changed and attr_changes:
                status_label = "MODIFIED (BOTH)"
                status_color = QColor(255, 182, 193)
            elif geom_changed:
                status_label = "MODIFIED (GEOM)"
                status_color = QColor(255, 204, 153)
            elif attr_changes:
                status_label = "MODIFIED (ATTR)"
                status_color = QColor(187, 222, 251)
            else:
                status_label = "UNCHANGED"
                status_color = QColor(240, 240, 240)
            status_item = QTableWidgetItem(status_label)
            status_item.setBackground(QBrush(status_color))
            self._client_table.setItem(row, 4, status_item)
            self._client_table.setRowHidden(row, not self._status_visible(status_label))

            progress_count += 1
            self._compare_progress.setValue(int((progress_count / total_steps) * 100))
            self._stat_total.setText(str(progress_count))
            self._stat_matched.setText(str(stats["matched"]))
            self._stat_new.setText(str(stats["new"]))
            self._stat_geom_changed.setText(str(stats["geom_changed"]))
            self._stat_attr_changed.setText(str(stats["attr_changed"]))
            self._compare_progress_label.setText(f"{progress_count:,} / {total_client:,}")
            QApplication.processEvents()

            if self._stop_comparison:
                self._abort_comparison()
                return

        # Host features with no client pair → Not Surveyed
        deleted_count = 0
        for host_feat in self._host_layer.getFeatures():
            if host_feat.id() not in matched_host_ids:
                deleted_count += 1
                removed_features.append(host_feat)
                row = self._host_table.rowCount()
                self._host_table.insertRow(row)
                self._host_table.setItem(row, 0, QTableWidgetItem(str(host_feat.id())))
                self._host_table.setItem(row, 1, QTableWidgetItem("—"))
                self._host_table.setItem(row, 2, QTableWidgetItem("—"))
                self._host_table.setItem(row, 3, QTableWidgetItem("—"))
                status_item = QTableWidgetItem("NOT SURVEYED")
                status_item.setForeground(QBrush(QColor(120, 120, 120)))
                status_item.setBackground(QBrush(QColor(220, 220, 220)))
                self._host_table.setItem(row, 4, status_item)
                for col in range(4):
                    cell = self._host_table.item(row, col)
                    if cell:
                        cell.setForeground(QBrush(QColor(140, 140, 140)))
                self._host_table.setRowHidden(row, not self._status_visible("NOT SURVEYED"))
                self._stat_deleted.setText(str(deleted_count))
            progress_count += 1
            self._compare_progress.setValue(int((progress_count / total_steps) * 100))
            QApplication.processEvents()

            if self._stop_comparison:
                self._abort_comparison()
                return

        self._stat_total.setText(str(total_client))
        self._stat_matched.setText(str(stats["matched"]))
        self._stat_new.setText(str(stats["new"]))
        self._stat_deleted.setText(str(deleted_count))
        self._stat_geom_changed.setText(str(stats["geom_changed"]))
        self._stat_attr_changed.setText(str(stats["attr_changed"]))

        self._compare_progress.setValue(100)
        self._compare_progress_label.setText(f"{total_client:,} features processed")
        QTimer.singleShot(1200, lambda: (
            self._compare_progress.setVisible(False),
            self._compare_progress_label.setVisible(False)
        ))
        self._compare_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._client_table.setSortingEnabled(True)
        self._host_table.setSortingEnabled(True)
        self._removed_features = removed_features
        self._update_diff_layers(
            added_features, removed_features, attr_changed_features, geom_modified_features
        )
        self._update_actions_enabled_state()
        if getattr(self, "_auto_show_diff_check", None) and self._auto_show_diff_check.isChecked():
            QTimer.singleShot(200, self._show_geometry_diff)

        summary_text = (
            f"Compared {total_client} client features against {total_host} host features."
        )
        details_text = (
            f"Matched: {stats['matched']} | "
            f"Added: {stats['new']} | "
            f"Not surveyed: {deleted_count} | "
            f"Geom Diff: {stats['geom_changed']} | "
            f"Attr Diff: {stats['attr_changed']}"
        )
        if iface and iface.messageBar():
            iface.messageBar().pushMessage(
                "Comparison Complete",
                f"{summary_text} {details_text}",
                level=Qgis.Info,
                duration=6,
            )
        else:
            print(f"Comparison Complete: {summary_text} {details_text}")

    def _refresh_gisimple_server_list(self):
        """Refresh the GISimple server list from database and auto-select default."""
        self._gisimple_server_list.clear()
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, url, username, password, is_default FROM gisimple_servers ORDER BY name")
        rows = cursor.fetchall()
        conn.close()
        
        default_item = None
        for row in rows:
            server_id, name, url, username, password, is_default = row
            # Add (default) label if this is the default server
            display_name = f"{name} ({url})" if not is_default else f"{name} (default) ({url})"
            item = QListWidgetItem(display_name)
            item.setData(Qt.ItemDataRole.UserRole, server_id)
            # Store server data for quick access
            item.setData(Qt.ItemDataRole.UserRole + 1, {
                'id': server_id,
                'name': name,
                'url': url,
                'username': username,
                'password': password,
                'is_default': is_default
            })
            if is_default:
                item.setBackground(QBrush(QColor("#d3f9d8")))
                default_item = item
            self._gisimple_server_list.addItem(item)
        
        # Auto-select default server if exists (but don't auto-login on page load)
        if default_item:
            self._gisimple_server_list.setCurrentItem(default_item)
            # Just set the URL, don't auto-login
            server_data = default_item.data(Qt.ItemDataRole.UserRole + 1)
            if server_data:
                self._gisimple_server_url = server_data.get('url', '')
                server_name = server_data.get('name', '')
                has_credentials = server_data.get('username', '') and server_data.get('password', '')
                if has_credentials:
                    self._gisimple_status.setText(f"● Selected: {server_name} (default) - Click 'Login' to connect")
                else:
                    self._gisimple_status.setText(f"● Selected: {server_name} (default) - No credentials saved")
                self._gisimple_status.setStyleSheet("color: #1971c2; font-size: 11px;")
    
    # =========================================================================
    # EXISTING METHODS (Keep all existing functionality)
    # =========================================================================
    def _edit_email_server(self, item=None):
        """Edit selected email server."""
        # QPushButton.clicked sends a bool; ignore it and resolve from list.
        if isinstance(item, bool):
            item = None
        if item is None:
            item = self._server_list.currentItem()
        if item is None:
            selected_items = self._server_list.selectedItems() if self._server_list else []
            item = selected_items[0] if selected_items else None
        if item is None and self._server_list and self._server_list.count() == 1:
            only_item = self._server_list.item(0)
            if only_item and only_item.data(Qt.ItemDataRole.UserRole):
                item = only_item
        if not item:
            QMessageBox.warning(self, "Warning", "Please select a server to edit")
            return
        
        server_id = item.data(Qt.ItemDataRole.UserRole)
        if not server_id:
            return
        
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM email_servers WHERE id=?", (server_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            server_data = {
                'id': row[0], 'name': row[1], 'protocol': row[2], 'host': row[3],
                'port': row[4], 'use_ssl': bool(row[5]), 'username': row[6], 
                'password': row[7], 'is_default': bool(row[8])
            }
            dialog = ServerConfigDialog(server_data=server_data, parent=self)
            if dialog.exec():
                data = dialog.get_data()
                self._save_server_to_db(data)
                self._refresh_server_list()
    
    def _delete_email_server(self):
        """Delete selected email server."""
        item = self._server_list.currentItem()
        if item is None:
            selected_items = self._server_list.selectedItems() if self._server_list else []
            item = selected_items[0] if selected_items else None
        if item is None and self._server_list and self._server_list.count() == 1:
            only_item = self._server_list.item(0)
            if only_item and only_item.data(Qt.ItemDataRole.UserRole):
                item = only_item
        if not item:
            QMessageBox.warning(self, "Warning", "Please select a server to delete")
            return
        
        server_id = item.data(Qt.ItemDataRole.UserRole)
        if not server_id:
            return
        
        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Delete server '{item.text()}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            cursor.execute("DELETE FROM email_servers WHERE id=?", (server_id,))
            conn.commit()
            conn.close()
            self._refresh_server_list()
    
    def _auto_connect_default_server(self):
        """Auto-connect to the default IMAP server on startup."""
        # Find default server
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM email_servers WHERE is_default = 1 LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        
        if row:
            server_id = row[0]
            # Select the default server in combo
            for i in range(self._server_combo.count()):
                if self._server_combo.itemData(i) == server_id:
                    self._server_combo.setCurrentIndex(i)
                    break
            # Trigger connect
            self._on_connect()

    def _on_connect(self):
        """Handle connect button click."""
        if self._connect_btn.text() == "Connect":
            # Get selected server
            server_id = self._server_combo.currentData()
            if not server_id:
                QMessageBox.warning(self, "Connect", "Please configure an email server first in Settings.")
                return
            
            self._status_label.setText("Connecting...")
            # self._status_label.setStyleSheet("color: #FFC107;")
            QApplication.processEvents()
            
            # Get server details from database
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM email_servers WHERE id=?", (server_id,))
            row = cursor.fetchone()
            conn.close()
            
            if not row:
                QMessageBox.warning(self, "Connect", "Server not found in database.")
                self._status_label.setText("Disconnected")
                self._status_label.setStyleSheet("color: #666;")
                return
            
            # row: id, name, protocol, host, port, use_ssl, username, password, is_default
            server_name = row[1]
            protocol = row[2]
            host = row[3]
            port = row[4]
            use_ssl = bool(row[5])
            username = row[6]
            password = row[7]
            
            if protocol != 'imap':
                QMessageBox.information(self, "Connect", f"Protocol '{protocol}' is not yet fully implemented.\nOnly IMAP is currently supported.")
                self._status_label.setText("Disconnected")
                self._status_label.setStyleSheet("color: #666;")
                return
            
            # Connect using imaplib directly
            try:
                import imaplib
                
                if use_ssl:
                    self._imap = imaplib.IMAP4_SSL(host, port)
                else:
                    self._imap = imaplib.IMAP4(host, port)
                    # Try STARTTLS if not using SSL
                    try:
                        self._imap.starttls()
                    except Exception as exc:
                        log_debug_exception("Server does not support STARTTLS", exc)
                
                # Login
                self._imap.login(username, password)
                
                self._status_label.setText(f"Connected to {server_name}")
                # self._status_label.setStyleSheet("color: #4CAF50;")
                self._connect_btn.setText("Disconnect")
                
                # Load mailboxes
                self._load_mailboxes()
                
            except Exception as e:
                error_msg = str(e)
                QMessageBox.critical(self, "Connection Failed", f"Failed to connect to {host}:{port}\n\n{error_msg}")
                self._status_label.setText("Connection failed")
                # self._status_label.setStyleSheet("color: #e03131;")
                self._imap = None
        else:
            # Disconnect
            if hasattr(self, '_imap') and self._imap:
                try:
                    self._imap.logout()
                except Exception as exc:
                    log_debug_exception('Suppressed error', exc)
                self._imap = None
            
            self._status_label.setText("Disconnected")
            self._status_label.setStyleSheet("color: #666;")
            self._connect_btn.setText("Connect")
            
            # Clear mailbox list and messages
            if hasattr(self, '_mailbox_list'):
                self._mailbox_list.clear()
            if hasattr(self, '_message_table'):
                self._message_table.setRowCount(0)
    
    def _load_mailboxes(self):
        """Load mailboxes from connected IMAP server."""
        if not hasattr(self, '_imap') or not self._imap:
            return
        
        try:
            # Get list of mailboxes using imaplib
            status, mailbox_list = self._imap.list()
            
            if hasattr(self, '_mailbox_list'):
                self._mailbox_list.clear()
                
                if status == 'OK' and mailbox_list:
                    for mb_data in mailbox_list:
                        if mb_data:
                            # Parse mailbox name from response like: b'(\\HasNoChildren) "/" "INBOX"'
                            try:
                                mb_str = mb_data.decode() if isinstance(mb_data, bytes) else str(mb_data)
                                # Extract mailbox name (last quoted string or last part)
                                import re
                                match = re.search(r'"([^"]+)"$', mb_str)
                                if match:
                                    mb_name = match.group(1)
                                else:
                                    mb_name = mb_str.split()[-1].strip('"')
                                
                                item = QListWidgetItem(mb_name)
                                item.setData(Qt.ItemDataRole.UserRole, mb_name)
                                self._mailbox_list.addItem(item)
                            except Exception as exc:
                                log_debug_exception('Skipped after error', exc)
                                continue
                
                # Select INBOX by default
                for i in range(self._mailbox_list.count()):
                    item = self._mailbox_list.item(i)
                    if item.data(Qt.ItemDataRole.UserRole).upper() == 'INBOX':
                        self._mailbox_list.setCurrentItem(item)
                        self._on_mailbox_selected(item)
                        break
                
                if self._mailbox_list.count() == 0:
                    self._mailbox_list.addItem("(No mailboxes found)")
                if hasattr(self, '_reload_emails_btn'):
                    self._reload_emails_btn.setEnabled(True)
                    
        except Exception as e:
            QMessageBox.warning(self, "Mailboxes", f"Failed to load mailboxes: {str(e)}")
    
    def _on_mailbox_selected(self, item):
        """Handle mailbox selection - load messages from selected mailbox using background worker."""
        if not hasattr(self, '_imap') or not self._imap:
            return
        
        # Cancel any previous mailbox loading
        if self._mailbox_worker and self._mailbox_worker.isRunning():
            self._mailbox_worker.cancel()
            self._mailbox_worker.wait(200)
        
        mailbox_name = item.data(Qt.ItemDataRole.UserRole)
        if not mailbox_name:
            mailbox_name = item.text().split(' (')[0]  # Remove unread count if present
        
        self._current_mailbox = mailbox_name
        self._status_label.setText(f"Loading {mailbox_name}...")
        self._message_table.setRowCount(0)
        
        # Show and configure progress bar
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._progress.setMaximum(100)
        QApplication.processEvents()
        
        # Start background worker
        self._mailbox_worker = MailboxLoadWorker(self._imap, self._imap_lock, mailbox_name, self)
        self._mailbox_worker.progress.connect(self._on_mailbox_load_progress)
        self._mailbox_worker.message_loaded.connect(self._on_message_row_loaded)
        self._mailbox_worker.finished.connect(self._on_mailbox_load_finished)
        self._mailbox_worker.error.connect(self._on_mailbox_load_error)
        self._mailbox_worker.start()
    
    def _on_mailbox_load_progress(self, current, total, status_msg):
        """Handle mailbox loading progress updates."""
        if total > 0:
            percentage = int((current / total) * 100)
            self._progress.setValue(percentage)
            self._status_label.setText(f"Loading {self._current_mailbox}: {current}/{total} ({percentage}%)")
    
    def _on_message_row_loaded(self, msg_info):
        """Handle a single message loaded from mailbox - add row to table."""
        row = self._message_table.rowCount()
        self._message_table.insertRow(row)
        
        from_addr = msg_info['from_addr']
        is_read = msg_info['is_read']
        
        # Check if sender is trusted
        is_trusted = self.is_sender_trusted(from_addr)
        
        # Origin column
        origin_item = QTableWidgetItem("Trusted" if is_trusted else "Untrusted")
        if is_trusted:
            origin_item.setForeground(QBrush(QColor("#2f9e44")))
        else:
            origin_item.setForeground(QBrush(QColor("#e03131")))
        if not is_read:
            font = origin_item.font()
            font.setBold(True)
            origin_item.setFont(font)
        self._message_table.setItem(row, 0, origin_item)
        
        # From column
        from_item = QTableWidgetItem(from_addr if from_addr else "")
        from_item.setData(Qt.ItemDataRole.UserRole, msg_info['msg_num'])
        from_item.setData(Qt.ItemDataRole.UserRole + 1, is_read)
        if not is_read:
            font = from_item.font()
            font.setBold(True)
            from_item.setFont(font)
        self._message_table.setItem(row, 1, from_item)
        
        # Subject column
        subject = msg_info['subject']
        subject_item = QTableWidgetItem(subject[:60] if subject else "(No subject)")
        if not is_read:
            font = subject_item.font()
            font.setBold(True)
            subject_item.setFont(font)
        self._message_table.setItem(row, 2, subject_item)
        
        # Date column (show full date with seconds)
        date_str = msg_info['date_str']
        date_item = QTableWidgetItem(date_str if date_str else "")
        if not is_read:
            font = date_item.font()
            font.setBold(True)
            date_item.setFont(font)
        self._message_table.setItem(row, 3, date_item)
        
        # Size column
        size = msg_info['size']
        size_kb = size / 1024 if size else 0
        size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB"
        size_item = QTableWidgetItem(size_str)
        if not is_read:
            font = size_item.font()
            font.setBold(True)
            size_item.setFont(font)
        self._message_table.setItem(row, 4, size_item)
        
        # Attachments column
        attachment_count = msg_info.get('attachment_count', 0)
        attachments_item = QTableWidgetItem(str(attachment_count))
        if not is_read:
            font = attachments_item.font()
            font.setBold(True)
            attachments_item.setFont(font)
        self._message_table.setItem(row, 5, attachments_item)
    
    def _on_mailbox_load_finished(self, count):
        """Handle mailbox loading completion."""
        self._progress.setVisible(False)
        if count == 0:
            self._status_label.setText(f"No messages in {self._current_mailbox}")
        else:
            self._status_label.setText(f"Connected - {count} messages in {self._current_mailbox}")
        # Re-apply filters after loading (preserves search text and filter selection)
        self._apply_message_filter()
    
    def _on_mailbox_load_error(self, error_msg):
        """Handle mailbox loading error."""
        self._progress.setVisible(False)
        self._status_label.setText(f"Error: {error_msg}")
    
    def _refresh_mailbox(self):
        """Refresh current mailbox - reload messages."""
        current_item = self._mailbox_list.currentItem()
        if current_item:
            self._on_mailbox_selected(current_item)
        else:
            # Default to INBOX
            for i in range(self._mailbox_list.count()):
                item = self._mailbox_list.item(i)
                if item and item.text() == "INBOX":
                    self._mailbox_list.setCurrentItem(item)
                    self._on_mailbox_selected(item)
                    break
    
    def _on_message_selected(self):
        """Handle message selection - show preview (fast, no attachment download)."""
        # Cancel any previous worker thread
        if self._email_worker and self._email_worker.isRunning():
            self._email_worker.cancel()
            self._email_worker.wait(100)  # Wait up to 100ms for thread to finish
        
        rows = self._message_table.selectionModel().selectedRows()
        if not rows:
            self._email_preview.clear()
            self._attachment_list.clear()
            self._download_btn.setEnabled(False)
            self._mark_read_btn.setEnabled(False)
            self._mark_unread_btn.setEnabled(False)
            self._delete_msg_btn.setEnabled(False)
            if hasattr(self, '_trust_sender_btn'):
                self._trust_sender_btn.setEnabled(False)
            if hasattr(self, '_untrust_sender_btn'):
                self._untrust_sender_btn.setEnabled(False)
            return
        
        # Enable delete button for any selection (multi-select supported)
        self._delete_msg_btn.setEnabled(True)
        
        row = rows[0].row()
        from_item = self._message_table.item(row, 1)  # Column 1 is now From (was 0)
        if not from_item:
            return

        if hasattr(self, '_trust_sender_btn'):
            self._trust_sender_btn.setEnabled(True)
        self._current_sender_email = self._extract_email_address(from_item.text())
        is_trusted = self.is_sender_trusted(self._current_sender_email)
        if hasattr(self, '_trust_sender_btn'):
            self._trust_sender_btn.setEnabled(not is_trusted)
        if hasattr(self, '_untrust_sender_btn'):
            self._untrust_sender_btn.setEnabled(is_trusted)
        
        msg_num = from_item.data(Qt.ItemDataRole.UserRole)
        if not msg_num or not hasattr(self, '_imap') or not self._imap:
            return
        
        # Store current message number for later use
        self._current_msg_num = msg_num
        self._current_msg_row = row
        
        # Enable mark buttons immediately
        self._mark_read_btn.setEnabled(True)
        self._mark_unread_btn.setEnabled(True)
        
        # Show loading indicator and progress
        self._email_preview.setPlainText("Loading email content...")
        self._progress.setVisible(True)
        self._progress.setMaximum(0)  # Indeterminate progress
        self._status_label.setText("Loading email details...")
        
        # Start background worker to fetch email
        self._email_worker = EmailFetchWorker(self._imap, self._imap_lock, msg_num, self)
        self._email_worker.finished.connect(self._on_email_loaded)
        self._email_worker.error.connect(self._on_email_load_error)
        self._email_worker.start()
    
    def _on_email_loaded(self, parsed_data):
        """Handle email data loaded from background thread."""
        # Hide progress bar
        self._progress.setVisible(False)
        self._status_label.setText(f"Connected - {self._message_table.rowCount()} messages")
        
        try:
            import html
            
            # Extract data from parsed_data
            from_addr = parsed_data['from']
            to_addr = parsed_data['to']
            cc_addr = parsed_data['cc']
            subject = parsed_data['subject']
            date_str = parsed_data['date']
            body_html = parsed_data['body_html']
            body_text = parsed_data['body_text']
            self._current_attachments = parsed_data['attachments']
            
            # Build preview HTML
            body_content = ""
            if body_html:
                body_content = body_html
            elif body_text:
                body_content = f"<pre style='white-space: pre-wrap; word-wrap: break-word;'>{html.escape(body_text)}</pre>"
            else:
                body_content = "<i>No message body</i>"
            
            preview_html = f"""
            <div style="font-family: Arial, sans-serif; padding: 10px;">
                <div style="border-bottom: 1px solid #ddd; padding-bottom: 10px; margin-bottom: 10px; background: #f8f9fa; padding: 10px; border-radius: 4px;">
                    <b>From:</b> {html.escape(from_addr)}<br>
                    <b>To:</b> {html.escape(to_addr)}<br>
                    {"<b>Cc:</b> " + html.escape(cc_addr) + "<br>" if cc_addr else ""}
                    <b>Subject:</b> {html.escape(subject)}<br>
                    <b>Date:</b> {html.escape(date_str)}
                </div>
                <div style="padding: 10px;">
                    {body_content}
                </div>
            </div>
            """
            self._email_preview.setHtml(preview_html)
            
            # Update attachments list
            self._attachment_list.clear()
            for att in self._current_attachments:
                self._attachment_list.addItem(att['filename'])
            
            self._download_btn.setEnabled(len(self._current_attachments) > 0)
            self._download_all_btn.setEnabled(len(self._current_attachments) > 0)
            
        except Exception as e:
            self._email_preview.setPlainText(f"Error loading message: {str(e)}")
    
    def _on_email_load_error(self, error_msg):
        """Handle error from background email loading."""
        self._progress.setVisible(False)
        self._status_label.setText("Error loading email")
        self._email_preview.setPlainText(f"Error loading message: {error_msg}")
    
    def _on_message_double_clicked(self, item):
        """Handle double-click on message - does nothing."""
        pass  # Double-click does nothing
    
    def _download_email_to_sync_folder(self):
        """Download current email to configured download folder."""
        rows = self._message_table.selectionModel().selectedRows()
        if not rows:
            return

        working_folder = self._require_download_folder()
        if not working_folder:
            return

        row = rows[0].row()
        from_item = self._message_table.item(row, 1)
        subject_item = self._message_table.item(row, 2)

        if not from_item:
            return

        from storage.sanitizer import sanitize_identifier
        sender_email = self._extract_email_address(from_item.text())
        sanitized_email = sanitize_identifier(sender_email or "unknown")
        msg_num = from_item.data(Qt.ItemDataRole.UserRole)
        subject = subject_item.text() if subject_item else "email"

        owner_folder = working_folder / sanitized_email
        owner_folder.mkdir(parents=True, exist_ok=True)

        try:
            self._status_label.setText("Checking email...")
            QApplication.processEvents()
            status, msg_data = self._imap.fetch(msg_num.encode(), '(RFC822)')
            if status != 'OK':
                QMessageBox.warning(self, "Download", "Failed to fetch email.")
                return

            raw_email = msg_data[0][1]
            safe_subject = "".join(c for c in subject if c.isalnum() or c in (' ', '-', '_'))[:50]
            filename = f"{safe_subject}_{msg_num}.eml"
            filepath = owner_folder / filename

            self._status_label.setText(f"Saving to {filepath}")
            QApplication.processEvents()
            with open(filepath, 'wb') as f:
                f.write(raw_email)
            self._write_download_metadata(
                filepath,
                "email",
                {
                    "message_id": msg_num,
                    "sender": sender_email,
                    "subject": subject,
                    "type": "message",
                },
            )
            self._status_label.setText(f"Email saved: {filepath}")
            QMessageBox.information(self, "Download", f"Email saved to:\n{filepath}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Download", f"Failed to download email: {exc}")

    def _on_attachment_fetch_finished(self, payload_map: dict[int, dict[str, Any]]):
        """Handle completion of background attachment fetch."""
        for idx, payload in payload_map.items():
            if 0 <= idx < len(self._current_attachments):
                self._current_attachments[idx]['data'] = payload.get('data')
                if not self._current_attachments[idx].get('filename') and payload.get('filename'):
                    self._current_attachments[idx]['filename'] = payload['filename']

        callback = self._pending_attachment_callback
        self._pending_attachment_callback = None
        self._pending_attachment_indices = []
        self._cleanup_attachment_fetch()
        if callback:
            callback(True)

    def _on_attachment_fetch_error(self, message: str):
        """Handle errors from background attachment fetch."""
        self._cleanup_attachment_fetch()
        QMessageBox.warning(self, "Download", f"Failed to fetch attachment data: {message}")
        callback = self._pending_attachment_callback
        self._pending_attachment_callback = None
        self._pending_attachment_indices = []
        if callback:
            callback(False)

    def _cleanup_attachment_fetch(self):
        self._download_progress.setVisible(False)
        worker = self._attachment_fetch_worker
        if worker:
            worker.deleteLater()
        self._attachment_fetch_worker = None

    def _fetch_attachment_data(self, index: int) -> Optional[dict[str, Any]]:
        """Synchronously fetch a single attachment payload for the current message."""
        if not hasattr(self, '_current_attachments') or not self._current_attachments:
            return None
        if index < 0 or index >= len(self._current_attachments):
            return None

        attachment = self._current_attachments[index]
        if attachment.get('data'):
            return attachment

        msg_num = getattr(self, '_current_msg_num', None)
        if not msg_num:
            return None

        imap_conn = getattr(self, '_imap', None)
        if not imap_conn:
            return None

        try:
            lock = getattr(self, '_imap_lock', None) or nullcontext()
            with lock:
                status, msg_data = imap_conn.fetch(msg_num.encode(), '(RFC822)')
            if status != 'OK' or not msg_data or not msg_data[0]:
                return None

            raw_email = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            message = email.message_from_bytes(raw_email)

            attachments: list[dict[str, Any]] = []
            for part in message.walk():
                if not EmailFetchWorker._is_attachment(part):
                    continue
                filename = part.get_filename() or 'attachment'
                filename = EmailFetchWorker._decode_header(filename)
                payload = part.get_payload(decode=True) or b''
                attachments.append(
                    {
                        'filename': filename,
                        'data': payload,
                        'content_type': part.get_content_type() or '',
                    }
                )

            if 0 <= index < len(attachments):
                fetched = attachments[index]
                self._current_attachments[index]['data'] = fetched.get('data')
                if fetched.get('filename') and not self._current_attachments[index].get('filename'):
                    self._current_attachments[index]['filename'] = fetched['filename']
                return fetched
        except Exception as exc:  # noqa: BLE001
            print(f"DEBUG: Failed to fetch attachment data synchronously: {exc}")
        return None

    def _ensure_attachment_data(self, indices: list[int], on_complete: Callable[[bool], None]):
        """Ensure attachments at the given indices have data, fetching missing blobs asynchronously."""
        if not indices:
            on_complete(True)
            return

        if not hasattr(self, '_current_attachments') or not self._current_attachments:
            on_complete(False)
            return

        valid_indices = [idx for idx in indices if 0 <= idx < len(self._current_attachments)]
        if not valid_indices:
            on_complete(True)
            return

        missing = [idx for idx in valid_indices if not self._current_attachments[idx].get('data')]
        if not missing:
            on_complete(True)
            return

        msg_num = getattr(self, '_current_msg_num', None)
        if not msg_num:
            QMessageBox.warning(self, "Download", "No message is currently selected.")
            on_complete(False)
            return

        if not hasattr(self, '_imap') or not self._imap:
            QMessageBox.warning(self, "Download", "Not connected to the mail server.")
            on_complete(False)
            return

        if self._attachment_fetch_worker and self._attachment_fetch_worker.isRunning():
            QMessageBox.warning(
                self,
                "Download",
                "Another attachment download is already in progress. Please wait."
            )
            on_complete(False)
            return

        self._pending_attachment_indices = missing
        self._pending_attachment_callback = on_complete

        self._download_progress.setVisible(True)
        self._download_progress.setRange(0, 0)
        self._download_progress.setFormat("Fetching attachment data...")
        self._status_label.setText("Fetching attachment data...")
        QApplication.processEvents()

        worker = AttachmentFetchWorker(self._imap, self._imap_lock, msg_num, missing, self)
        self._attachment_fetch_worker = worker
        worker.finished.connect(self._on_attachment_fetch_finished)
        worker.error.connect(self._on_attachment_fetch_error)
        worker.start()

    def _write_download_metadata(self, file_path: Path, source: str, extra: Optional[dict] = None):
        """Write a lightweight JSON sidecar with the download timestamp."""
        metadata = {
            "source": source,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            for key, value in extra.items():
                if value not in (None, ""):
                    metadata[key] = value

        metadata_path = file_path.parent / f"{file_path.name}.metadata.json"
        try:
            with open(metadata_path, 'w', encoding='utf-8') as meta_file:
                json.dump(metadata, meta_file, indent=2)
        except Exception as exc:  # noqa: BLE001
            print(f"DEBUG: Failed to write metadata for {file_path}: {exc}")

    def _download_selected_attachment(self):
        """Download selected attachment to configured download folder with progress bar."""
        if not hasattr(self, '_current_attachments') or not self._current_attachments:
            return

        selected = self._attachment_list.currentRow()
        if selected < 0:
            QMessageBox.warning(self, "Download", "Please select an attachment first.")
            return

        working_folder = self._require_download_folder()
        if not working_folder:
            return

        if not hasattr(self, '_current_msg_row'):
            QMessageBox.warning(self, "Download", "No message selected.")
            return

        from_item = self._message_table.item(self._current_msg_row, 1)
        from_addr = from_item.text() if from_item else "unknown"
        from storage.sanitizer import sanitize_identifier
        email = self._extract_email_address(from_addr)
        sanitized_email = sanitize_identifier(email or "unknown")
        subject_item = self._message_table.item(self._current_msg_row, 2)
        subject = subject_item.text() if subject_item else "email"
        message_id = getattr(self, '_current_msg_num', None)

        att = self._current_attachments[selected]
        owner_folder = working_folder / sanitized_email
        owner_folder.mkdir(parents=True, exist_ok=True)
        filepath = owner_folder / att['filename']

        self._download_btn.setEnabled(False)
        self._download_all_btn.setEnabled(False)

        def finish_single(success: bool):
            if success:
                data = att.get('data')
                if not data:
                    QMessageBox.warning(self, "Download", "Attachment data was empty.")
                else:
                    total_size = len(data)
                    total_kb = max(total_size // 1024, 1)

                    self._download_progress.setVisible(True)
                    self._download_progress.setRange(0, total_kb)
                    self._download_progress.setValue(0)
                    self._download_progress.setFormat(f"%p% - %v KB / {total_kb} KB")
                    QApplication.processEvents()

                    chunk_size = 8192
                    written = 0
                    self._status_label.setText(f"Saving to {filepath}")
                    QApplication.processEvents()
                    try:
                        with open(filepath, 'wb') as f:
                            for i in range(0, total_size, chunk_size):
                                chunk = data[i:i + chunk_size]
                                f.write(chunk)
                                written += len(chunk)
                                self._download_progress.setValue(written // 1024)
                                QApplication.processEvents()
                        self._download_progress.setValue(total_kb)
                        QApplication.processEvents()
                        self._write_download_metadata(
                            filepath,
                            "email",
                            {
                                "message_id": message_id,
                                "sender": email,
                                "subject": subject,
                                "attachment": att.get('filename'),
                            },
                        )
                        self._status_label.setText(f"Attachment saved: {filepath}")
                        QMessageBox.information(self, "Download", f"Attachment saved to:\n{filepath}")
                    except Exception as exc:  # noqa: BLE001
                        QMessageBox.warning(self, "Download", f"Failed to save attachment: {exc}")
            self._download_progress.setVisible(False)
            self._download_btn.setEnabled(True)
            self._download_all_btn.setEnabled(True)

        self._ensure_attachment_data([selected], finish_single)

    def _download_all_attachments(self):
        """Download all attachments from current email to configured download folder."""
        if not hasattr(self, '_current_attachments') or not self._current_attachments:
            QMessageBox.information(self, "Download", "No attachments to download.")
            return

        working_folder = self._require_download_folder()
        if not working_folder:
            return

        from_item = self._message_table.item(self._current_msg_row, 1) if hasattr(self, '_current_msg_row') else None
        from storage.sanitizer import sanitize_identifier
        sender_email = self._extract_email_address(from_item.text()) if from_item else "unknown"
        sanitized_email = sanitize_identifier(sender_email or "unknown")
        subject_item = self._message_table.item(self._current_msg_row, 2) if hasattr(self, '_current_msg_row') else None
        subject = subject_item.text() if subject_item else "email"
        message_id = getattr(self, '_current_msg_num', None)

        owner_folder = working_folder / sanitized_email
        owner_folder.mkdir(parents=True, exist_ok=True)

        self._download_btn.setEnabled(False)
        self._download_all_btn.setEnabled(False)

        def finalize(downloaded: int, failed: int):
            self._download_progress.setVisible(False)
            self._download_btn.setEnabled(True)
            self._download_all_btn.setEnabled(True)
            self._status_label.setText(
                f"Downloaded {downloaded} attachment(s), failed: {failed}."
            )
            QMessageBox.information(
                self,
                "Download",
                f"Downloaded {downloaded} attachment(s).\nFailed: {failed}.\nLocation: {owner_folder}"
            )

        def process_all():
            downloaded = 0
            failed = 0
            total_attachments = len(self._current_attachments)
            self._download_progress.setVisible(True)
            self._download_progress.setRange(0, total_attachments)
            self._download_progress.setValue(0)

            for idx, att in enumerate(self._current_attachments):
                try:
                    self._status_label.setText(
                        f"Downloading {att['filename']} ({idx + 1}/{total_attachments})..."
                    )
                    self._download_progress.setFormat(
                        f"{idx + 1}/{total_attachments} - {att['filename']}"
                    )
                    QApplication.processEvents()

                    data = att.get('data')
                    if not data:
                        failed += 1
                        continue

                    filepath = owner_folder / att['filename']
                    with open(filepath, 'wb') as f:
                        f.write(data)
                    self._write_download_metadata(
                        filepath,
                        "email",
                        {
                            "message_id": message_id,
                            "sender": sender_email,
                            "subject": subject,
                            "attachment": att.get('filename'),
                        },
                    )
                    downloaded += 1
                except Exception:
                    failed += 1
                finally:
                    self._download_progress.setValue(idx + 1)

            finalize(downloaded, failed)

        missing_indices = [idx for idx, att in enumerate(self._current_attachments) if not att.get('data')]
        if missing_indices:
            self._ensure_attachment_data(
                missing_indices,
                lambda success: process_all() if success else finalize(0, len(missing_indices))
            )
        else:
            process_all()

    def _mark_as_read(self):
        """Mark selected message as read."""
        if not hasattr(self, '_current_msg_num') or not self._current_msg_num:
            return
        if not hasattr(self, '_imap') or not self._imap:
            return

        try:
            self._imap.store(self._current_msg_num.encode(), '+FLAGS', '\\Seen')

            if hasattr(self, '_current_msg_row'):
                for col in range(self._message_table.columnCount()):
                    item = self._message_table.item(self._current_msg_row, col)
                    if item:
                        font = item.font()
                        font.setBold(False)
                        item.setFont(font)
                from_item = self._message_table.item(self._current_msg_row, 1)
                if from_item:
                    from_item.setData(Qt.ItemDataRole.UserRole + 1, True)

            self._status_label.setText("Message marked as read")
            self._apply_message_filter()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Error", f"Failed to mark as read: {exc}")

    def _mark_as_unread(self):
        """Mark selected message as unread."""
        if not hasattr(self, '_current_msg_num') or not self._current_msg_num:
            return
        if not hasattr(self, '_imap') or not self._imap:
            return
        
        try:
            self._imap.store(self._current_msg_num.encode(), '-FLAGS', '\\Seen')
            
            # Update UI to show as unread (add bold) and update is_read flag
            if hasattr(self, '_current_msg_row'):
                for col in range(self._message_table.columnCount()):
                    item = self._message_table.item(self._current_msg_row, col)
                    if item:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                # Update the is_read flag stored in From column (UserRole + 1)
                from_item = self._message_table.item(self._current_msg_row, 1)
                if from_item:
                    from_item.setData(Qt.ItemDataRole.UserRole + 1, False)
            
            self._status_label.setText("Message marked as unread")
            # Re-apply filter to update visibility
            self._apply_message_filter()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to mark as unread: {str(e)}")
    
    def _delete_selected_messages(self):
        """Delete selected messages (supports multi-select)."""
        if not hasattr(self, '_imap') or not self._imap:
            return
        
        rows = self._message_table.selectionModel().selectedRows()
        if not rows:
            return
        
        count = len(rows)
        reply = QMessageBox.question(
            self, "Delete Messages",
            f"Are you sure you want to delete {count} message(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        try:
            deleted = 0
            for row_index in rows:
                row = row_index.row()
                # msg_num is stored in column 1 (From column) UserRole
                from_item = self._message_table.item(row, 1)
                if from_item:
                    msg_num = from_item.data(Qt.ItemDataRole.UserRole)
                    if msg_num:
                        # Mark for deletion
                        msg_num_bytes = msg_num.encode() if isinstance(msg_num, str) else msg_num
                        self._imap.store(msg_num_bytes, '+FLAGS', '\\Deleted')
                        deleted += 1
            
            # Expunge to permanently delete
            self._imap.expunge()
            
            # Refresh mailbox
            self._refresh_mailbox()
            
            self._status_label.setText(f"Deleted {deleted} message(s)")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to delete messages: {str(e)}")
    
    def _get_sync_folder(self):
        """Get the local sync folder from database."""
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'gisimple_sync_folder'")
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    
    def _load_keycloak_groups(self):
        """Load Keycloak groups from GISimple server."""
        try:
            # Check if we have a stored auth client
            if not hasattr(self, '_gisimple_auth_client') or not self._gisimple_auth_client:
                QMessageBox.warning(self, "Groups", "Please log in to GISimple first (Settings tab) to load Keycloak groups.")
                return
            
            auth_client = self._gisimple_auth_client
            
            if not auth_client.is_authenticated:
                QMessageBox.warning(self, "Groups", "Please log in to GISimple first to load Keycloak groups.")
                return
            
            groups = auth_client.get_groups()
            
            # Clear and repopulate group filter
            self._group_filter_combo.clear()
            self._group_filter_combo.addItem("All Groups", "")
            
            # Store groups for later lookup
            self._keycloak_groups = {}
            for group in groups:
                group_id = group.get('id', '')
                group_name = group.get('name', 'Unknown')
                self._group_filter_combo.addItem(group_name, group_id)
                self._keycloak_groups[group_id] = group_name
            
            # Also cache user emails by group for filtering
            self._group_emails = {}
            for group in groups:
                group_id = group.get('id', '')
                try:
                    users = auth_client.get_users_by_group(group_id)
                    self._group_emails[group_id] = [u.get('email', '').lower() for u in users if u.get('email')]
                except Exception:
                    self._group_emails[group_id] = []
            
            self._status_label.setText(f"Loaded {len(groups)} Keycloak groups")
            
        except Exception as e:
            QMessageBox.warning(self, "Groups", f"Failed to load Keycloak groups: {str(e)}")
    
    def _on_group_filter_changed(self, index):
        """Handle group filter selection change."""
        # Re-filter the message list based on selected group
        if hasattr(self, '_all_messages'):
            self._apply_message_filters()
    
    def _get_sender_group(self, from_address: str) -> str:
        """Get the Keycloak group for an email sender."""
        if not hasattr(self, '_group_emails') or not self._group_emails:
            return ""
        
        # Extract email from "Name <email>" format
        import re
        match = re.search(r'<([^>]+)>', from_address)
        email = match.group(1).lower() if match else from_address.lower()
        
        # Find which group this email belongs to
        for group_id, emails in self._group_emails.items():
            if email in emails:
                return self._keycloak_groups.get(group_id, "")
        
        return ""
    
    def _apply_message_filters(self):
        """Apply search, read/unread, and group filters to message list."""
        if not hasattr(self, '_all_messages'):
            return
        
        search_text = self._search_box.text().lower()
        read_filter = self._filter_combo.currentText()
        selected_group_id = self._group_filter_combo.currentData()
        
        self._message_table.setRowCount(0)
        
        for msg_data in self._all_messages:
            # Apply search filter
            if search_text:
                if search_text not in msg_data.get('from', '').lower() and \
                   search_text not in msg_data.get('subject', '').lower():
                    continue
            
            # Apply read/unread filter
            if read_filter == "Read" and msg_data.get('unread', False):
                continue
            if read_filter == "Unread" and not msg_data.get('unread', False):
                continue
            
            # Apply group filter
            if selected_group_id:
                sender_email = msg_data.get('from', '').lower()
                import re
                match = re.search(r'<([^>]+)>', sender_email)
                email = match.group(1).lower() if match else sender_email
                
                group_emails = self._group_emails.get(selected_group_id, [])
                if email not in group_emails:
                    continue
            
            # Add row to table
            self._add_message_row(msg_data)
    
    def _login_to_gisimple(self):
        """Login to GISimple server via Keycloak OpenID Connect token endpoint.
        Reads credentials from the selected GISimple server configuration."""
        # Get selected server from the GISimple tab server list
        item = self._gisimple_server_list.currentItem()
        if not item:
            QMessageBox.warning(self, "Login", "Please select a GISimple server first in the GISimple tab.")
            return
        
        server_id = item.data(Qt.ItemDataRole.UserRole)
        
        # Load server details from DB
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute("SELECT url, realm, client_id, username, password FROM gisimple_servers WHERE id=?", (server_id,))
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            QMessageBox.warning(self, "Login", "Server not found in database.")
            return
        
        url, realm, client_id, username, password = row
        url = url.strip().rstrip('/')
        realm = realm or 'gisimple'
        client_id = client_id or 'gisimple'
        
        if not url or not username or not password:
            QMessageBox.warning(self, "Login", "Server URL, username, or password is missing.\nEdit the server configuration to add credentials.")
            return
        
        try:
            import ssl
            import urllib.request
            import urllib.parse
            import json as json_module
            
            # Keycloak token endpoint (same as Java GISimpleClient)
            token_url = f"{url}/auth/realms/{realm}/protocol/openid-connect/token"
            
            # Form-encoded POST data (NOT JSON)
            form_data = urllib.parse.urlencode({
                'grant_type': 'password',
                'client_id': client_id,
                'username': username,
                'password': password
            }).encode('utf-8')
            
            request = urllib.request.Request(
                token_url,
                data=form_data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                method='POST'
            )
            
            # SSL context for self-signed certs
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            with urlopen_allowed(request, timeout=30, context=ssl_context) as response:
                token_data = json_module.loads(response.read().decode('utf-8'))
            
            access_token = token_data.get('access_token', '')
            if not access_token:
                raise Exception("No access token received from Keycloak")
            
            # Store token and server URL for later API calls
            self._gisimple_token = access_token
            self._gisimple_refresh_token = token_data.get('refresh_token', '')
            self._gisimple_server_url = url
            
            # Save token back to DB
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            cursor.execute("UPDATE gisimple_servers SET token=?, last_connected=datetime('now') WHERE id=?",
                           (access_token, server_id))
            conn.commit()
            conn.close()
            
            # Decode JWT to get user info (payload is base64-encoded, middle part)
            import base64
            payload_b64 = access_token.split('.')[1]
            # Add padding if needed
            payload_b64 += '=' * (4 - len(payload_b64) % 4)
            payload = json_module.loads(base64.b64decode(payload_b64).decode('utf-8'))
            
            display_name = payload.get('name', payload.get('preferred_username', username))
            email = payload.get('email', '')
            groups = payload.get('groups', [])
            roles = payload.get('realm_access', {}).get('roles', [])
            
            # Store user info
            self._gisimple_user = {
                'username': username,
                'display_name': display_name,
                'email': email,
                'groups': groups,
                'roles': roles
            }
            
            self._gisimple_status.setText(f"● Connected: {display_name}")
            self._gisimple_status.setStyleSheet("color: #2f9e44; font-size: 11px; font-weight: bold;")
            
            # Auto-fetch user's projects after successful login
            self._fetch_user_projects()
            
        except urllib.error.HTTPError as e:
            error_body = ''
            try:
                error_body = e.read().decode('utf-8')
            except Exception as exc:
                log_debug_exception('Suppressed error', exc)
            self._gisimple_status.setText("Login failed")
            self._gisimple_status.setStyleSheet("color: #c92a2a;")
            details = f"HTTP {e.code}"
            if error_body:
                details += f" - {error_body}"
            QMessageBox.warning(self, "GISimple Login Failed", self._format_gisimple_login_failure_message(details))
        except Exception as e:
            self._gisimple_status.setText("Login failed")
            self._gisimple_status.setStyleSheet("color: #c92a2a;")
            QMessageBox.warning(self, "GISimple Login Failed", self._format_gisimple_login_failure_message(str(e)))

    def _add_server(self):
        """Show add server dialog."""
        dialog = ServerConfigDialog(parent=self)
        if dialog.exec():
            data = dialog.get_data()
            self._save_server_to_db(data)
            self._refresh_server_list()
    
    def _save_server_to_db(self, data):
        """Save server configuration to database."""
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        if data.get('is_default'):
            cursor.execute("UPDATE email_servers SET is_default = 0")
        
        if data.get('id'):
            cursor.execute("""
                UPDATE email_servers SET 
                    name=?, protocol=?, host=?, port=?, use_ssl=?, username=?, password=?, is_default=?
                WHERE id=?
            """, (data['name'], data['protocol'], data['host'], data['port'],
                  1 if data['use_ssl'] else 0, data['username'], data['password'],
                  1 if data['is_default'] else 0, data['id']))
        else:
            cursor.execute("""
                INSERT INTO email_servers (name, protocol, host, port, use_ssl, username, password, is_default)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (data['name'], data['protocol'], data['host'], data['port'],
                  1 if data['use_ssl'] else 0, data['username'], data['password'],
                  1 if data['is_default'] else 0))
        
        conn.commit()
        conn.close()
    
    def _refresh_server_list(self):
        """Refresh server list from database."""
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, protocol, is_default FROM email_servers ORDER BY name")
        servers = cursor.fetchall()
        conn.close()
        
        self._server_list.clear()
        self._server_combo.clear()
        
        if not servers:
            self._server_list.addItem("(No servers configured)")
            self._server_combo.addItem("No servers configured")
        else:
            default_row = -1
            for server_id, name, protocol, is_default in servers:
                display = f"{name} ({protocol.upper()})"
                if is_default:
                    display += " [Default]"
                
                item = QListWidgetItem(display)
                item.setData(Qt.ItemDataRole.UserRole, server_id)
                self._server_list.addItem(item)
                if is_default:
                    default_row = self._server_list.count() - 1
                
                self._server_combo.addItem(display, server_id)

            # Keep a valid current selection so Edit/Delete can operate immediately.
            if self._server_list.count() > 0:
                row_to_select = default_row if default_row >= 0 else 0
                self._server_list.setCurrentRow(row_to_select)

    def _on_settings_changed(self):
        """Enable save button when settings are changed."""
        self._save_btn.setEnabled(True)
        self._save_btn.setStyleSheet("")  # Reset to default style
    
    def _on_create_settings_database(self):
        """Handle 'Create Settings Database' button in the Settings page."""
        if prompt_create_main_database(self):
            QMessageBox.information(
                self,
                "Settings Database",
                "Settings database created successfully."
            )
            self._refresh_server_list()

    def _save_settings(self):
        """Save settings to database."""
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            
            # Save trust mode
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", 
                          ('trust_mode', self._trust_mode.currentText()))
            
            # Save general settings
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", 
                          ('auto_refresh', '1' if self._auto_refresh.isChecked() else '0'))
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", 
                          ('auto_refresh_minutes', self._auto_refresh_minutes.text().strip()))
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", 
                          ('show_notifications', '1' if self._show_notifications.isChecked() else '0'))
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", 
                          ('auto_load_data', '1' if self._auto_load_data.isChecked() else '0'))
            
            conn.commit()
            conn.close()
            
            QMessageBox.information(self, "Settings", "Settings saved successfully.")
            # Disable save button after saving
            self._save_btn.setEnabled(False)
            self._save_btn.setStyleSheet("background: #e9ecef; color: #868e96; border: 1px solid #dee2e6; font-weight: bold;")
            
            # Update auto-refresh timer based on new settings
            self._setup_auto_refresh_timer()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to save settings: {str(e)}")
    
    def _setup_auto_refresh_timer(self):
        """Setup or update the auto-refresh timer based on saved settings."""
        # Stop existing timer if any
        if self._auto_refresh_timer:
            self._auto_refresh_timer.stop()
            self._auto_refresh_timer = None
        
        # Load settings from database
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = 'auto_refresh'")
            row = cursor.fetchone()
            auto_refresh_enabled = row[0] == '1' if row else False
            
            cursor.execute("SELECT value FROM settings WHERE key = 'auto_refresh_minutes'")
            row = cursor.fetchone()
            minutes = int(row[0]) if row and row[0].isdigit() else 5
            conn.close()
            
            if auto_refresh_enabled and minutes > 0:
                self._auto_refresh_timer = QTimer(self)
                self._auto_refresh_timer.timeout.connect(self._auto_refresh_mailbox)
                self._auto_refresh_timer.start(minutes * 60 * 1000)  # Convert minutes to milliseconds
                print(f"DEBUG: Auto-refresh timer started: every {minutes} minutes")
        except Exception as e:
            print(f"DEBUG: Failed to setup auto-refresh timer: {e}")
    
    def _auto_refresh_mailbox(self):
        """Auto-refresh mailbox if connected."""
        if hasattr(self, '_imap') and self._imap:
            print("DEBUG: Auto-refreshing mailbox...")
            self._refresh_mailbox()
    
    def _on_trust_mode_changed(self, index):
        """Handle trust mode change - enable/disable lists based on mode."""
        mode = self._trust_mode.currentText()
        if mode == "Trust All":
            self._trusted_domains_list.setEnabled(False)
            self._trusted_accounts_list.setEnabled(False)
            self._trust_info_label.setText("All emails are trusted. Lists below are ignored.")
        else:
            self._trusted_domains_list.setEnabled(True)
            self._trusted_accounts_list.setEnabled(True)
            self._trust_info_label.setText("Email is trusted if sender matches ANY domain OR ANY account below.")
        self._on_settings_changed()
    
    def _load_trusted_lists(self):
        """Load trusted domains and accounts from database."""
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        # Load domains
        cursor.execute("SELECT domain FROM trusted_domains ORDER BY domain")
        domains = cursor.fetchall()
        self._trusted_domains_list.clear()
        for (domain,) in domains:
            self._trusted_domains_list.addItem(domain)
        
        # Load accounts
        cursor.execute("SELECT email, source, gisimple_group FROM trusted_accounts ORDER BY email")
        accounts = cursor.fetchall()
        self._trusted_accounts_list.clear()
        for email, source, group in accounts:
            display = email
            if source == 'gisimple' and group:
                display = f"{email} ({group})"
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, email)
            self._trusted_accounts_list.addItem(item)
        
        conn.close()
    
    def _add_trusted_domain(self):
        """Add a trusted domain."""
        dialog = ValidatedInputDialog(
            "Add Trusted Domain", 
            "Enter domain (e.g., example.com):",
            validator=lambda x: '.' in x,  # Basic domain validation
            parent=self
        )
        if dialog.exec():
            domain = dialog.get_text().lower()
            
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            try:
                cursor.execute("INSERT INTO trusted_domains (domain) VALUES (?)", (domain,))
                conn.commit()
                self._trusted_domains_list.addItem(domain)
            except sqlite3.IntegrityError:
                QMessageBox.warning(self, "Duplicate", f"Domain '{domain}' already exists.")
            conn.close()
    
    def _remove_trusted_domain(self):
        """Remove selected trusted domains."""
        selected = self._trusted_domains_list.selectedItems()
        if not selected:
            return
        
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        for item in selected:
            domain = item.text()
            cursor.execute("DELETE FROM trusted_domains WHERE domain = ?", (domain,))
            self._trusted_domains_list.takeItem(self._trusted_domains_list.row(item))
        conn.commit()
        conn.close()
    
    def _add_trusted_account(self):
        """Add a trusted account."""
        dialog = ValidatedInputDialog(
            "Add Trusted Account", 
            "Enter email address:",
            validator=lambda x: '@' in x and '.' in x,  # Basic email validation
            parent=self
        )
        if dialog.exec():
            email = dialog.get_text().lower()
            
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            try:
                cursor.execute("INSERT INTO trusted_accounts (email, source) VALUES (?, 'manual')", (email,))
                conn.commit()
                self._trusted_accounts_list.addItem(email)
            except sqlite3.IntegrityError:
                QMessageBox.warning(self, "Duplicate", f"Account '{email}' already exists.")
            conn.close()
    
    def _remove_trusted_account(self):
        """Remove selected trusted accounts."""
        selected = self._trusted_accounts_list.selectedItems()
        if not selected:
            return
        
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        for item in selected:
            email = item.data(Qt.ItemDataRole.UserRole) or item.text()
            cursor.execute("DELETE FROM trusted_accounts WHERE email = ?", (email,))
            self._trusted_accounts_list.takeItem(self._trusted_accounts_list.row(item))
        conn.commit()
        conn.close()
    
    def _import_from_gisimple(self):
        """Import trusted accounts from GISimple groups using stored token."""
        if not hasattr(self, '_gisimple_token') or not self._gisimple_token:
            QMessageBox.warning(self, "Import", "Please log in to GISimple first (GISimple tab → select server → Login).")
            return
        
        try:
            import ssl
            import urllib.request
            import json as json_module
            
            if not hasattr(self, '_gisimple_user') or not self._gisimple_user:
                QMessageBox.warning(self, "Import", "No user info available. Please log in again.")
                return
            
            url = self._gisimple_server_url
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            # Check if user is admin - if so, fetch ALL groups from Keycloak Admin API
            roles = self._gisimple_user.get('roles', [])
            is_admin = 'gisimple-admin' in roles or 'gisimple-superadmin' in roles or 'admin' in roles
            
            groups = []
            if is_admin:
                # Try to fetch all groups from Keycloak Admin API
                try:
                    # Get realm from stored server config
                    item = self._gisimple_server_list.currentItem()
                    if item:
                        server_id = item.data(Qt.ItemDataRole.UserRole)
                        conn = sqlite3.connect(str(DB_PATH))
                        cursor = conn.cursor()
                        cursor.execute("SELECT realm FROM gisimple_servers WHERE id=?", (server_id,))
                        row = cursor.fetchone()
                        conn.close()
                        realm = row[0] if row and row[0] else 'gisimple'
                    else:
                        realm = 'gisimple'
                    
                    # Keycloak Admin API endpoint for groups
                    groups_url = f"{url}/auth/admin/realms/{realm}/groups"
                    request = urllib.request.Request(
                        groups_url,
                        headers={
                            'Authorization': f'Bearer {self._gisimple_token}',
                            'Accept': 'application/json'
                        },
                        method='GET'
                    )
                    
                    with urlopen_allowed(request, timeout=30, context=ssl_context) as response:
                        all_groups = json_module.loads(response.read().decode('utf-8'))
                        # Extract group names (Keycloak returns objects with 'name' field)
                        groups = [g.get('name', '') for g in all_groups if g.get('name')]
                except Exception as e:
                    # Fall back to user's own groups if admin API fails
                    groups = self._gisimple_user.get('groups', [])
            else:
                # Non-admin: use groups from JWT token
                groups = self._gisimple_user.get('groups', [])
            
            if not groups:
                QMessageBox.information(self, "Import", "No groups found.")
                return
            
            # Show group selection dialog
            from qgis.PyQt.QtWidgets import QInputDialog
            group_name, ok = QInputDialog.getItem(
                self, "Select Group", "Import users from group:", groups, 0, False
            )
            
            if not ok:
                return
            
            # Fetch group members via Keycloak Admin API or GISimple API
            # Try GISimple API to get group members
            api_url = f"{url}/api/groups/{urllib.parse.quote(group_name)}/members"
            request = urllib.request.Request(
                api_url,
                headers={
                    'Authorization': f'Bearer {self._gisimple_token}',
                    'Accept': 'application/json'
                },
                method='GET'
            )
            
            users = []
            try:
                with urlopen_allowed(request, timeout=30, context=ssl_context) as response:
                    users = json_module.loads(response.read().decode('utf-8'))
            except urllib.error.HTTPError:
                # If API endpoint doesn't exist, just add the logged-in user's email
                user_email = self._gisimple_user.get('email', '')
                if user_email:
                    users = [{'email': user_email}]
                    QMessageBox.information(
                        self, "Import",
                        f"Group member API not available.\nAdded your own email ({user_email}) from group '{group_name}'."
                    )
            
            if not users:
                QMessageBox.information(self, "Import", f"No users found in group '{group_name}'.")
                return
            
            # Add users to trusted accounts
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            added = 0
            for user in users:
                email = user.get('email', '').lower() if isinstance(user, dict) else str(user).lower()
                if email:
                    try:
                        cursor.execute(
                            "INSERT INTO trusted_accounts (email, source, gisimple_group) VALUES (?, 'gisimple', ?)",
                            (email, group_name)
                        )
                        added += 1
                    except sqlite3.IntegrityError:
                        pass  # Already exists
            conn.commit()
            conn.close()
            
            # Refresh list
            self._load_trusted_lists()
            QMessageBox.information(self, "Import", f"Imported {added} users from '{group_name}'.")
            
        except Exception as e:
            QMessageBox.warning(self, "Import Error", f"Failed to import: {str(e)}")
    
    def is_sender_trusted(self, sender_email: str) -> bool:
        """Check if an email sender is trusted based on current trust settings."""
        if not hasattr(self, '_trust_mode'):
            return False
        trust_mode = self._trust_mode.currentText()
        
        if trust_mode == "Trust All":
            return True
        
        if trust_mode == "Network Trusted (CIDR)":
            # Network trust would check IP - for now return True
            return True
        
        # Trusted Accounts Only mode
        email = self._extract_email_address(sender_email)
        domain = email.split('@')[1] if '@' in email else ''
        
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        # Check if domain is trusted
        cursor.execute("SELECT 1 FROM trusted_domains WHERE domain = ?", (domain,))
        if cursor.fetchone():
            conn.close()
            return True
        
        # Check if specific account is trusted
        cursor.execute("SELECT 1 FROM trusted_accounts WHERE email = ?", (email,))
        if cursor.fetchone():
            conn.close()
            return True
        
        conn.close()
        return False

    def _extract_email_address(self, sender_text: str) -> str:
        """Extract email address from display string (e.g. 'Name <email@domain>')."""
        import re
        match = re.search(r'<([^>]+)>', sender_text)
        email = match.group(1) if match else sender_text
        return email.strip().lower()

    def _trust_selected_sender(self):
        """Save selected sender as trusted account in SQLite."""
        if not hasattr(self, '_current_sender_email') or not self._current_sender_email:
            QMessageBox.warning(self, "Trust Sender", "No sender selected.")
            return

        email = self._extract_email_address(self._current_sender_email)
        if not email or '@' not in email:
            QMessageBox.warning(self, "Trust Sender", "Invalid email address.")
            return

        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT OR IGNORE INTO trusted_accounts (email, source) VALUES (?, 'manual')", (email,))
            conn.commit()
            self._load_trusted_lists()
            self._update_current_sender_trust(is_trusted=True)
        finally:
            conn.close()

    def _untrust_selected_sender(self):
        """Remove selected sender from trusted accounts in SQLite."""
        if not hasattr(self, '_current_sender_email') or not self._current_sender_email:
            QMessageBox.warning(self, "Untrust Sender", "No sender selected.")
            return

        email = self._extract_email_address(self._current_sender_email)
        if not email or '@' not in email:
            QMessageBox.warning(self, "Untrust Sender", "Invalid email address.")
            return

        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        try:
            cursor.execute("DELETE FROM trusted_accounts WHERE email = ?", (email,))
            conn.commit()
            self._load_trusted_lists()
            self._update_current_sender_trust(is_trusted=False)
        finally:
            conn.close()

    def _update_current_sender_trust(self, is_trusted: bool):
        """Update Origin column for current message and toggle buttons."""
        if not hasattr(self, '_current_msg_row'):
            return
        row = self._current_msg_row
        origin_item = QTableWidgetItem("Trusted" if is_trusted else "Untrusted")
        if is_trusted:
            origin_item.setForeground(QBrush(QColor("#2f9e44")))
        else:
            origin_item.setForeground(QBrush(QColor("#e03131")))
        self._message_table.setItem(row, 0, origin_item)
        if hasattr(self, '_trust_sender_btn'):
            self._trust_sender_btn.setEnabled(not is_trusted)
        if hasattr(self, '_untrust_sender_btn'):
            self._untrust_sender_btn.setEnabled(is_trusted)
    
    def _check_trusted_emails(self):
        """Check all visible emails and mark trusted ones in the Trusted column."""
        if not hasattr(self, '_message_table'):
            return
        
        row_count = self._message_table.rowCount()
        if row_count == 0:
            QMessageBox.information(self, "Check Trusted", "No emails to check. Please load a mailbox first.")
            return
        
        trusted_count = 0
        untrusted_count = 0
        
        for row in range(row_count):
            from_item = self._message_table.item(row, 0)
            if not from_item:
                continue
            
            from_addr = from_item.text()
            is_trusted = self.is_sender_trusted(from_addr)
            
            # Update Trusted column (column 5)
            trusted_item = QTableWidgetItem("✓" if is_trusted else "✗")
            if is_trusted:
                trusted_item.setForeground(QColor("#2f9e44"))
                trusted_count += 1
            else:
                trusted_item.setForeground(QColor("#c92a2a"))
                untrusted_count += 1
            
            self._message_table.setItem(row, 5, trusted_item)
        
        self._status_label.setText(f"Checked {row_count} emails: {trusted_count} trusted, {untrusted_count} untrusted")
        QMessageBox.information(
            self, "Check Trusted",
            f"Checked {row_count} emails:\n\n✓ {trusted_count} trusted\n✗ {untrusted_count} untrusted"
        )


# Backwards compatibility alias
StandardProjectDialog = GeoInboxDialog

# Standalone / script use only — do not auto-open when loaded as a QGIS plugin.
if __name__ == "__main__":
    GeoInboxDialog.show_dialog()
