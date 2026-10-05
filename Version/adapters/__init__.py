"""
GeoInbox - Adapters Package

Email protocol adapters for IMAP, POP3, EWS, and Microsoft Graph.
"""

from .base import (
    EmailAdapter,
    AdapterConfig,
    MessageMetadata,
    AttachmentMetadata,
    MailboxInfo
)

__all__ = [
    'EmailAdapter',
    'AdapterConfig',
    'MessageMetadata',
    'AttachmentMetadata',
    'MailboxInfo'
]
