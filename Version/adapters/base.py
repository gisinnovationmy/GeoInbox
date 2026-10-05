"""
GeoInbox - Base Adapter

Abstract base class for email protocol adapters.
Defines the interface that all adapters must implement.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Callable, Dict, Any
from datetime import datetime


@dataclass
class AttachmentMetadata:
    """Metadata for an email attachment."""
    attachment_id: str
    filename: str
    size: int
    content_type: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'attachment_id': self.attachment_id,
            'filename': self.filename,
            'size': self.size,
            'content_type': self.content_type
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AttachmentMetadata':
        """Create from dictionary."""
        return cls(
            attachment_id=data['attachment_id'],
            filename=data['filename'],
            size=data['size'],
            content_type=data['content_type']
        )


@dataclass
class MessageMetadata:
    """Metadata for an email message."""
    message_id: str
    from_address: str
    subject: str
    date: datetime
    is_read: bool
    mailbox: str
    size: int
    attachments: List[AttachmentMetadata] = field(default_factory=list)
    body_preview: str = ''
    body_html: str = ''

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'message_id': self.message_id,
            'from_address': self.from_address,
            'subject': self.subject,
            'date': self.date.isoformat() if self.date else None,
            'is_read': self.is_read,
            'mailbox': self.mailbox,
            'size': self.size,
            'attachments': [a.to_dict() for a in self.attachments],
            'body_preview': self.body_preview,
            'body_html': self.body_html
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MessageMetadata':
        """Create from dictionary."""
        return cls(
            message_id=data['message_id'],
            from_address=data['from_address'],
            subject=data['subject'],
            date=datetime.fromisoformat(data['date']) if data.get('date') else None,
            is_read=data['is_read'],
            mailbox=data['mailbox'],
            size=data['size'],
            attachments=[
                AttachmentMetadata.from_dict(a) for a in data.get('attachments', [])
            ],
            body_preview=data.get('body_preview', ''),
            body_html=data.get('body_html', '')
        )


@dataclass
class MailboxInfo:
    """Information about a mailbox/folder."""
    name: str
    message_count: int
    unread_count: int
    delimiter: str = '/'
    flags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'name': self.name,
            'message_count': self.message_count,
            'unread_count': self.unread_count,
            'delimiter': self.delimiter,
            'flags': self.flags
        }


@dataclass
class AdapterConfig:
    """Configuration for an email adapter."""
    host: str
    port: int
    username: str
    password: Optional[str] = None
    oauth2_token: Optional[str] = None
    use_ssl: bool = True
    use_tls: bool = False
    timeout: int = 30
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary (excluding sensitive data)."""
        return {
            'host': self.host,
            'port': self.port,
            'username': self.username,
            'use_ssl': self.use_ssl,
            'use_tls': self.use_tls,
            'timeout': self.timeout,
            'extra': self.extra
        }


class EmailAdapter(ABC):
    """
    Abstract base class for email protocol adapters.
    
    All email adapters (IMAP, POP3, EWS, Graph) must implement this interface.
    """

    def __init__(self):
        """Initialize the adapter."""
        self._connected = False
        self._config: Optional[AdapterConfig] = None

    @property
    def is_connected(self) -> bool:
        """Check if adapter is connected."""
        return self._connected

    @property
    def config(self) -> Optional[AdapterConfig]:
        """Get the current configuration."""
        return self._config

    @abstractmethod
    def connect(self, config: AdapterConfig) -> None:
        """
        Establish connection to email server.
        
        Args:
            config: Connection configuration
            
        Raises:
            AdapterError: If connection fails
        """
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """
        Close connection to email server.
        
        Should not raise exceptions.
        """
        pass

    @abstractmethod
    def list_mailboxes(self) -> List[MailboxInfo]:
        """
        List available mailboxes/folders.
        
        Returns:
            List of mailbox information
            
        Raises:
            AdapterError: If operation fails
        """
        pass

    @abstractmethod
    def list_messages(
        self,
        mailbox: str,
        filter_read: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[MessageMetadata]:
        """
        List messages in a mailbox with optional filtering.
        
        Args:
            mailbox: Mailbox name
            filter_read: Filter by read status (None = all, True = read only, False = unread only)
            limit: Maximum number of messages to return
            offset: Number of messages to skip
            
        Returns:
            List of message metadata
            
        Raises:
            AdapterError: If operation fails
        """
        pass

    @abstractmethod
    def fetch_message(self, message_id: str) -> MessageMetadata:
        """
        Fetch full message metadata including body preview.
        
        Args:
            message_id: Message ID
            
        Returns:
            Full message metadata
            
        Raises:
            AdapterError: If message not found or operation fails
        """
        pass

    @abstractmethod
    def fetch_attachment(
        self,
        message_id: str,
        attachment_id: str,
        stream_callback: Callable[[bytes], None]
    ) -> None:
        """
        Stream attachment content to callback.
        
        Args:
            message_id: Message ID
            attachment_id: Attachment ID
            stream_callback: Callback function to receive data chunks
            
        Raises:
            AdapterError: If attachment not found or operation fails
        """
        pass

    @abstractmethod
    def mark_as_read(self, message_id: str, read: bool = True) -> None:
        """
        Mark message as read or unread.
        
        Args:
            message_id: Message ID
            read: True to mark as read, False to mark as unread
            
        Raises:
            AdapterError: If operation fails
        """
        pass

    def get_protocol_name(self) -> str:
        """
        Get the protocol name for this adapter.
        
        Returns:
            Protocol name (e.g., 'imap', 'pop3', 'ews', 'graph')
        """
        return 'unknown'

    def supports_mailboxes(self) -> bool:
        """
        Check if this adapter supports multiple mailboxes.
        
        Returns:
            True if multiple mailboxes are supported
        """
        return True

    def supports_mark_as_read(self) -> bool:
        """
        Check if this adapter supports marking messages as read.
        
        Returns:
            True if mark as read is supported
        """
        return True

    def supports_search(self) -> bool:
        """
        Check if this adapter supports server-side search.
        
        Returns:
            True if search is supported
        """
        return False

    def search_messages(
        self,
        mailbox: str,
        query: str,
        limit: int = 100
    ) -> List[MessageMetadata]:
        """
        Search messages in a mailbox.
        
        Args:
            mailbox: Mailbox name
            query: Search query
            limit: Maximum number of results
            
        Returns:
            List of matching messages
            
        Raises:
            AdapterError: If search not supported or operation fails
        """
        from ..utils.errors import AdapterError, AdapterErrorCode
        raise AdapterError(
            AdapterErrorCode.PROTOCOL_ERROR,
            "Search not supported by this adapter"
        )
