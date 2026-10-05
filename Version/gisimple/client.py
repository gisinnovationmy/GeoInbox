"""
GeoInbox - GISimple Client

High-level client for GISimple server integration.
Combines authentication and data access functionality.
"""

from typing import Optional, List, Dict, Any
from pathlib import Path
import sys
import os

# Add plugin directory to path
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from security.gisimple_auth import GISimpleAuthClient, GISimpleUser, GISimpleDataset
from storage.sanitizer import get_attachment_storage_path, ensure_parent_dir
from storage.database import DatabaseManager
from utils.errors import GISimpleServerError, GISimpleErrorCode


class GISimpleClient:
    """
    High-level client for GISimple server integration.
    
    Provides a unified interface for:
    - Authentication
    - Group browsing
    - Dataset downloading
    - Trust evaluation
    """
    
    def __init__(self, db: Optional[DatabaseManager] = None):
        """
        Initialize the GISimple client.
        
        Args:
            db: Database manager instance
        """
        self._db = db or DatabaseManager()
        self._auth = GISimpleAuthClient(self._db)
    
    @property
    def is_enabled(self) -> bool:
        """Check if GISimple integration is enabled."""
        return self._db.get_setting('gisimple_enabled', 'false') == 'true'
    
    @property
    def is_authenticated(self) -> bool:
        """Check if currently authenticated."""
        return self._auth.is_authenticated
    
    @property
    def user(self) -> Optional[GISimpleUser]:
        """Get the current user."""
        return self._auth.user
    
    @property
    def server_url(self) -> str:
        """Get the configured server URL."""
        return self._db.get_setting('gisimple_url', '')
    
    def login(self, username: str, password: str) -> GISimpleUser:
        """
        Log in to GISimple server.
        
        Args:
            username: Username
            password: Password
            
        Returns:
            User information
            
        Raises:
            GISimpleServerError: On authentication failure
        """
        if not self.is_enabled:
            raise GISimpleServerError(
                GISimpleErrorCode.CONNECTION_FAILED,
                "GISimple integration is not enabled"
            )
        
        return self._auth.login(username, password)
    
    def logout(self) -> None:
        """Log out from GISimple server."""
        self._auth.logout()
    
    def get_groups(self) -> List[Dict[str, Any]]:
        """
        Get list of groups the user belongs to.
        
        Returns:
            List of group information
        """
        if not self.is_authenticated:
            raise GISimpleServerError(
                GISimpleErrorCode.AUTHENTICATION_FAILED,
                "Not authenticated"
            )
        
        return self._auth.get_groups()
    
    def get_group_datasets(self, group_id: str) -> List[GISimpleDataset]:
        """
        Get datasets shared in a group.
        
        Args:
            group_id: Group ID
            
        Returns:
            List of dataset information
        """
        if not self.is_authenticated:
            raise GISimpleServerError(
                GISimpleErrorCode.AUTHENTICATION_FAILED,
                "Not authenticated"
            )
        
        return self._auth.get_group_datasets(group_id)
    
    def download_dataset(
        self,
        dataset: GISimpleDataset,
        progress_callback: Optional[callable] = None
    ) -> Path:
        """
        Download a dataset from GISimple.
        
        Args:
            dataset: Dataset to download
            progress_callback: Optional progress callback (current, total)
            
        Returns:
            Path to downloaded file
        """
        if not self.is_authenticated:
            raise GISimpleServerError(
                GISimpleErrorCode.AUTHENTICATION_FAILED,
                "Not authenticated"
            )
        
        # Determine output path
        output_path = get_attachment_storage_path(
            sender=f"gisimple_{dataset.owner}",
            message_id=dataset.dataset_id,
            filename=dataset.name
        )
        
        ensure_parent_dir(output_path)
        
        # Download
        self._auth.download_dataset(
            dataset.dataset_id,
            str(output_path),
            progress_callback
        )
        
        return output_path
    
    def is_sender_trusted(self, from_address: str) -> bool:
        """
        Check if a sender is trusted according to GISimple.
        
        Args:
            from_address: Sender email address
            
        Returns:
            True if sender is trusted
        """
        if not self.is_authenticated:
            return False
        
        return self._auth.is_sender_trusted(from_address)
    
    def get_email_config(self) -> Optional[Dict[str, Any]]:
        """
        Get email server configuration from GISimple.
        
        Returns:
            Email configuration or None if not available
        """
        if not self.is_authenticated:
            return None
        
        try:
            return self._auth.get_email_config()
        except GISimpleServerError:
            return None
    
    def get_client_source_uri(self, dataset: GISimpleDataset) -> str:
        """
        Get the client source URI for a GISimple dataset.
        
        Used for version record provenance.
        
        Args:
            dataset: Dataset
            
        Returns:
            URI string (e.g., "gisimpleserver://dataset_id")
        """
        return f"gisimpleserver://{dataset.dataset_id}"
    
    def get_upload_groups(self) -> List[Dict[str, Any]]:
        """
        Get list of upload groups the user belongs to.
        
        Returns:
            List of upload group information
        """
        if not self.is_authenticated:
            raise GISimpleServerError(
                GISimpleErrorCode.AUTHENTICATION_FAILED,
                "Not authenticated"
            )
        
        return self._auth.get_upload_groups()
    
    def get_uploaded_files(self) -> List[Dict[str, Any]]:
        """
        Get list of uploaded files accessible to the user.
        
        Returns files uploaded by the user and by members of their groups.
        
        Returns:
            List of file information with owner, filename, size, modified
        """
        if not self.is_authenticated:
            raise GISimpleServerError(
                GISimpleErrorCode.AUTHENTICATION_FAILED,
                "Not authenticated"
            )
        
        return self._auth.get_uploaded_files()
    
    def download_uploaded_file(
        self,
        owner: str,
        filename: str,
        progress_callback: Optional[callable] = None
    ) -> Path:
        """
        Download an uploaded file from GISimple.
        
        Args:
            owner: Owner identifier (sanitized email)
            filename: Filename to download
            progress_callback: Optional progress callback (current, total)
            
        Returns:
            Path to downloaded file
        """
        if not self.is_authenticated:
            raise GISimpleServerError(
                GISimpleErrorCode.AUTHENTICATION_FAILED,
                "Not authenticated"
            )
        
        # Determine output path
        output_path = get_attachment_storage_path(
            sender=f"gisimple_{owner}",
            message_id=f"upload_{filename}",
            filename=filename
        )
        
        ensure_parent_dir(output_path)
        
        # Download
        self._auth.download_uploaded_file(
            owner,
            filename,
            str(output_path),
            progress_callback
        )
        
        return output_path
    
    def get_trusted_emails(self) -> List[str]:
        """
        Get list of trusted email addresses from GISimple.
        
        Returns all group members' emails that should be trusted.
        
        Returns:
            List of trusted email addresses
        """
        if not self.is_authenticated:
            return []
        
        try:
            response = self._auth.get_trusted_senders()
            return response.get('trusted_emails', [])
        except GISimpleServerError:
            return []
    
    def get_uploaded_file_source_uri(self, owner: str, filename: str) -> str:
        """
        Get the client source URI for an uploaded file.
        
        Used for version record provenance.
        
        Args:
            owner: Owner identifier
            filename: Filename
            
        Returns:
            URI string (e.g., "gisimpleserver://upload/owner/filename")
        """
        return f"gisimpleserver://upload/{owner}/{filename}"
