"""
GeoInbox - Settings Dialog

Dialog for configuring email servers, trust rules, and plugin settings.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QFormLayout, QLineEdit, QSpinBox, QCheckBox, QComboBox,
    QPushButton, QListWidget, QListWidgetItem, QGroupBox,
    QLabel, QFileDialog, QMessageBox, QInputDialog,
    QDialogButtonBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QSizePolicy
)
from qgis.PyQt.QtCore import Qt

from typing import Optional, List

from ..storage.database import (
    DatabaseManager, EmailServerConfig, TrustRule,
    get_database_path, prompt_create_database,
)
from ..security.credentials import credential_exists
from .widgets import show_error_dialog, show_warning_dialog, show_info_dialog


class ServerEditDialog(QDialog):
    """Dialog for editing an email server configuration."""
    
    def __init__(self, server: Optional[EmailServerConfig] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._server = server or EmailServerConfig()
        self._setup_ui()
        self._load_data()
    
    def _setup_ui(self):
        self.setWindowTitle("Edit Email Server" if self._server.id else "Add Email Server")
        self.setMinimumWidth(400)
        
        layout = QVBoxLayout(self)
        
        form = QFormLayout()
        
        # Name
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("My Email Server")
        form.addRow("Name:", self._name_edit)
        
        # Protocol
        self._protocol_combo = QComboBox()
        self._protocol_combo.addItem("IMAP", "imap")
        self._protocol_combo.addItem("POP3", "pop3")
        self._protocol_combo.addItem("EWS (Experimental)", "ews")
        self._protocol_combo.addItem("Microsoft Graph (Experimental)", "graph")
        self._protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        form.addRow("Protocol:", self._protocol_combo)
        
        # Host
        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText("imap.example.com")
        form.addRow("Host:", self._host_edit)
        
        # Port
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(993)
        form.addRow("Port:", self._port_spin)
        
        # SSL/TLS — mutually exclusive
        self._ssl_check = QCheckBox("Use SSL (port 993/995)")
        self._ssl_check.setChecked(True)
        form.addRow("", self._ssl_check)

        self._tls_check = QCheckBox("Use STARTTLS (port 143/110)")
        form.addRow("", self._tls_check)

        self._ssl_check.toggled.connect(
            lambda checked: self._tls_check.setChecked(False) if checked else None
        )
        self._tls_check.toggled.connect(
            lambda checked: self._ssl_check.setChecked(False) if checked else None
        )
        
        # Username
        self._username_edit = QLineEdit()
        self._username_edit.setPlaceholderText("user@example.com")
        form.addRow("Username:", self._username_edit)
        
        # Password with visibility toggle (eye icon inside textbox)
        self._password_edit = QLineEdit()
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_edit.setPlaceholderText("Enter password")
        self._show_password_action = self._password_edit.addAction(
            self._password_edit.style().standardIcon(self._password_edit.style().StandardPixmap.SP_DialogYesButton),
            QLineEdit.ActionPosition.TrailingPosition
        )
        self._show_password_action.setToolTip("Show/Hide password")
        self._show_password_action.triggered.connect(self._toggle_password_visibility)
        self._password_visible = False
        form.addRow("Password:", self._password_edit)
        
        # Default server
        self._default_check = QCheckBox("Set as default server")
        form.addRow("", self._default_check)
        
        layout.addLayout(form)
        
        # Buttons
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
    
    def _on_protocol_changed(self, index: int):
        """Update default port based on protocol."""
        protocol = self._protocol_combo.currentData()
        default_ports = {
            'imap': 993,
            'pop3': 995,
            'ews': 443,
            'graph': 443
        }
        self._port_spin.setValue(default_ports.get(protocol, 993))
    
    def _load_data(self):
        """Load server data into form."""
        if self._server.id:
            self._name_edit.setText(self._server.name)
            
            # Set protocol
            index = self._protocol_combo.findData(self._server.protocol)
            if index >= 0:
                self._protocol_combo.setCurrentIndex(index)
            
            self._host_edit.setText(self._server.host)
            self._port_spin.setValue(self._server.port)
            self._ssl_check.setChecked(self._server.use_ssl)
            self._tls_check.setChecked(self._server.use_tls)
            self._username_edit.setText(self._server.username)
            self._default_check.setChecked(self._server.is_default)
            
            # Password placeholder
            if self._server.password:
                self._password_edit.setPlaceholderText("(unchanged)")
    
    def _on_accept(self):
        """Validate and accept."""
        # Validate
        if not self._name_edit.text().strip():
            show_error_dialog(self, "Validation Error", "Name is required")
            return
        
        if not self._host_edit.text().strip():
            show_error_dialog(self, "Validation Error", "Host is required")
            return
        
        if not self._username_edit.text().strip():
            show_error_dialog(self, "Validation Error", "Username is required")
            return
        
        # Password required for new servers
        password = self._password_edit.text()
        if not self._server.id and not password:
            show_error_dialog(self, "Validation Error", "Password is required")
            return
        
        self.accept()
    
    def get_server(self) -> EmailServerConfig:
        """Get the configured server."""
        self._server.name = self._name_edit.text().strip()
        self._server.protocol = self._protocol_combo.currentData()
        self._server.host = self._host_edit.text().strip()
        self._server.port = self._port_spin.value()
        self._server.use_ssl = self._ssl_check.isChecked()
        self._server.use_tls = self._tls_check.isChecked()
        self._server.username = self._username_edit.text().strip()
        password = self._password_edit.text()
        if password:
            self._server.password = password
        self._server.is_default = self._default_check.isChecked()
        return self._server
    
    def get_password(self) -> Optional[str]:
        """Get the entered password (if any)."""
        password = self._password_edit.text()
        return password if password else None


class SettingsDialog(QDialog):
    """Main settings dialog."""
    
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._db = DatabaseManager()
        self._setup_ui()
        self._load_data()
    
    def _setup_ui(self):
        self.setWindowTitle("GeoInbox Settings")
        self.setMinimumSize(600, 500)
        
        layout = QVBoxLayout(self)
        
        # Tab widget
        tabs = QTabWidget()
        
        # Servers tab
        tabs.addTab(self._create_servers_tab(), "Email Servers")
        
        # GISimple tab
        tabs.addTab(self._create_gisimple_tab(), "GISimple")
        
        # Trust tab
        tabs.addTab(self._create_trust_tab(), "Trust Rules")
        
        # Storage tab
        tabs.addTab(self._create_storage_tab(), "Storage")
        
        # Cache tab
        tabs.addTab(self._create_cache_tab(), "Cache")
        
        layout.addWidget(tabs)
        
        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
    
    def _create_servers_tab(self) -> QWidget:
        """Create the email servers tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # Server list
        self._server_list = QListWidget()
        self._server_list.itemDoubleClicked.connect(self._edit_server)
        layout.addWidget(self._server_list)
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        
        add_btn = QPushButton("Add")
        add_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        add_btn.clicked.connect(self._add_server)
        btn_layout.addWidget(add_btn)
        
        edit_btn = QPushButton("Edit")
        edit_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        edit_btn.clicked.connect(self._edit_server)
        btn_layout.addWidget(edit_btn)
        
        delete_btn = QPushButton("Delete")
        delete_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        delete_btn.clicked.connect(self._delete_server)
        btn_layout.addWidget(delete_btn)
        
        layout.addLayout(btn_layout)
        
        return widget
    
    def _create_gisimple_tab(self) -> QWidget:
        """Create the GISimple configuration tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # GISimple server settings
        group = QGroupBox("GISimple Server")
        form = QFormLayout(group)
        
        self._gisimple_url_edit = QLineEdit()
        self._gisimple_url_edit.setPlaceholderText("https://gisimple.example.com")
        form.addRow("Server URL:", self._gisimple_url_edit)
        
        self._gisimple_enabled = QCheckBox("Enable GISimple integration")
        form.addRow("", self._gisimple_enabled)
        
        layout.addWidget(group)
        
        # Info label
        info = QLabel(
            "GISimple integration allows you to:\n"
            "• Authenticate using your GISimple account\n"
            "• Browse group-shared datasets\n"
            "• Automatically trust senders from your groups\n\n"
            "Note: All authentication is handled by the GISimple server."
        )
        info.setWordWrap(True)
        # info.setStyleSheet("color: #666; padding: 10px;")
        layout.addWidget(info)
        
        layout.addStretch()
        
        return widget
    
    def _create_trust_tab(self) -> QWidget:
        """Create the trust rules tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # Trust mode
        mode_group = QGroupBox("Trust Mode")
        mode_layout = QVBoxLayout(mode_group)
        
        self._trust_mode_combo = QComboBox()
        self._trust_mode_combo.addItem("Trusted Accounts Only", "accounts")
        self._trust_mode_combo.addItem("Network Trusted (CIDR)", "network")
        self._trust_mode_combo.addItem("Trust All", "all")
        mode_layout.addWidget(self._trust_mode_combo)
        
        mode_info = QLabel(
            "• Trusted Accounts: Only trust senders from GISimple or configured list\n"
            "• Network Trusted: Trust senders from specified IP ranges\n"
            "• Trust All: Trust all senders (not recommended)"
        )
        # mode_info.setStyleSheet("color: #666; font-size: 11px;")
        mode_layout.addWidget(mode_info)
        
        layout.addWidget(mode_group)
        
        # Trust rules table
        rules_group = QGroupBox("Trust Rules")
        rules_layout = QVBoxLayout(rules_group)
        
        self._trust_table = QTableWidget()
        self._trust_table.setColumnCount(3)
        self._trust_table.setHorizontalHeaderLabels(["Type", "Value", "Enabled"])
        self._trust_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        rules_layout.addWidget(self._trust_table)
        
        # Rule buttons
        rule_btn_layout = QHBoxLayout()
        rule_btn_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        
        add_rule_btn = QPushButton("Add Rule")
        add_rule_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        add_rule_btn.clicked.connect(self._add_trust_rule)
        rule_btn_layout.addWidget(add_rule_btn)
        
        delete_rule_btn = QPushButton("Delete Rule")
        delete_rule_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        delete_rule_btn.clicked.connect(self._delete_trust_rule)
        rule_btn_layout.addWidget(delete_rule_btn)
        
        rules_layout.addLayout(rule_btn_layout)
        layout.addWidget(rules_group)
        
        return widget
    
    def _create_storage_tab(self) -> QWidget:
        """Create the storage settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Settings Database
        db_group = QGroupBox("Settings Database")
        db_layout = QVBoxLayout(db_group)

        db_path_label = QLabel(str(get_database_path()))
        db_path_label.setWordWrap(True)
        db_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        db_layout.addWidget(db_path_label)

        db_btn_layout = QHBoxLayout()
        db_btn_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        create_db_btn = QPushButton("Create Settings Database")
        create_db_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        create_db_btn.clicked.connect(self._on_create_settings_database)
        db_btn_layout.addWidget(create_db_btn)
        db_layout.addLayout(db_btn_layout)

        layout.addWidget(db_group)

        # Download directory
        dir_group = QGroupBox("Download Directory")
        dir_layout = QHBoxLayout(dir_group)
        
        self._download_dir_edit = QLineEdit()
        self._download_dir_edit.setPlaceholderText("Default: QGIS plugin data directory")
        dir_layout.addWidget(self._download_dir_edit)
        
        browse_btn = QPushButton("Browse...")
        browse_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        browse_btn.clicked.connect(self._browse_download_dir)
        dir_layout.addWidget(browse_btn)
        
        layout.addWidget(dir_group)
        
        # Attachment settings
        att_group = QGroupBox("Attachment Settings")
        att_form = QFormLayout(att_group)
        
        self._max_size_spin = QSpinBox()
        self._max_size_spin.setRange(1, 1000)
        self._max_size_spin.setValue(50)
        self._max_size_spin.setSuffix(" MB")
        att_form.addRow("Max attachment size:", self._max_size_spin)
        
        self._auto_extract_check = QCheckBox("Automatically extract ZIP files")
        self._auto_extract_check.setChecked(True)
        att_form.addRow("", self._auto_extract_check)
        
        self._validate_shapefiles_check = QCheckBox("Validate shapefile completeness")
        self._validate_shapefiles_check.setChecked(True)
        att_form.addRow("", self._validate_shapefiles_check)
        
        layout.addWidget(att_group)
        
        layout.addStretch()
        
        return widget
    
    def _create_cache_tab(self) -> QWidget:
        """Create the cache settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # Cache settings
        cache_group = QGroupBox("Message Cache")
        cache_form = QFormLayout(cache_group)
        
        self._cache_ttl_spin = QSpinBox()
        self._cache_ttl_spin.setRange(1, 168)  # 1 hour to 1 week
        self._cache_ttl_spin.setValue(24)
        self._cache_ttl_spin.setSuffix(" hours")
        cache_form.addRow("Cache TTL:", self._cache_ttl_spin)
        
        self._auto_mark_read_check = QCheckBox("Auto-mark as read on download")
        cache_form.addRow("", self._auto_mark_read_check)
        
        layout.addWidget(cache_group)
        
        # Clear cache button
        clear_layout = QHBoxLayout()
        clear_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        clear_btn = QPushButton("Clear Message Cache")
        clear_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        clear_btn.clicked.connect(self._clear_cache)
        clear_layout.addWidget(clear_btn)
        layout.addLayout(clear_layout)
        
        layout.addStretch()
        
        return widget
    
    def _load_data(self):
        """Load settings data."""
        # Load servers
        self._refresh_server_list()
        
        # Load GISimple settings
        self._gisimple_url_edit.setText(
            self._db.get_setting('gisimple_url', '')
        )
        self._gisimple_enabled.setChecked(
            self._db.get_setting('gisimple_enabled', 'false') == 'true'
        )
        
        # Load trust settings
        trust_mode = self._db.get_setting('trust_mode', 'accounts')
        index = self._trust_mode_combo.findData(trust_mode)
        if index >= 0:
            self._trust_mode_combo.setCurrentIndex(index)
        
        self._refresh_trust_rules()
        
        # Load storage settings
        self._download_dir_edit.setText(
            self._db.get_setting('download_dir', '')
        )
        self._max_size_spin.setValue(
            int(self._db.get_setting('max_attachment_size', '50'))
        )
        self._auto_extract_check.setChecked(
            self._db.get_setting('auto_extract', 'true') == 'true'
        )
        self._validate_shapefiles_check.setChecked(
            self._db.get_setting('validate_shapefiles', 'true') == 'true'
        )
        
        # Load cache settings
        self._cache_ttl_spin.setValue(
            int(self._db.get_setting('cache_ttl', '24'))
        )
        self._auto_mark_read_check.setChecked(
            self._db.get_setting('auto_mark_read', 'false') == 'true'
        )
    
    def _refresh_server_list(self):
        """Refresh the server list."""
        self._server_list.clear()
        servers = self._db.get_email_servers()
        
        for server in servers:
            text = f"{server.name} ({server.protocol.upper()})"
            if server.is_default:
                text += " [Default]"
            
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, server.id)
            self._server_list.addItem(item)
    
    def _refresh_trust_rules(self):
        """Refresh the trust rules table."""
        self._trust_table.setRowCount(0)
        rules = self._db.get_trust_rules()
        
        for rule in rules:
            row = self._trust_table.rowCount()
            self._trust_table.insertRow(row)
            
            type_item = QTableWidgetItem(rule.rule_type)
            type_item.setData(Qt.ItemDataRole.UserRole, rule.id)
            self._trust_table.setItem(row, 0, type_item)
            
            self._trust_table.setItem(row, 1, QTableWidgetItem(rule.value))
            
            enabled_item = QTableWidgetItem("✓" if rule.enabled else "")
            enabled_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._trust_table.setItem(row, 2, enabled_item)
    
    def _add_server(self):
        """Add a new server."""
        dialog = ServerEditDialog(parent=self)
        if dialog.exec():
            server = dialog.get_server()
            password = dialog.get_password()
            
            if password:
                server.password = password
            
            # Save server
            self._db.save_email_server(server)
            self._refresh_server_list()
    
    def _edit_server(self, item: Optional[QListWidgetItem] = None):
        """Edit the selected server."""
        if item is None:
            item = self._server_list.currentItem()
        
        if not item:
            return
        
        server_id = item.data(Qt.ItemDataRole.UserRole)
        server = self._db.get_email_server(server_id)
        
        if not server:
            return
        
        dialog = ServerEditDialog(server, parent=self)
        if dialog.exec():
            updated_server = dialog.get_server()
            password = dialog.get_password()
            
            if password:
                updated_server.password = password
            
            # Save server
            self._db.save_email_server(updated_server)
            self._refresh_server_list()
    
    def _delete_server(self):
        """Delete the selected server."""
        item = self._server_list.currentItem()
        if not item:
            return
        
        if not show_warning_dialog(
            self,
            "Delete Server",
            "Are you sure you want to delete this server configuration?"
        ):
            return
        
        server_id = item.data(Qt.ItemDataRole.UserRole)
        server = self._db.get_email_server(server_id)
        
        # Passwords are stored in sqlite; no credential manager cleanup needed.
        
        self._db.delete_email_server(server_id)
        self._refresh_server_list()
    
    def _add_trust_rule(self):
        """Add a new trust rule."""
        # Get rule type
        rule_type, ok = QInputDialog.getItem(
            self,
            "Add Trust Rule",
            "Rule Type:",
            ["email", "domain", "cidr"],
            0,
            False
        )
        if not ok:
            return
        
        # Get value
        placeholders = {
            "email": "user@example.com",
            "domain": "example.com",
            "cidr": "192.168.1.0/24"
        }
        
        value, ok = QInputDialog.getText(
            self,
            "Add Trust Rule",
            f"Enter {rule_type}:",
            text=placeholders.get(rule_type, "")
        )
        if not ok or not value:
            return
        
        rule = TrustRule(rule_type=rule_type, value=value, enabled=True)
        self._db.save_trust_rule(rule)
        self._refresh_trust_rules()
    
    def _delete_trust_rule(self):
        """Delete the selected trust rule."""
        row = self._trust_table.currentRow()
        if row < 0:
            return
        
        item = self._trust_table.item(row, 0)
        if not item:
            return
        
        rule_id = item.data(Qt.ItemDataRole.UserRole)
        self._db.delete_trust_rule(rule_id)
        self._refresh_trust_rules()
    
    def _on_create_settings_database(self):
        """Handle 'Create Settings Database' button — warns on overwrite."""
        if prompt_create_database(self):
            show_info_dialog(
                self,
                "Database Created",
                "Settings database created successfully.",
            )
            self._load_data()

    def _browse_download_dir(self):
        """Browse for download directory."""
        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Download Directory"
        )
        if directory:
            self._download_dir_edit.setText(directory)
    
    def _clear_cache(self):
        """Clear the message cache."""
        if show_warning_dialog(
            self,
            "Clear Cache",
            "Are you sure you want to clear the message cache?"
        ):
            count = self._db.clear_message_cache()
            show_info_dialog(self, "Cache Cleared", f"Cleared {count} cached messages.")
    
    def _on_accept(self):
        """Save settings and close."""
        # Save GISimple settings
        self._db.set_setting('gisimple_url', self._gisimple_url_edit.text().strip())
        self._db.set_setting(
            'gisimple_enabled',
            'true' if self._gisimple_enabled.isChecked() else 'false'
        )
        
        # Save trust settings
        self._db.set_setting('trust_mode', self._trust_mode_combo.currentData())
        
        # Save storage settings
        self._db.set_setting('download_dir', self._download_dir_edit.text().strip())
        self._db.set_setting('max_attachment_size', str(self._max_size_spin.value()))
        self._db.set_setting(
            'auto_extract',
            'true' if self._auto_extract_check.isChecked() else 'false'
        )
        self._db.set_setting(
            'validate_shapefiles',
            'true' if self._validate_shapefiles_check.isChecked() else 'false'
        )
        
        # Save cache settings
        self._db.set_setting('cache_ttl', str(self._cache_ttl_spin.value()))
        self._db.set_setting(
            'auto_mark_read',
            'true' if self._auto_mark_read_check.isChecked() else 'false'
        )
        
        self.accept()
