"""
GeoInbox - Sanitizer

GISimple-compatible filename and path sanitization utilities.
Follows the same sanitization rules as GISimple server.
"""

import re
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

from qgis.core import QgsApplication


def sanitize_identifier(value: str) -> str:
    """
    GISimple-compatible sanitization for identifiers.
    
    Rules:
    - Trim whitespace
    - Lowercase
    - Replace spaces with underscore
    - Replace @ with underscore
    - Allow only [a-z0-9._-]
    - Remove all other characters
    
    Args:
        value: String to sanitize
        
    Returns:
        Sanitized string
    """
    if not value:
        return ''
    result = value.strip().lower()
    result = result.replace(' ', '_')
    result = result.replace('@', '_')
    result = re.sub(r'[^a-z0-9._-]', '', result)
    return result


def sanitize_filename(filename: str, max_length: int = 64) -> str:
    """
    Sanitize a filename for safe storage.
    
    Args:
        filename: Original filename
        max_length: Maximum length for the sanitized name
        
    Returns:
        Sanitized filename
    """
    if not filename:
        return 'unnamed'
    
    # Get extension separately
    name, ext = os.path.splitext(filename)
    ext = ext.lower()
    
    # Sanitize the name part
    sanitized_name = sanitize_identifier(name)
    
    # Ensure we have a name
    if not sanitized_name:
        sanitized_name = 'unnamed'
    
    # Truncate if needed (accounting for extension)
    max_name_length = max_length - len(ext)
    if len(sanitized_name) > max_name_length:
        sanitized_name = sanitized_name[:max_name_length].rstrip('_-.')
    
    return f"{sanitized_name}{ext}"


def get_timestamp_suffix() -> str:
    """
    Get a timestamp suffix in YYYYMMDDHHMMSS format.
    
    Returns:
        Timestamp string
    """
    now = datetime.now()
    return now.strftime('%Y%m%d%H%M%S')


def get_date_prefix() -> str:
    """
    Get a date prefix in YYYYMMDD format.
    
    Returns:
        Date string
    """
    now = datetime.now()
    return now.strftime('%Y%m%d')


def get_plugin_data_dir() -> Path:
    """
    Get the plugin data directory.
    
    Returns:
        Path to plugin data directory
    """
    qgis_dir = Path(QgsApplication.qgisSettingsDirPath())
    plugin_dir = qgis_dir / 'qgisimple_integrator'
    plugin_dir.mkdir(parents=True, exist_ok=True)
    return plugin_dir


def get_email_attachments_dir() -> Path:
    """
    Get the email attachments storage directory.
    
    Returns:
        Path to email attachments directory
    """
    attachments_dir = get_plugin_data_dir() / 'email_attachments'
    attachments_dir.mkdir(parents=True, exist_ok=True)
    return attachments_dir


def get_cache_dir() -> Path:
    """
    Get the cache directory.
    
    Returns:
        Path to cache directory
    """
    cache_dir = get_plugin_data_dir() / 'cache'
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_backup_dir() -> Path:
    """
    Get the backup directory on Desktop.
    
    Returns:
        Path to backup directory
    """
    desktop = Path.home() / 'Desktop'
    if not desktop.exists():
        # Fallback to home directory if Desktop doesn't exist
        desktop = Path.home()
    
    backup_dir = desktop / 'QGIS_EmailPlugin_Backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def get_attachment_storage_path(
    sender: str,
    message_id: str,
    filename: str,
    message_date: Optional[datetime] = None
) -> Path:
    """
    Build sanitized storage path for an email attachment.
    
    Pattern: <attachments_dir>/<sanitized_sender>/<YYYYMMDD>_<sanitized_message_id>/<sanitized_filename>
    
    Args:
        sender: Email sender address
        message_id: Message ID
        filename: Attachment filename
        message_date: Message date (defaults to now)
        
    Returns:
        Full path for storing the attachment
    """
    attachments_dir = get_email_attachments_dir()
    
    sanitized_sender = sanitize_identifier(sender)
    if not sanitized_sender:
        sanitized_sender = 'unknown_sender'
    
    sanitized_msg_id = sanitize_identifier(message_id)
    if not sanitized_msg_id:
        sanitized_msg_id = 'unknown_message'
    
    sanitized_filename = sanitize_filename(filename)
    
    if message_date:
        date_prefix = message_date.strftime('%Y%m%d')
    else:
        date_prefix = get_date_prefix()
    
    folder_name = f"{date_prefix}_{sanitized_msg_id}"
    
    return attachments_dir / sanitized_sender / folder_name / sanitized_filename


def get_backup_path(
    host_layer: str,
    sender: str,
    version_id: str
) -> Path:
    """
    Build backup path for a commit operation.
    
    Pattern: <backup_dir>/<sanitized_sender>/<sanitized_host_layer>_backup_<YYYYMMDDHHMMSS>_<version_id>.geojson
    
    Args:
        host_layer: Name of the host layer being backed up
        sender: Original data sender (email or gisimple)
        version_id: Version UUID
        
    Returns:
        Full path for the backup file
    """
    backup_dir = get_backup_dir()
    
    sanitized_sender = sanitize_identifier(sender)
    if not sanitized_sender:
        sanitized_sender = 'local'
    
    sanitized_layer = sanitize_identifier(host_layer)
    if not sanitized_layer:
        sanitized_layer = 'unknown_layer'
    
    timestamp = get_timestamp_suffix()
    sanitized_version = sanitize_identifier(version_id)
    
    filename = f"{sanitized_layer}_backup_{timestamp}_{sanitized_version}.geojson"
    
    sender_dir = backup_dir / sanitized_sender
    sender_dir.mkdir(parents=True, exist_ok=True)
    
    return sender_dir / filename


def resolve_collision(path: Path) -> Path:
    """
    Resolve filename collision by appending _1, _2, etc.
    
    Args:
        path: Original file path
        
    Returns:
        Path with collision resolved
    """
    if not path.exists():
        return path
    
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    
    while True:
        new_path = parent / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1
        
        # Safety limit
        if counter > 10000:
            raise ValueError(f"Too many collisions for path: {path}")


def set_user_only_permissions(path: Path) -> None:
    """
    Set file permissions to user-only (mode 0o600).
    
    Args:
        path: Path to the file
    """
    try:
        if os.name != 'nt':  # Not Windows
            os.chmod(path, 0o600)
    except OSError:
        pass  # Ignore permission errors on Windows or other issues


def ensure_parent_dir(path: Path) -> None:
    """
    Ensure the parent directory exists.
    
    Args:
        path: Path to a file
    """
    path.parent.mkdir(parents=True, exist_ok=True)
