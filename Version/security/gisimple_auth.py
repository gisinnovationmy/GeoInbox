"""
GeoInbox - GISimple Auth Client

Client for authenticating with GISimple server.
All Keycloak interaction is handled server-side for security.
"""

import json
import urllib.request
import urllib.error
import urllib.parse
import ssl
import sys
import os
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

# Add plugin directory to path
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from utils.errors import GISimpleServerError, GISimpleErrorCode
from storage.database import DatabaseManager
from utils.security import urlopen_allowed, log_debug_exception


@dataclass
class GISimpleUser:
    """GISimple user information."""
    email: str
    username: str
    display_name: str
    groups: List[str]
    roles: List[str]
    is_admin: bool


@dataclass
class GISimpleDataset:
    """GISimple group dataset information."""
    dataset_id: str
    name: str
    format: str
    size: int
    owner: str
    last_modified: str
    group_id: str


class GISimpleAuthClient:
    """
    Client for GISimple server authentication and API access.
    
    Security principle: This client NEVER connects to Keycloak directly.
    All authentication is handled by the GISimple server, which manages
    Keycloak internally. The plugin only sees:
    - GISimple server URL
    - Bearer token (received from login)
    - User info (received from server)
    """
    
    def __init__(self, db: Optional[DatabaseManager] = None):
        """
        Initialize the GISimple auth client.
        
        Args:
            db: Database manager instance
        """
        self._db = db or DatabaseManager()
        self._token: Optional[str] = None
        self._user: Optional[GISimpleUser] = None
        self._server_url: Optional[str] = None
    
    @property
    def is_authenticated(self) -> bool:
        """Check if currently authenticated."""
        return self._token is not None and self._user is not None
    
    @property
    def user(self) -> Optional[GISimpleUser]:
        """Get the current user."""
        return self._user
    
    @property
    def token(self) -> Optional[str]:
        """Get the current token."""
        return self._token
    
    def _get_server_url(self) -> str:
        """Get the configured GISimple server URL."""
        if self._server_url:
            return self._server_url
        
        url = self._db.get_setting('gisimple_url', '')
        if not url:
            raise GISimpleServerError(
                GISimpleErrorCode.CONNECTION_FAILED,
                "GISimple server URL not configured"
            )
        
        # Ensure no trailing slash
        self._server_url = url.rstrip('/')
        return self._server_url
    
    def _make_request(
        self,
        endpoint: str,
        method: str = 'GET',
        data: Optional[Dict[str, Any]] = None,
        require_auth: bool = True
    ) -> Dict[str, Any]:
        """
        Make an HTTP request to the GISimple server.
        
        Args:
            endpoint: API endpoint (e.g., '/api/auth/login')
            method: HTTP method
            data: Request body data
            require_auth: Whether to include auth token
            
        Returns:
            Response JSON data
            
        Raises:
            GISimpleServerError: On request failure
        """
        url = f"{self._get_server_url()}{endpoint}"
        
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        if require_auth and self._token:
            headers['Authorization'] = f'Bearer {self._token}'
        
        body = None
        if data:
            body = json.dumps(data).encode('utf-8')
        
        try:
            request = urllib.request.Request(
                url,
                data=body,
                headers=headers,
                method=method
            )
            
            # Create SSL context that doesn't verify certificates (for self-signed certs)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            with urlopen_allowed(request, timeout=30, context=ssl_context) as response:
                response_data = response.read().decode('utf-8')
                return json.loads(response_data) if response_data else {}
                
        except urllib.error.HTTPError as e:
            error_body = ''
            try:
                error_body = e.read().decode('utf-8')
            except Exception as exc:
                log_debug_exception("Failed to read GISimple HTTP error body", exc)
            
            if e.code == 401:
                raise GISimpleServerError(
                    GISimpleErrorCode.AUTHENTICATION_FAILED,
                    "Authentication failed",
                    error_body
                )
            elif e.code == 403:
                raise GISimpleServerError(
                    GISimpleErrorCode.PERMISSION_DENIED,
                    "Permission denied",
                    error_body
                )
            elif e.code == 404:
                raise GISimpleServerError(
                    GISimpleErrorCode.DATASET_NOT_FOUND,
                    "Resource not found",
                    error_body
                )
            else:
                raise GISimpleServerError(
                    GISimpleErrorCode.SERVER_ERROR,
                    f"Server error: {e.code}",
                    error_body
                )
                
        except urllib.error.URLError as e:
            raise GISimpleServerError(
                GISimpleErrorCode.CONNECTION_FAILED,
                "Failed to connect to GISimple server",
                str(e.reason)
            )
        except json.JSONDecodeError as e:
            raise GISimpleServerError(
                GISimpleErrorCode.SERVER_ERROR,
                "Invalid response from server",
                str(e)
            )
        except Exception as e:
            raise GISimpleServerError(
                GISimpleErrorCode.UNKNOWN_ERROR,
                "Request failed",
                str(e)
            )
    
    def login(self, username: str, password: str) -> GISimpleUser:
        """
        Authenticate with GISimple server.
        
        Args:
            username: Username
            password: Password
            
        Returns:
            User information
            
        Raises:
            GISimpleServerError: On authentication failure
        """
        response = self._make_request(
            '/api/auth/login',
            method='POST',
            data={'username': username, 'password': password},
            require_auth=False
        )
        
        self._token = response.get('token')
        if not self._token:
            raise GISimpleServerError(
                GISimpleErrorCode.AUTHENTICATION_FAILED,
                "No token received from server"
            )
        
        # Get user info
        user_data = response.get('user', {})
        self._user = GISimpleUser(
            email=user_data.get('email', ''),
            username=user_data.get('username', username),
            display_name=user_data.get('displayName', username),
            groups=user_data.get('groups', []),
            roles=user_data.get('roles', []),
            is_admin='gisimple-admin' in user_data.get('roles', []) or 
                     'gisimple-superadmin' in user_data.get('roles', [])
        )
        
        return self._user
    
    def logout(self) -> None:
        """Log out and clear credentials."""
        self._token = None
        self._user = None
    
    def get_user_info(self) -> GISimpleUser:
        """
        Get current user information from server.
        
        Returns:
            User information
            
        Raises:
            GISimpleServerError: If not authenticated or request fails
        """
        if not self._token:
            raise GISimpleServerError(
                GISimpleErrorCode.AUTHENTICATION_FAILED,
                "Not authenticated"
            )
        
        response = self._make_request('/api/auth/user')
        
        self._user = GISimpleUser(
            email=response.get('email', ''),
            username=response.get('username', ''),
            display_name=response.get('displayName', ''),
            groups=response.get('groups', []),
            roles=response.get('roles', []),
            is_admin='gisimple-admin' in response.get('roles', []) or
                     'gisimple-superadmin' in response.get('roles', [])
        )
        
        return self._user
    
    def get_email_config(self) -> Dict[str, Any]:
        """
        Get email server configuration for the current user.
        
        Returns:
            Email configuration from GISimple server
            
        Raises:
            GISimpleServerError: If not authenticated or request fails
        """
        return self._make_request('/api/email/config')
    
    def get_trusted_senders(self) -> Dict[str, Any]:
        """
        Get list of trusted senders from GISimple server.
        
        Returns:
            Dictionary with trusted_emails and groups lists
        """
        return self._make_request('/api/trusted-emails')
    
    def get_groups(self) -> List[Dict[str, Any]]:
        """
        Get list of groups the user belongs to.
        
        Returns:
            List of group information
        """
        response = self._make_request('/api/groups')
        return response.get('groups', [])
    
    def get_group_datasets(self, group_id: str) -> List[GISimpleDataset]:
        """
        Get datasets shared in a group.
        
        Args:
            group_id: Group ID
            
        Returns:
            List of dataset information
        """
        response = self._make_request(f'/api/groups/{group_id}/datasets')
        
        datasets = []
        for item in response.get('datasets', []):
            datasets.append(GISimpleDataset(
                dataset_id=item.get('id', ''),
                name=item.get('name', ''),
                format=item.get('format', ''),
                size=item.get('size', 0),
                owner=item.get('owner', ''),
                last_modified=item.get('lastModified', ''),
                group_id=group_id
            ))
        
        return datasets
    
    def get_dataset_metadata(self, dataset_id: str) -> Dict[str, Any]:
        """
        Get metadata for a dataset.
        
        Args:
            dataset_id: Dataset ID
            
        Returns:
            Dataset metadata
        """
        return self._make_request(f'/api/datasets/{dataset_id}')
    
    def download_dataset(
        self,
        dataset_id: str,
        output_path: str,
        progress_callback: Optional[callable] = None
    ) -> str:
        """
        Download a dataset file.
        
        Args:
            dataset_id: Dataset ID
            output_path: Path to save the file
            progress_callback: Optional callback for progress updates
            
        Returns:
            Path to downloaded file
            
        Raises:
            GISimpleServerError: On download failure
        """
        url = f"{self._get_server_url()}/api/datasets/{dataset_id}/download"
        
        headers = {}
        if self._token:
            headers['Authorization'] = f'Bearer {self._token}'
        
        try:
            request = urllib.request.Request(url, headers=headers)
            
            with urlopen_allowed(request, timeout=300) as response:
                total_size = int(response.headers.get('Content-Length', 0))
                downloaded = 0
                
                with open(output_path, 'wb') as f:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        if progress_callback and total_size > 0:
                            progress_callback(downloaded, total_size)
            
            return output_path
            
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise GISimpleServerError(
                    GISimpleErrorCode.DATASET_NOT_FOUND,
                    f"Dataset not found: {dataset_id}"
                )
            raise GISimpleServerError(
                GISimpleErrorCode.SERVER_ERROR,
                f"Download failed: {e.code}"
            )
        except Exception as e:
            raise GISimpleServerError(
                GISimpleErrorCode.UNKNOWN_ERROR,
                "Download failed",
                str(e)
            )
    
    def is_sender_trusted(self, from_address: str) -> bool:
        """
        Check if a sender is trusted according to GISimple server.
        
        Args:
            from_address: Sender email address
            
        Returns:
            True if sender is trusted
        """
        try:
            trusted_data = self.get_trusted_senders()
            
            # Extract email from "Name <email>" format
            import re
            match = re.search(r'<([^>]+)>', from_address)
            email = match.group(1) if match else from_address
            email = email.lower()
            
            # Check trusted emails
            trusted_emails = [e.lower() for e in trusted_data.get('trusted_emails', [])]
            if email in trusted_emails:
                return True
            
            # Check trusted domains
            if '@' in email:
                domain = email.split('@')[1]
                trusted_domains = [d.lower() for d in trusted_data.get('trusted_domains', [])]
                if domain in trusted_domains:
                    return True
            
            return False
            
        except GISimpleServerError:
            return False
    
    def get_all_users(self) -> List[Dict[str, Any]]:
        """
        Get all users from GISimple/Keycloak.
        
        Returns:
            List of user information with email and groups
        """
        response = self._make_request('/api/users')
        return response.get('users', [])
    
    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """
        Look up a user by email address.
        
        Args:
            email: Email address to look up
            
        Returns:
            User information or None if not found
        """
        try:
            # Extract email from "Name <email>" format
            import re
            match = re.search(r'<([^>]+)>', email)
            clean_email = match.group(1) if match else email
            clean_email = clean_email.lower().strip()
            
            response = self._make_request(
                '/api/users/lookup',
                method='POST',
                data={'email': clean_email}
            )
            return response.get('user')
        except GISimpleServerError:
            return None
    
    def get_users_by_group(self, group_id: str) -> List[Dict[str, Any]]:
        """
        Get all users in a specific Keycloak group.
        
        Args:
            group_id: Group ID
            
        Returns:
            List of users in the group
        """
        response = self._make_request(f'/api/groups/{group_id}/users')
        return response.get('users', [])
    
    def get_sender_groups(self, from_address: str) -> List[str]:
        """
        Get the Keycloak groups that an email sender belongs to.
        
        Args:
            from_address: Sender email address
            
        Returns:
            List of group names the sender belongs to
        """
        user = self.get_user_by_email(from_address)
        if user:
            return user.get('groups', [])
        return []
    
    def get_upload_groups(self) -> List[Dict[str, Any]]:
        """
        Get list of upload groups the user belongs to.
        
        Returns:
            List of upload group information
        """
        response = self._make_request('/api/upload-groups')
        return response.get('groups', [])
    
    def get_uploaded_files(self) -> List[Dict[str, Any]]:
        """
        Get list of uploaded files accessible to the user.
        
        Returns files uploaded by the user and by members of their groups.
        
        Returns:
            List of file information with owner, filename, size, modified
        """
        response = self._make_request('/api/upload-other-data/files')
        return response.get('files', [])
    
    def download_uploaded_file(
        self,
        owner: str,
        filename: str,
        output_path: str,
        progress_callback: Optional[callable] = None
    ) -> str:
        """
        Download an uploaded file from GISimple.
        
        Args:
            owner: Owner identifier (sanitized email)
            filename: Filename to download
            output_path: Path to save the file
            progress_callback: Optional callback for progress updates
            
        Returns:
            Path to downloaded file
            
        Raises:
            GISimpleServerError: On download failure
        """
        url = f"{self._get_server_url()}/api/upload-other-data/download/{owner}/{filename}"
        
        headers = {}
        if self._token:
            headers['Authorization'] = f'Bearer {self._token}'
        
        try:
            request = urllib.request.Request(url, headers=headers)
            
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            with urlopen_allowed(request, timeout=300, context=ssl_context) as response:
                total_size = int(response.headers.get('Content-Length', 0))
                downloaded = 0
                
                with open(output_path, 'wb') as f:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        if progress_callback and total_size > 0:
                            progress_callback(downloaded, total_size)
            
            return output_path
            
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise GISimpleServerError(
                    GISimpleErrorCode.DATASET_NOT_FOUND,
                    f"File not found: {owner}/{filename}"
                )
            elif e.code == 403:
                raise GISimpleServerError(
                    GISimpleErrorCode.PERMISSION_DENIED,
                    f"Access denied to file: {owner}/{filename}"
                )
            raise GISimpleServerError(
                GISimpleErrorCode.SERVER_ERROR,
                f"Download failed: {e.code}"
            )
        except Exception as e:
            raise GISimpleServerError(
                GISimpleErrorCode.UNKNOWN_ERROR,
                "Download failed",
                str(e)
            )
