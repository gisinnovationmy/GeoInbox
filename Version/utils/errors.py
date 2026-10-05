"""
GeoInbox - Error Codes and Exceptions

Standardized error codes for UI mapping and consistent error handling.
"""

from enum import Enum
from typing import Optional


class AdapterErrorCode(Enum):
    """Standardized error codes for email adapters."""
    CONNECTION_FAILED = "CONNECTION_FAILED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    MAILBOX_NOT_FOUND = "MAILBOX_NOT_FOUND"
    MESSAGE_NOT_FOUND = "MESSAGE_NOT_FOUND"
    ATTACHMENT_NOT_FOUND = "ATTACHMENT_NOT_FOUND"
    NETWORK_ERROR = "NETWORK_ERROR"
    TIMEOUT = "TIMEOUT"
    SSL_ERROR = "SSL_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class StorageErrorCode(Enum):
    """Standardized error codes for storage operations."""
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    DISK_FULL = "DISK_FULL"
    INVALID_PATH = "INVALID_PATH"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    INVALID_FORMAT = "INVALID_FORMAT"
    SHAPEFILE_INCOMPLETE = "SHAPEFILE_INCOMPLETE"
    DATABASE_ERROR = "DATABASE_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class ConnectorErrorCode(Enum):
    """Standardized error codes for host connectors (GeoPackage, PostGIS)."""
    CONNECTION_FAILED = "CONNECTION_FAILED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    LAYER_NOT_FOUND = "LAYER_NOT_FOUND"
    TRANSACTION_FAILED = "TRANSACTION_FAILED"
    COMMIT_FAILED = "COMMIT_FAILED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    FEATURE_NOT_FOUND = "FEATURE_NOT_FOUND"
    INVALID_GEOMETRY = "INVALID_GEOMETRY"
    CRS_MISMATCH = "CRS_MISMATCH"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class GISimpleErrorCode(Enum):
    """Standardized error codes for GISimple server operations."""
    CONNECTION_FAILED = "CONNECTION_FAILED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"  # nosec B105
    TOKEN_INVALID = "TOKEN_INVALID"  # nosec B105
    GROUP_NOT_FOUND = "GROUP_NOT_FOUND"
    DATASET_NOT_FOUND = "DATASET_NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    SERVER_ERROR = "SERVER_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class GeoInboxError(Exception):
    """Base exception for GeoInbox."""

    def __init__(self, message: str, details: Optional[str] = None):
        self.message = message
        self.details = details
        super().__init__(message)

    def __str__(self):
        if self.details:
            return f"{self.message}: {self.details}"
        return self.message


class AdapterError(GeoInboxError):
    """Exception for email adapter errors."""

    def __init__(self, code: AdapterErrorCode, message: str, details: Optional[str] = None):
        self.code = code
        super().__init__(message, details)

    def __str__(self):
        base = f"[{self.code.value}] {self.message}"
        if self.details:
            return f"{base}: {self.details}"
        return base


class StorageError(GeoInboxError):
    """Exception for storage operation errors."""

    def __init__(self, code: StorageErrorCode, message: str, details: Optional[str] = None):
        self.code = code
        super().__init__(message, details)

    def __str__(self):
        base = f"[{self.code.value}] {self.message}"
        if self.details:
            return f"{base}: {self.details}"
        return base


class ConnectorError(GeoInboxError):
    """Exception for host connector errors."""

    def __init__(self, code: ConnectorErrorCode, message: str, details: Optional[str] = None):
        self.code = code
        super().__init__(message, details)

    def __str__(self):
        base = f"[{self.code.value}] {self.message}"
        if self.details:
            return f"{base}: {self.details}"
        return base


class GISimpleServerError(GeoInboxError):
    """Exception for GISimple server errors."""

    def __init__(self, code: GISimpleErrorCode, message: str, details: Optional[str] = None):
        self.code = code
        super().__init__(message, details)

    def __str__(self):
        base = f"[{self.code.value}] {self.message}"
        if self.details:
            return f"{base}: {self.details}"
        return base


