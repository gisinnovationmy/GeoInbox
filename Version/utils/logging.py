"""
GeoInbox - Logging

Operation logging utilities for debugging and audit trails.
"""

import logging
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

from qgis.core import QgsMessageLog, Qgis

from ..storage.sanitizer import get_plugin_data_dir


PLUGIN_NAME = "GeoInbox"

# Module-level logger
_logger: Optional[logging.Logger] = None


def get_logger() -> logging.Logger:
    """
    Get the plugin logger.
    
    Returns:
        Configured logger instance
    """
    global _logger
    
    if _logger is None:
        _logger = logging.getLogger(PLUGIN_NAME)
        _logger.setLevel(logging.DEBUG)
        
        # File handler
        log_dir = get_plugin_data_dir() / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        
        log_file = log_dir / f"qgisimple_{datetime.now().strftime('%Y%m%d')}.log"
        
        file_handler = logging.FileHandler(str(log_file), encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(formatter)
        
        _logger.addHandler(file_handler)
    
    return _logger


def log_debug(message: str) -> None:
    """Log a debug message."""
    get_logger().debug(message)
    QgsMessageLog.logMessage(message, PLUGIN_NAME, Qgis.MessageLevel.Info)


def log_info(message: str) -> None:
    """Log an info message."""
    get_logger().info(message)
    QgsMessageLog.logMessage(message, PLUGIN_NAME, Qgis.MessageLevel.Info)


def log_warning(message: str) -> None:
    """Log a warning message."""
    get_logger().warning(message)
    QgsMessageLog.logMessage(message, PLUGIN_NAME, Qgis.MessageLevel.Warning)


def log_error(message: str, exception: Optional[Exception] = None) -> None:
    """Log an error message."""
    if exception:
        message = f"{message}: {exception}"
    get_logger().error(message)
    QgsMessageLog.logMessage(message, PLUGIN_NAME, Qgis.MessageLevel.Critical)


def log_operation(
    operation: str,
    details: Optional[dict] = None,
    success: bool = True
) -> None:
    """
    Log an operation for audit trail.
    
    Args:
        operation: Operation name (e.g., 'email_connect', 'download_attachment')
        details: Optional details dict
        success: Whether operation succeeded
    """
    status = "SUCCESS" if success else "FAILED"
    message = f"[{status}] {operation}"
    
    if details:
        detail_str = ", ".join(f"{k}={v}" for k, v in details.items())
        message = f"{message} - {detail_str}"
    
    if success:
        log_info(message)
    else:
        log_error(message)


def cleanup_old_logs(days: int = 30) -> int:
    """
    Remove log files older than specified days.
    
    Args:
        days: Age threshold in days
        
    Returns:
        Number of files removed
    """
    from datetime import timedelta
    
    log_dir = get_plugin_data_dir() / 'logs'
    if not log_dir.exists():
        return 0
    
    cutoff = datetime.now() - timedelta(days=days)
    removed = 0
    
    for log_file in log_dir.glob('qgisimple_*.log'):
        try:
            # Parse date from filename
            date_str = log_file.stem.replace('qgisimple_', '')
            file_date = datetime.strptime(date_str, '%Y%m%d')
            
            if file_date < cutoff:
                log_file.unlink()
                removed += 1
        except (ValueError, OSError):
            continue
    
    return removed
