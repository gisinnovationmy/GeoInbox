"""
GeoInbox - Database Manager

SQLite database manager for standalone mode configuration storage.
Handles server configs, trust rules, settings, message cache, and version records.
"""

import sqlite3
import json
import sys
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

# Add plugin directory to path
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from storage.sanitizer import get_plugin_data_dir


DATABASE_VERSION = 2
DATABASE_FILENAME = 'config.db'


def get_database_path() -> Path:
    """Get the path to the SQLite database file."""
    return get_plugin_data_dir() / DATABASE_FILENAME


def database_exists() -> bool:
    """Return True if the database file is present on disk."""
    return get_database_path().exists()


@contextmanager
def get_connection():
    """
    Context manager for database connections.
    
    Yields:
        sqlite3.Connection with row factory set
    """
    conn = sqlite3.connect(str(get_database_path()))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _apply_schema() -> None:
    """Create/migrate tables in an already-open database file."""
    with get_connection() as conn:
        cursor = conn.cursor()
        
        # Check if database is already initialized
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='settings'
        """)
        existing = cursor.fetchone()
        if existing:
            # Check version
            cursor.execute("SELECT value FROM settings WHERE key='db_version'")
            row = cursor.fetchone()
            current_version = int(row['value']) if row and row['value'] else 1
            if current_version >= DATABASE_VERSION:
                return  # Already up to date
        
        # Create tables
        cursor.executescript("""
            -- Server configurations
            CREATE TABLE IF NOT EXISTS email_servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                protocol TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                use_ssl INTEGER DEFAULT 1,
                use_tls INTEGER DEFAULT 0,
                username TEXT,
                password TEXT,
                credential_key TEXT,
                is_default INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );

            -- Trust rules
            CREATE TABLE IF NOT EXISTS trust_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_type TEXT NOT NULL,
                value TEXT NOT NULL,
                enabled INTEGER DEFAULT 1
            );

            -- Settings
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            -- Message cache
            CREATE TABLE IF NOT EXISTS message_cache (
                message_id TEXT PRIMARY KEY,
                server_id INTEGER,
                mailbox TEXT,
                from_address TEXT,
                subject TEXT,
                date TEXT,
                size INTEGER,
                is_read INTEGER,
                is_trusted INTEGER,
                attachments_json TEXT,
                cached_at TEXT,
                FOREIGN KEY (server_id) REFERENCES email_servers(id)
            );

            -- Version history
            CREATE TABLE IF NOT EXISTS version_records (
                version_id TEXT PRIMARY KEY,
                timestamp TEXT,
                user TEXT,
                client_source TEXT,
                host_layer TEXT,
                host_type TEXT,
                host_path TEXT,
                operation_type TEXT,
                feature_mappings_json TEXT,
                field_mappings_json TEXT,
                diff_summary_json TEXT,
                status TEXT,
                backup_reference TEXT,
                undo_of TEXT
            );

            -- Create indexes
            CREATE INDEX IF NOT EXISTS idx_message_cache_server 
                ON message_cache(server_id);
            CREATE INDEX IF NOT EXISTS idx_message_cache_mailbox 
                ON message_cache(mailbox);
            CREATE INDEX IF NOT EXISTS idx_message_cache_date 
                ON message_cache(date);
            CREATE INDEX IF NOT EXISTS idx_version_records_timestamp 
                ON version_records(timestamp);
            CREATE INDEX IF NOT EXISTS idx_version_records_host_layer 
                ON version_records(host_layer);
        """)
        
        # Set database version
        # Migration: add password column if upgrading from v1
        if existing and current_version < 2:
            try:
                cursor.execute("ALTER TABLE email_servers ADD COLUMN password TEXT")
            except sqlite3.OperationalError:
                pass

        cursor.execute("""
            INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', ?)
        """, (str(DATABASE_VERSION),))
        
        conn.commit()


def init_database() -> None:
    """Update schema on an existing database. No-op when the DB file is absent."""
    if not database_exists():
        return
    _apply_schema()


def create_database() -> None:
    """Create the database file and full schema unconditionally."""
    db_path = get_database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _apply_schema()


def prompt_create_database(parent=None) -> bool:
    """
    Show a cross-platform dialog informing the user about the database path and
    asking whether to create it.  If the file already exists the user is warned
    that it will be overwritten.  Returns True when the database was created,
    False when the user cancelled.
    """
    from qgis.PyQt.QtWidgets import QMessageBox

    db_path = get_database_path()
    exists = db_path.exists()

    if exists:
        reply = QMessageBox.warning(
            parent,
            "Overwrite Settings Database",
            (
                "A settings database already exists at:\n\n"
                f"{db_path}\n\n"
                "Recreating it will permanently delete all stored settings, "
                "email server configurations, and cached data.\n\n"
                "Do you want to continue?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False
        db_path.unlink()
    else:
        reply = QMessageBox.question(
            parent,
            "Create Settings Database",
            (
                "GeoInbox needs to create a settings database at:\n\n"
                f"{db_path}\n\n"
                "Do you want to create it now?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False

    create_database()
    return True


class EmailServerConfig:
    """Email server configuration data class."""
    
    def __init__(
        self,
        id: Optional[int] = None,
        name: str = '',
        protocol: str = 'imap',
        host: str = '',
        port: int = 993,
        use_ssl: bool = True,
        use_tls: bool = False,
        username: str = '',
        password: Optional[str] = None,
        credential_key: str = '',
        is_default: bool = False,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None
    ):
        self.id = id
        self.name = name
        self.protocol = protocol
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.use_tls = use_tls
        self.username = username
        self.password = password or ''
        self.credential_key = credential_key
        self.is_default = is_default
        self.created_at = created_at
        self.updated_at = updated_at

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'EmailServerConfig':
        """Create from database row."""
        return cls(
            id=row['id'],
            name=row['name'],
            protocol=row['protocol'],
            host=row['host'],
            port=row['port'],
            use_ssl=bool(row['use_ssl']),
            use_tls=bool(row['use_tls']),
            username=row['username'],
            password=row['password'],
            credential_key=row['credential_key'],
            is_default=bool(row['is_default']),
            created_at=row['created_at'],
            updated_at=row['updated_at']
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'id': self.id,
            'name': self.name,
            'protocol': self.protocol,
            'host': self.host,
            'port': self.port,
            'use_ssl': self.use_ssl,
            'use_tls': self.use_tls,
            'username': self.username,
            'password': self.password,
            'credential_key': self.credential_key,
            'is_default': self.is_default,
            'created_at': self.created_at,
            'updated_at': self.updated_at
        }


class TrustRule:
    """Trust rule data class."""
    
    def __init__(
        self,
        id: Optional[int] = None,
        rule_type: str = 'email',
        value: str = '',
        enabled: bool = True
    ):
        self.id = id
        self.rule_type = rule_type
        self.value = value
        self.enabled = enabled

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'TrustRule':
        """Create from database row."""
        return cls(
            id=row['id'],
            rule_type=row['rule_type'],
            value=row['value'],
            enabled=bool(row['enabled'])
        )


class DatabaseManager:
    """Manager class for all database operations."""

    def __init__(self):
        if not database_exists():
            raise RuntimeError(
                f"GeoInbox settings database not found at:\n{get_database_path()}\n"
                "Please create the database from Settings."
            )
        init_database()

    # ─── Email Server Operations ───────────────────────────────────────────

    def get_email_servers(self) -> List[EmailServerConfig]:
        """Get all email server configurations."""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM email_servers ORDER BY name")
            return [EmailServerConfig.from_row(row) for row in cursor.fetchall()]

    def get_email_server(self, server_id: int) -> Optional[EmailServerConfig]:
        """Get a specific email server configuration."""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM email_servers WHERE id = ?", (server_id,))
            row = cursor.fetchone()
            return EmailServerConfig.from_row(row) if row else None

    def get_default_email_server(self) -> Optional[EmailServerConfig]:
        """Get the default email server configuration."""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM email_servers WHERE is_default = 1 LIMIT 1")
            row = cursor.fetchone()
            return EmailServerConfig.from_row(row) if row else None

    def save_email_server(self, config: EmailServerConfig) -> int:
        """
        Save an email server configuration.
        
        Args:
            config: Server configuration to save
            
        Returns:
            Server ID
        """
        now = datetime.now().isoformat()
        
        with get_connection() as conn:
            cursor = conn.cursor()
            
            # If setting as default, clear other defaults
            if config.is_default:
                cursor.execute("UPDATE email_servers SET is_default = 0")
            
            if config.id:
                # Update existing
                cursor.execute("""
                    UPDATE email_servers SET
                        name = ?, protocol = ?, host = ?, port = ?,
                        use_ssl = ?, use_tls = ?, username = ?, password = ?,
                        credential_key = ?, is_default = ?, updated_at = ?
                    WHERE id = ?
                """, (
                    config.name, config.protocol, config.host, config.port,
                    int(config.use_ssl), int(config.use_tls), config.username, config.password,
                    config.credential_key, int(config.is_default), now,
                    config.id
                ))
                server_id = config.id
            else:
                # Insert new
                cursor.execute("""
                    INSERT INTO email_servers (
                        name, protocol, host, port, use_ssl, use_tls,
                        username, password, credential_key, is_default, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    config.name, config.protocol, config.host, config.port,
                    int(config.use_ssl), int(config.use_tls), config.username,
                    config.password, config.credential_key, int(config.is_default), now, now
                ))
                server_id = cursor.lastrowid
            
            conn.commit()
            return server_id

    def delete_email_server(self, server_id: int) -> None:
        """Delete an email server configuration."""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM email_servers WHERE id = ?", (server_id,))
            # Also delete cached messages for this server
            cursor.execute("DELETE FROM message_cache WHERE server_id = ?", (server_id,))
            conn.commit()

    # ─── Trust Rule Operations ─────────────────────────────────────────────

    def get_trust_rules(self, rule_type: Optional[str] = None) -> List[TrustRule]:
        """Get trust rules, optionally filtered by type."""
        with get_connection() as conn:
            cursor = conn.cursor()
            if rule_type:
                cursor.execute(
                    "SELECT * FROM trust_rules WHERE rule_type = ? ORDER BY value",
                    (rule_type,)
                )
            else:
                cursor.execute("SELECT * FROM trust_rules ORDER BY rule_type, value")
            return [TrustRule.from_row(row) for row in cursor.fetchall()]

    def save_trust_rule(self, rule: TrustRule) -> int:
        """Save a trust rule."""
        with get_connection() as conn:
            cursor = conn.cursor()
            if rule.id:
                cursor.execute("""
                    UPDATE trust_rules SET rule_type = ?, value = ?, enabled = ?
                    WHERE id = ?
                """, (rule.rule_type, rule.value, int(rule.enabled), rule.id))
                rule_id = rule.id
            else:
                cursor.execute("""
                    INSERT INTO trust_rules (rule_type, value, enabled)
                    VALUES (?, ?, ?)
                """, (rule.rule_type, rule.value, int(rule.enabled)))
                rule_id = cursor.lastrowid
            conn.commit()
            return rule_id

    def delete_trust_rule(self, rule_id: int) -> None:
        """Delete a trust rule."""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM trust_rules WHERE id = ?", (rule_id,))
            conn.commit()

    # ─── Settings Operations ───────────────────────────────────────────────

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a setting value."""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row['value'] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """Set a setting value."""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)
            """, (key, value))
            conn.commit()

    def get_settings_dict(self) -> Dict[str, str]:
        """Get all settings as a dictionary."""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM settings")
            return {row['key']: row['value'] for row in cursor.fetchall()}

    # ─── Message Cache Operations ──────────────────────────────────────────

    def cache_message(
        self,
        message_id: str,
        server_id: int,
        mailbox: str,
        from_address: str,
        subject: str,
        date: str,
        size: int,
        is_read: bool,
        is_trusted: bool,
        attachments: List[Dict[str, Any]]
    ) -> None:
        """Cache a message header."""
        now = datetime.now().isoformat()
        attachments_json = json.dumps(attachments)
        
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO message_cache (
                    message_id, server_id, mailbox, from_address, subject,
                    date, size, is_read, is_trusted, attachments_json, cached_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                message_id, server_id, mailbox, from_address, subject,
                date, size, int(is_read), int(is_trusted), attachments_json, now
            ))
            conn.commit()

    def get_cached_messages(
        self,
        server_id: int,
        mailbox: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get cached messages for a mailbox."""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM message_cache
                WHERE server_id = ? AND mailbox = ?
                ORDER BY date DESC
                LIMIT ?
            """, (server_id, mailbox, limit))
            
            messages = []
            for row in cursor.fetchall():
                msg = dict(row)
                msg['attachments'] = json.loads(msg['attachments_json'] or '[]')
                del msg['attachments_json']
                msg['is_read'] = bool(msg['is_read'])
                msg['is_trusted'] = bool(msg['is_trusted'])
                messages.append(msg)
            return messages

    def clear_message_cache(
        self,
        server_id: Optional[int] = None,
        older_than: Optional[datetime] = None
    ) -> int:
        """
        Clear message cache.
        
        Args:
            server_id: Optional server ID to clear cache for
            older_than: Optional datetime to clear messages older than
            
        Returns:
            Number of messages cleared
        """
        with get_connection() as conn:
            cursor = conn.cursor()
            
            conditions = []
            params = []
            
            if server_id is not None:
                conditions.append("server_id = ?")
                params.append(server_id)
            
            if older_than is not None:
                conditions.append("cached_at < ?")
                params.append(older_than.isoformat())
            
            if conditions:
                query = "DELETE FROM message_cache WHERE " + " AND ".join(conditions)  # nosec B608
            else:
                query = "DELETE FROM message_cache"
            
            cursor.execute(query, params)
            count = cursor.rowcount
            conn.commit()
            return count

    # ─── Version Record Operations ─────────────────────────────────────────

    def save_version_record(
        self,
        version_id: str,
        user: str,
        client_source: str,
        host_layer: str,
        host_type: str,
        host_path: str,
        operation_type: str,
        feature_mappings: Dict[str, Any],
        field_mappings: Dict[str, Any],
        diff_summary: Dict[str, Any],
        status: str,
        backup_reference: str,
        undo_of: Optional[str] = None
    ) -> None:
        """Save a version record."""
        now = datetime.now().isoformat()
        
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO version_records (
                    version_id, timestamp, user, client_source, host_layer,
                    host_type, host_path, operation_type, feature_mappings_json,
                    field_mappings_json, diff_summary_json, status,
                    backup_reference, undo_of
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                version_id, now, user, client_source, host_layer,
                host_type, host_path, operation_type,
                json.dumps(feature_mappings),
                json.dumps(field_mappings),
                json.dumps(diff_summary),
                status, backup_reference, undo_of
            ))
            conn.commit()

    def get_version_records(
        self,
        host_layer: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get version records, optionally filtered by host layer."""
        with get_connection() as conn:
            cursor = conn.cursor()
            
            if host_layer:
                cursor.execute("""
                    SELECT * FROM version_records
                    WHERE host_layer = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (host_layer, limit))
            else:
                cursor.execute("""
                    SELECT * FROM version_records
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (limit,))
            
            records = []
            for row in cursor.fetchall():
                record = dict(row)
                record['feature_mappings'] = json.loads(
                    record['feature_mappings_json'] or '{}'
                )
                record['field_mappings'] = json.loads(
                    record['field_mappings_json'] or '{}'
                )
                record['diff_summary'] = json.loads(
                    record['diff_summary_json'] or '{}'
                )
                del record['feature_mappings_json']
                del record['field_mappings_json']
                del record['diff_summary_json']
                records.append(record)
            return records

    def get_version_record(self, version_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific version record."""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM version_records WHERE version_id = ?",
                (version_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            
            record = dict(row)
            record['feature_mappings'] = json.loads(
                record['feature_mappings_json'] or '{}'
            )
            record['field_mappings'] = json.loads(
                record['field_mappings_json'] or '{}'
            )
            record['diff_summary'] = json.loads(
                record['diff_summary_json'] or '{}'
            )
            del record['feature_mappings_json']
            del record['field_mappings_json']
            del record['diff_summary_json']
            return record

    def update_version_status(self, version_id: str, status: str) -> None:
        """Update the status of a version record."""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE version_records SET status = ? WHERE version_id = ?
            """, (status, version_id))
            conn.commit()