def get_user_friendly_message(error_code) -> str:
    """
    Get a user-friendly message for an error code.
    
    Args:
        error_code: An error code enum value
        
    Returns:
        User-friendly error message string
    """
    messages = {
        # Adapter errors
        AdapterErrorCode.CONNECTION_FAILED: "Could not connect to the email server. Please check your server settings.",
        AdapterErrorCode.AUTHENTICATION_FAILED: "Login failed. Please check your username and password.",
        AdapterErrorCode.MAILBOX_NOT_FOUND: "The requested mailbox was not found.",
        AdapterErrorCode.MESSAGE_NOT_FOUND: "The requested message was not found.",
        AdapterErrorCode.ATTACHMENT_NOT_FOUND: "The requested attachment was not found.",
        AdapterErrorCode.NETWORK_ERROR: "A network error occurred. Please check your internet connection.",
        AdapterErrorCode.TIMEOUT: "The connection timed out. Please try again.",
        AdapterErrorCode.SSL_ERROR: "SSL/TLS error. Please check your security settings.",
        AdapterErrorCode.PERMISSION_DENIED: "Permission denied. You may not have access to this resource.",
        AdapterErrorCode.INVALID_RESPONSE: "The server returned an invalid response.",
        AdapterErrorCode.PROTOCOL_ERROR: "A protocol error occurred.",
        AdapterErrorCode.UNKNOWN_ERROR: "An unknown error occurred.",
        
        # Storage errors
        StorageErrorCode.FILE_NOT_FOUND: "The file was not found.",
        StorageErrorCode.PERMISSION_DENIED: "Permission denied. Cannot access the file or folder.",
        StorageErrorCode.DISK_FULL: "Not enough disk space available.",
        StorageErrorCode.INVALID_PATH: "The file path is invalid.",
        StorageErrorCode.EXTRACTION_FAILED: "Failed to extract the archive.",
        StorageErrorCode.INVALID_FORMAT: "The file format is not supported.",
        StorageErrorCode.SHAPEFILE_INCOMPLETE: "The shapefile is incomplete. Required files: .shp, .dbf, .shx",
        StorageErrorCode.DATABASE_ERROR: "A database error occurred.",
        StorageErrorCode.UNKNOWN_ERROR: "An unknown storage error occurred.",
        
        # Connector errors
        ConnectorErrorCode.CONNECTION_FAILED: "Could not connect to the database.",
        ConnectorErrorCode.AUTHENTICATION_FAILED: "Database authentication failed.",
        ConnectorErrorCode.LAYER_NOT_FOUND: "The layer was not found.",
        ConnectorErrorCode.TRANSACTION_FAILED: "The transaction failed.",
        ConnectorErrorCode.COMMIT_FAILED: "Failed to commit changes.",
        ConnectorErrorCode.ROLLBACK_FAILED: "Failed to rollback changes.",
        ConnectorErrorCode.FEATURE_NOT_FOUND: "The feature was not found.",
        ConnectorErrorCode.INVALID_GEOMETRY: "The geometry is invalid.",
        ConnectorErrorCode.CRS_MISMATCH: "Coordinate reference systems do not match.",
        ConnectorErrorCode.PERMISSION_DENIED: "Permission denied for database operation.",
        ConnectorErrorCode.UNKNOWN_ERROR: "An unknown database error occurred.",
        
        # GISimple errors
        GISimpleErrorCode.CONNECTION_FAILED: "Could not connect to GISimple server.",
        GISimpleErrorCode.AUTHENTICATION_FAILED: "GISimple authentication failed.",
        GISimpleErrorCode.TOKEN_EXPIRED: "Your session has expired. Please log in again.",
        GISimpleErrorCode.TOKEN_INVALID: "Invalid authentication token.",
        GISimpleErrorCode.GROUP_NOT_FOUND: "The group was not found.",
        GISimpleErrorCode.DATASET_NOT_FOUND: "The dataset was not found.",
        GISimpleErrorCode.PERMISSION_DENIED: "You do not have permission to access this resource.",
        GISimpleErrorCode.SERVER_ERROR: "The GISimple server encountered an error.",
        GISimpleErrorCode.UNKNOWN_ERROR: "An unknown GISimple error occurred.",
    }
    
    return messages.get(error_code, "An error occurred.")
