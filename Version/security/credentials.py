"""
GeoInbox - Credentials Manager

Wrapper for QGIS credential manager for secure password storage.
"""

from typing import Optional
from qgis.core import QgsApplication, QgsAuthMethodConfig


def get_credential(credential_key: str) -> Optional[str]:
    """
    Get a credential (password/token) from QGIS credential manager.
    
    Args:
        credential_key: The credential key/ID
        
    Returns:
        The credential value or None if not found
    """
    if not credential_key:
        return None
    
    auth_manager = QgsApplication.authManager()
    
    # Try to get the auth config
    config = QgsAuthMethodConfig()
    if auth_manager.loadAuthenticationConfig(credential_key, config, True):
        # For basic auth, password is in config
        return config.config('password', '')
    
    return None


def store_credential(
    name: str,
    username: str,
    password: str,
    credential_key: Optional[str] = None
) -> Optional[str]:
    """
    Store a credential in QGIS credential manager.
    
    Args:
        name: Display name for the credential
        username: Username
        password: Password to store
        credential_key: Existing key to update, or None to create new
        
    Returns:
        The credential key/ID, or None if failed
    """
    auth_manager = QgsApplication.authManager()
    
    config = QgsAuthMethodConfig()
    config.setName(name)
    config.setMethod('Basic')
    config.setConfig('username', username)
    config.setConfig('password', password)
    
    if credential_key:
        # Update existing
        config.setId(credential_key)
        if auth_manager.updateAuthenticationConfig(config):
            return credential_key
        return None
    else:
        # Create new
        if auth_manager.storeAuthenticationConfig(config):
            return config.id()
        return None


def delete_credential(credential_key: str) -> bool:
    """
    Delete a credential from QGIS credential manager.
    
    Args:
        credential_key: The credential key/ID to delete
        
    Returns:
        True if deleted successfully
    """
    if not credential_key:
        return False
    
    auth_manager = QgsApplication.authManager()
    return auth_manager.removeAuthenticationConfig(credential_key)


def credential_exists(credential_key: str) -> bool:
    """
    Check if a credential exists.
    
    Args:
        credential_key: The credential key/ID
        
    Returns:
        True if the credential exists
    """
    if not credential_key:
        return False
    
    auth_manager = QgsApplication.authManager()
    config = QgsAuthMethodConfig()
    return auth_manager.loadAuthenticationConfig(credential_key, config, False)


def get_credential_username(credential_key: str) -> Optional[str]:
    """
    Get the username associated with a credential.
    
    Args:
        credential_key: The credential key/ID
        
    Returns:
        The username or None if not found
    """
    if not credential_key:
        return None
    
    auth_manager = QgsApplication.authManager()
    config = QgsAuthMethodConfig()
    
    if auth_manager.loadAuthenticationConfig(credential_key, config, False):
        return config.config('username', '')
    
    return None
