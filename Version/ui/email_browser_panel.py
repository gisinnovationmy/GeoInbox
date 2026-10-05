"""
GeoInbox - Email Browser Panel

Dockable panel for browsing email messages and attachments.
"""

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QToolBar, QComboBox, QPushButton, QLabel, QFrame,
    QTextBrowser, QScrollArea, QSizePolicy, QTabWidget
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QThread, QObject, QTimer
from qgis.PyQt.QtGui import QIcon

from typing import Optional, List, Dict, Any
import os

from .widgets import (
    StatusIndicator, ProgressWidget, SearchBox, FilterComboBox,
    MessageListTable, MailboxList, AttachmentItem, TrustBadge,
    show_error_dialog, show_warning_dialog, show_info_dialog
)
from ..adapters import EmailAdapter, AdapterConfig, MessageMetadata
from ..adapters.imap_adapter import IMAPAdapter
from ..storage.database import DatabaseManager, EmailServerConfig
from ..utils.errors import AdapterError, get_user_friendly_message
from ..security.trust import TrustEvaluator
from ..gisimple.client import GISimpleClient
from ..gisimple.groups import GISimpleGroupsWidget
from ..gisimple.uploads import GISimpleUploadsWidget


class EmailFetchWorker(QObject):
    """Worker for fetching emails in a background thread."""

    finished = pyqtSignal()
    error = pyqtSignal(str, str)          # message, details
    connected = pyqtSignal()
    mailboxesLoaded = pyqtSignal(list)
    messagesLoaded = pyqtSignal(list)
    messageLoaded = pyqtSignal(object)    # MessageMetadata

    def __init__(self, adapter: EmailAdapter):
        super().__init__()
        self._adapter = adapter
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def connect_server(self, config: AdapterConfig):
        """Connect to the email server."""
        try:
            if self._cancelled:
                return
            self._adapter.connect(config)
            if not self._cancelled:
                self.connected.emit()
        except AdapterError as e:
            self.error.emit(get_user_friendly_message(e.code), e.details or '')
        except Exception as e:
            self.error.emit("Connection failed", str(e))
        finally:
            self.finished.emit()

    def fetch_mailboxes(self):
        """Fetch mailbox list."""
        try:
            if self._cancelled:
                return
            mailboxes = self._adapter.list_mailboxes()
            if not self._cancelled:
                self.mailboxesLoaded.emit([mb.to_dict() for mb in mailboxes])
        except AdapterError as e:
            self.error.emit(get_user_friendly_message(e.code), e.details or '')
        except Exception as e:
            self.error.emit("Failed to fetch mailboxes", str(e))
        finally:
            self.finished.emit()

    def fetch_messages(self, mailbox: str, filter_read: Optional[bool] = None):
        """Fetch messages from a mailbox."""
        try:
            if self._cancelled:
                return
            messages = self._adapter.list_messages(mailbox, filter_read=filter_read)
            if not self._cancelled:
                self.messagesLoaded.emit([msg.to_dict() for msg in messages])
        except AdapterError as e:
            self.error.emit(get_user_friendly_message(e.code), e.details or '')
        except Exception as e:
            self.error.emit("Failed to fetch messages", str(e))
        finally:
            self.finished.emit()

    def fetch_message(self, message_id: str):
        """Fetch a single message with body."""
        try:
            if self._cancelled:
                return
            message = self._adapter.fetch_message(message_id)
            if not self._cancelled:
                self.messageLoaded.emit(message)
        except AdapterError as e:
            self.error.emit(get_user_friendly_message(e.code), e.details or '')
        except Exception as e:
            self.error.emit("Failed to fetch message", str(e))
        finally:
            self.finished.emit()

    def search_messages(self, mailbox: str, query: str):
        """Search messages in a mailbox."""
        try:
            if self._cancelled:
                return
            messages = self._adapter.search_messages(mailbox, query)
            if not self._cancelled:
                self.messagesLoaded.emit([msg.to_dict() for msg in messages])
        except AdapterError as e:
            self.error.emit(get_user_friendly_message(e.code), e.details or '')
        except Exception as e:
            self.error.emit("Failed to search messages", str(e))
        finally:
            self.finished.emit()


class EmailBrowserPanel(QDockWidget):
    """Dockable panel for browsing emails."""

    def __init__(self, iface, parent: Optional[QWidget] = None):
        super().__init__("GeoInbox — Email Browser", parent)
        self.iface = iface
        self._adapter: Optional[EmailAdapter] = None
        self._db = DatabaseManager()
        self._current_mailbox: Optional[str] = None
        self._current_message: Optional[MessageMetadata] = None
        self._current_message_id: Optional[str] = None
        self._worker: Optional[EmailFetchWorker] = None
        self._thread: Optional[QThread] = None
        self._trust_evaluator = TrustEvaluator(self._db)
        self._gisimple_client = GISimpleClient(self._db)

        self._setup_ui()
        self._load_servers()

    # ------------------------------------------------------------------ #
    #  UI setup                                                            #
    # ------------------------------------------------------------------ #

    def _setup_ui(self):
        main_widget = QWidget()
        self.setWidget(main_widget)

        layout = QVBoxLayout(main_widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        layout.addWidget(self._create_toolbar())

        self._progress = ProgressWidget()
        self._progress.setVisible(False)
        self._progress.cancelled.connect(self._cancel_operation)
        layout.addWidget(self._progress)

        self._tab_widget = QTabWidget()
        self._tab_widget.addTab(self._create_email_tab(), "Email")

        self._groups_widget = GISimpleGroupsWidget(self._gisimple_client)
        self._uploads_widget = GISimpleUploadsWidget(self._gisimple_client)

        if self._db.get_setting('gisimple_enabled', 'false') == 'true':
            self._tab_widget.addTab(self._groups_widget, "GISimple Groups")
            self._tab_widget.addTab(self._uploads_widget, "GISimple Uploads")

        layout.addWidget(self._tab_widget)
        layout.addWidget(self._create_status_bar())

    def _create_email_tab(self) -> QWidget:
        email_widget = QWidget()
        email_layout = QVBoxLayout(email_widget)
        email_layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._create_mailbox_panel())
        splitter.addWidget(self._create_message_list_panel())
        splitter.addWidget(self._create_preview_panel())
        splitter.setSizes([150, 400, 300])

        email_layout.addWidget(splitter)
        return email_widget

    def _create_toolbar(self) -> QToolBar:
        toolbar = QToolBar()
        toolbar.setMovable(False)

        toolbar.addWidget(QLabel("Server: "))
        self._server_combo = QComboBox()
        self._server_combo.setMinimumWidth(150)
        self._server_combo.currentIndexChanged.connect(self._on_server_changed)
        toolbar.addWidget(self._server_combo)

        toolbar.addSeparator()

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._connect_btn.clicked.connect(self._toggle_connection)
        toolbar.addWidget(self._connect_btn)

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setToolTip("Reload mailboxes and messages")
        self._refresh_btn.clicked.connect(self._refresh)
        toolbar.addWidget(self._refresh_btn)

        toolbar.addSeparator()

        settings_btn = QPushButton("Settings")
        settings_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        settings_btn.clicked.connect(self._show_settings)
        toolbar.addWidget(settings_btn)

        return toolbar

    def _create_mailbox_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameStyle(QFrame.Shape.StyledPanel)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(QLabel("Mailboxes"))

        self._mailbox_list = MailboxList()
        self._mailbox_list.mailboxSelected.connect(self._on_mailbox_selected)
        layout.addWidget(self._mailbox_list)

        return panel

    def _create_message_list_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameStyle(QFrame.Shape.StyledPanel)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        filter_row = QHBoxLayout()

        self._search_box = SearchBox("Search messages...")
        self._search_box.searchSubmitted.connect(self._on_search)
        filter_row.addWidget(self._search_box)

        self._read_filter = FilterComboBox()
        self._read_filter.set_options([('Read', True), ('Unread', False)], include_all=True)
        self._read_filter.filterChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self._read_filter)

        layout.addLayout(filter_row)

        self._message_table = MessageListTable()
        self._message_table.messageSelected.connect(self._on_message_selected)
        self._message_table.messageDoubleClicked.connect(self._on_message_double_clicked)
        layout.addWidget(self._message_table)

        return panel

    def _create_preview_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameStyle(QFrame.Shape.StyledPanel)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._preview_header = QLabel("Select a message")
        self._preview_header.setWordWrap(True)
        layout.addWidget(self._preview_header)

        self._trust_badge = TrustBadge()
        self._trust_badge.setVisible(False)
        layout.addWidget(self._trust_badge)

        self._body_preview = QTextBrowser()
        self._body_preview.setOpenExternalLinks(False)
        self._body_preview.setMinimumHeight(150)
        layout.addWidget(self._body_preview)

        layout.addWidget(QLabel("Attachments"))

        self._attachments_scroll = QScrollArea()
        self._attachments_scroll.setWidgetResizable(True)
        self._attachments_scroll.setMaximumHeight(200)

        self._attachments_container = QWidget()
        self._attachments_layout = QVBoxLayout(self._attachments_container)
        self._attachments_layout.setContentsMargins(0, 0, 0, 0)
        self._attachments_layout.setSpacing(2)
        self._attachments_scroll.setWidget(self._attachments_container)
        layout.addWidget(self._attachments_scroll)

        btn_row = QHBoxLayout()
        btn_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self._download_all_btn = QPushButton("Download All")
        self._download_all_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._download_all_btn.setEnabled(False)
        self._download_all_btn.clicked.connect(self._download_all_attachments)
        btn_row.addWidget(self._download_all_btn)

        self._mark_read_btn = QPushButton("Mark Read")
        self._mark_read_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._mark_read_btn.setEnabled(False)
        self._mark_read_btn.clicked.connect(self._mark_as_read)
        btn_row.addWidget(self._mark_read_btn)

        layout.addLayout(btn_row)
        return panel

    def _create_status_bar(self) -> QWidget:
        status = QFrame()
        status.setFrameStyle(QFrame.Shape.StyledPanel)

        layout = QHBoxLayout(status)
        layout.setContentsMargins(4, 2, 4, 2)

        self._status_indicator = StatusIndicator()
        layout.addWidget(self._status_indicator)
        layout.addStretch()

        self._message_count_label = QLabel("")
        layout.addWidget(self._message_count_label)

        return status

    # ------------------------------------------------------------------ #
    #  Server list                                                         #
    # ------------------------------------------------------------------ #

    def _load_servers(self):
        """Reload server combo without triggering spurious signals."""
        self._server_combo.blockSignals(True)
        self._server_combo.clear()
        servers = self._db.get_email_servers()
        if not servers:
            self._server_combo.addItem("No servers configured", None)
        else:
            for server in servers:
                self._server_combo.addItem(server.name, server.id)
        self._server_combo.blockSignals(False)

    def _on_server_changed(self, index: int):
        if self._adapter and self._adapter.is_connected:
            self._disconnect()

    # ------------------------------------------------------------------ #
    #  Connection management                                               #
    # ------------------------------------------------------------------ #

    def _toggle_connection(self):
        if self._adapter and self._adapter.is_connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        """Start a background connection to the selected server."""
        server_id = self._server_combo.currentData()
        if not server_id:
            show_error_dialog(self, "Error", "No server selected")
            return

        server = self._db.get_email_server(server_id)
        if not server:
            show_error_dialog(self, "Error", "Server configuration not found")
            return

        from ..security.credentials import get_credential
        password = get_credential(server.credential_key)
        if not password:
            show_error_dialog(
                self, "Error",
                "Password not found. Please configure the server in Settings."
            )
            return

        if server.protocol == 'imap':
            self._adapter = IMAPAdapter()
        else:
            show_error_dialog(self, "Error", f"Protocol not supported: {server.protocol}")
            return

        config = AdapterConfig(
            host=server.host,
            port=server.port,
            username=server.username,
            password=password,
            use_ssl=server.use_ssl,
            use_tls=server.use_tls
        )

        self._status_indicator.set_status('connecting')
        self._connect_btn.setEnabled(False)
        self._progress.set_label("Connecting...")
        self._progress.set_indeterminate(True)
        self._progress.setVisible(True)

        self._start_worker(
            lambda w: w.connect_server(config),
            on_connected=self._on_connected,
            on_error=self._on_connect_error
        )

    def _on_connected(self):
        """Called when the server connection succeeds."""
        self._status_indicator.set_status('connected')
        self._connect_btn.setText("Disconnect")
        self._connect_btn.setEnabled(True)
        self._refresh_btn.setEnabled(True)
        self._progress.setVisible(False)
        # Defer mailbox fetch so the connect worker finishes cleanly first
        QTimer.singleShot(0, self._fetch_mailboxes)

    def _on_connect_error(self, message: str, details: str):
        """Called when the connection attempt fails."""
        self._adapter = None
        self._status_indicator.set_status('error')
        self._connect_btn.setEnabled(True)
        self._on_fetch_error(message, details)

    def _disconnect(self):
        """Stop any running operations and disconnect from the server."""
        self._stop_worker()

        if self._adapter:
            self._adapter.disconnect()
            self._adapter = None

        self._status_indicator.set_status('disconnected')
        self._connect_btn.setText("Connect")
        self._connect_btn.setEnabled(True)
        self._refresh_btn.setEnabled(False)
        self._current_mailbox = None
        self._current_message_id = None
        self._mailbox_list.set_mailboxes([])
        self._message_table.set_messages([])
        self._message_count_label.setText("")
        self._clear_preview()

    # ------------------------------------------------------------------ #
    #  Worker / thread management                                          #
    # ------------------------------------------------------------------ #

    def _start_worker(
        self,
        operation_fn,
        *,
        on_connected=None,
        on_mailboxes=None,
        on_messages=None,
        on_message=None,
        on_error=None
    ):
        """
        Stop any existing operation, create a new worker/thread pair, wire
        callbacks, and start it.  The cleanup closure captures the specific
        thread/worker objects so it is immune to self._thread being replaced
        before it runs.
        """
        self._stop_worker()

        thread = QThread()
        worker = EmailFetchWorker(self._adapter)
        worker.moveToThread(thread)

        self._thread = thread
        self._worker = worker

        if on_connected:
            worker.connected.connect(on_connected)
        if on_mailboxes:
            worker.mailboxesLoaded.connect(on_mailboxes)
        if on_messages:
            worker.messagesLoaded.connect(on_messages)
        if on_message:
            worker.messageLoaded.connect(on_message)

        worker.error.connect(on_error or self._on_fetch_error)

        # Closure captures local thread/worker references — safe even if
        # self._thread or self._worker have already been replaced.
        def _finish(t=thread):
            t.quit()
            t.wait()

        worker.finished.connect(_finish)
        thread.started.connect(lambda: operation_fn(worker))
        thread.start()

    def _stop_worker(self):
        """
        Cancel the running worker, disconnect all its signals, then quit and
        wait for (or terminate) the thread.  Setting self._thread / self._worker
        to None prevents _finish closures from orphaned workers touching them.
        """
        if self._worker is not None:
            self._worker.cancel()
            for sig in (
                self._worker.connected,
                self._worker.mailboxesLoaded,
                self._worker.messagesLoaded,
                self._worker.messageLoaded,
                self._worker.error,
                self._worker.finished,
            ):
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass

        if self._thread is not None:
            self._thread.quit()
            if not self._thread.wait(2000):
                self._thread.terminate()
                self._thread.wait()
            self._thread = None

        self._worker = None

    # ------------------------------------------------------------------ #
    #  Fetch operations                                                    #
    # ------------------------------------------------------------------ #

    def _fetch_mailboxes(self):
        if not self._adapter:
            return
        self._progress.set_label("Loading mailboxes...")
        self._progress.set_indeterminate(True)
        self._progress.setVisible(True)
        self._start_worker(
            lambda w: w.fetch_mailboxes(),
            on_mailboxes=self._on_mailboxes_loaded
        )

    def _on_mailboxes_loaded(self, mailboxes: List[Dict[str, Any]]):
        self._progress.setVisible(False)
        self._mailbox_list.set_mailboxes(mailboxes)
        # Defer INBOX auto-select so the fetch worker finishes cleanly first
        for mb in mailboxes:
            if mb.get('name', '').upper() == 'INBOX':
                QTimer.singleShot(0, lambda: self._on_mailbox_selected('INBOX'))
                break

    def _on_mailbox_selected(self, mailbox: str):
        self._current_mailbox = mailbox
        self._mailbox_list.highlight_mailbox(mailbox)
        self._fetch_messages(mailbox)

    def _fetch_messages(self, mailbox: str):
        if not self._adapter:
            return
        self._progress.set_label(f"Loading messages from {mailbox}...")
        self._progress.set_indeterminate(True)
        self._progress.setVisible(True)
        filter_read = self._read_filter.get_value()
        self._start_worker(
            lambda w: w.fetch_messages(mailbox, filter_read),
            on_messages=self._on_messages_loaded
        )

    def _on_messages_loaded(self, messages: List[Dict[str, Any]]):
        self._progress.setVisible(False)
        self._message_table.set_messages(messages)
        n = len(messages)
        self._message_count_label.setText(f"{n} message{'s' if n != 1 else ''}")

    def _on_message_selected(self, message_id: str):
        """Fetch a message only if it is not the one already displayed."""
        if not self._adapter:
            return
        if message_id == self._current_message_id:
            return
        self._progress.set_label("Loading message...")
        self._progress.set_indeterminate(True)
        self._progress.setVisible(True)
        self._start_worker(
            lambda w: w.fetch_message(message_id),
            on_message=self._on_message_loaded
        )

    def _on_message_loaded(self, message: MessageMetadata):
        self._progress.setVisible(False)
        self._current_message = message
        self._current_message_id = message.message_id
        self._update_preview(message)

    def _on_message_double_clicked(self, message_id: str):
        """Double-click: open detail only if different from the loaded message."""
        if message_id != self._current_message_id:
            self._on_message_selected(message_id)

    # ------------------------------------------------------------------ #
    #  Preview panel                                                       #
    # ------------------------------------------------------------------ #

    def _update_preview(self, message: MessageMetadata):
        self._preview_header.setText(
            f"From: {message.from_address}\nSubject: {message.subject}"
        )

        is_trusted = self._trust_evaluator.is_trusted(message.from_address)
        self._trust_badge.set_trusted(is_trusted)
        self._trust_badge.setVisible(True)

        if message.body_html:
            self._body_preview.setHtml(self._sanitize_html(message.body_html))
        else:
            self._body_preview.setPlainText(message.body_preview)

        # Replace attachment widgets
        while self._attachments_layout.count():
            item = self._attachments_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for att in message.attachments:
            widget = AttachmentItem(
                att.attachment_id, att.filename, att.size, att.content_type
            )
            widget.downloadClicked.connect(self._download_attachment)
            widget.openClicked.connect(self._download_and_open_attachment)
            self._attachments_layout.addWidget(widget)

        self._download_all_btn.setEnabled(bool(message.attachments))
        self._mark_read_btn.setEnabled(True)
        self._mark_read_btn.setText("Mark Unread" if message.is_read else "Mark Read")

    def _sanitize_html(self, html: str) -> str:
        import re
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'\s+on\w+\s*=\s*["\'][^"\']*["\']', '', html, flags=re.IGNORECASE)
        html = re.sub(r'href\s*=\s*["\']javascript:[^"\']*["\']', 'href="#"', html, flags=re.IGNORECASE)
        return html

    def _clear_preview(self):
        self._preview_header.setText("Select a message")
        self._trust_badge.setVisible(False)
        self._body_preview.clear()
        self._current_message = None
        self._current_message_id = None

        while self._attachments_layout.count():
            item = self._attachments_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._download_all_btn.setEnabled(False)
        self._mark_read_btn.setEnabled(False)

    # ------------------------------------------------------------------ #
    #  Actions                                                             #
    # ------------------------------------------------------------------ #

    def _download_attachment(self, attachment_id: str):
        if not self._current_message or not self._adapter:
            return
        show_info_dialog(self, "Download", f"Downloading attachment {attachment_id}...")

    def _download_and_open_attachment(self, attachment_id: str):
        if not self._current_message or not self._adapter:
            return
        # TODO: implement download + open in QGIS

    def _download_all_attachments(self):
        if not self._current_message:
            return
        # TODO: implement batch download

    def _mark_as_read(self):
        if not self._current_message or not self._adapter:
            return
        try:
            new_status = not self._current_message.is_read
            self._adapter.mark_as_read(self._current_message.message_id, new_status)
            self._current_message.is_read = new_status
            self._mark_read_btn.setText("Mark Unread" if new_status else "Mark Read")
            if self._current_mailbox:
                self._fetch_messages(self._current_mailbox)
        except AdapterError as e:
            show_error_dialog(self, "Error", get_user_friendly_message(e.code), e.details)

    def _refresh(self):
        """Reload mailboxes (and auto-reload current mailbox messages)."""
        if self._adapter and self._adapter.is_connected:
            self._trust_evaluator.reload_rules()
            self._fetch_mailboxes()

    def _on_search(self, query: str):
        if not self._adapter or not self._current_mailbox:
            return
        if not query:
            self._fetch_messages(self._current_mailbox)
            return
        if not self._adapter.supports_search():
            return

        self._progress.set_label(f"Searching for '{query}'...")
        self._progress.set_indeterminate(True)
        self._progress.setVisible(True)

        def on_results(messages):
            self._progress.setVisible(False)
            self._message_table.set_messages(messages)
            n = len(messages)
            self._message_count_label.setText(f"{n} result{'s' if n != 1 else ''}")

        self._start_worker(
            lambda w: w.search_messages(self._current_mailbox, query),
            on_messages=on_results
        )

    def _on_filter_changed(self, value):
        if self._current_mailbox:
            self._fetch_messages(self._current_mailbox)

    def _on_fetch_error(self, message: str, details: str):
        self._progress.setVisible(False)
        show_error_dialog(self, "Error", message, details if details else None)

    def _cancel_operation(self):
        """Cancel the current background operation and hide the progress bar."""
        self._stop_worker()
        self._progress.setVisible(False)

    # ------------------------------------------------------------------ #
    #  Settings                                                            #
    # ------------------------------------------------------------------ #

    def _show_settings(self):
        from .settings_dialog import SettingsDialog
        dialog = SettingsDialog(self)
        if dialog.exec():
            self._trust_evaluator.reload_rules()
            self._load_servers()
            self._sync_gisimple_tabs()

    def _sync_gisimple_tabs(self):
        """Add or remove GISimple tabs to match the current enabled setting."""
        enabled = self._db.get_setting('gisimple_enabled', 'false') == 'true'
        groups_idx = self._tab_widget.indexOf(self._groups_widget)
        uploads_idx = self._tab_widget.indexOf(self._uploads_widget)

        if enabled:
            if groups_idx == -1:
                self._tab_widget.addTab(self._groups_widget, "GISimple Groups")
            if self._tab_widget.indexOf(self._uploads_widget) == -1:
                self._tab_widget.addTab(self._uploads_widget, "GISimple Uploads")
        else:
            # Remove in reverse order so indices don't shift mid-removal
            for widget in (self._uploads_widget, self._groups_widget):
                idx = self._tab_widget.indexOf(widget)
                if idx != -1:
                    self._tab_widget.removeTab(idx)

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def closeEvent(self, event):
        self._disconnect()
        super().closeEvent(event)
