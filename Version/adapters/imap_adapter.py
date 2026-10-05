"""
GeoInbox - IMAP Adapter

IMAP email protocol adapter using Python's imaplib.
Supports SSL/TLS and OAuth2 bearer tokens.
"""

import imaplib
import email
import email.header
import email.utils
import logging
import re
import ssl
from typing import List, Optional, Callable, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

from .base import (
    EmailAdapter,
    AdapterConfig,
    MessageMetadata,
    AttachmentMetadata,
    MailboxInfo
)
from ..utils.errors import AdapterError, AdapterErrorCode


class IMAPAdapter(EmailAdapter):
    """IMAP email protocol adapter."""

    def __init__(self):
        """Initialize the IMAP adapter."""
        super().__init__()
        self._imap: Optional[imaplib.IMAP4_SSL] = None
        self._current_mailbox: Optional[str] = None

    def get_protocol_name(self) -> str:
        """Get the protocol name."""
        return 'imap'

    def connect(self, config: AdapterConfig) -> None:
        """
        Connect to IMAP server.
        
        Args:
            config: Connection configuration
            
        Raises:
            AdapterError: If connection fails
        """
        self._config = config
        
        try:
            if config.use_ssl:
                context = ssl.create_default_context()
                self._imap = imaplib.IMAP4_SSL(
                    config.host,
                    config.port,
                    ssl_context=context,
                    timeout=config.timeout
                )
            else:
                self._imap = imaplib.IMAP4(
                    config.host,
                    config.port,
                    timeout=config.timeout
                )
                if config.use_tls:
                    context = ssl.create_default_context()
                    self._imap.starttls(ssl_context=context)
            
            # Authenticate
            if config.oauth2_token:
                # OAuth2 authentication
                auth_string = f"user={config.username}\x01auth=Bearer {config.oauth2_token}\x01\x01"
                self._imap.authenticate('XOAUTH2', lambda x: auth_string.encode())
            else:
                # Password authentication
                self._imap.login(config.username, config.password)
            
            self._connected = True
            
        except imaplib.IMAP4.error as e:
            self._connected = False
            error_msg = str(e)
            if 'authentication' in error_msg.lower() or 'login' in error_msg.lower():
                raise AdapterError(
                    AdapterErrorCode.AUTHENTICATION_FAILED,
                    "Authentication failed",
                    error_msg
                )
            raise AdapterError(
                AdapterErrorCode.CONNECTION_FAILED,
                "Failed to connect to IMAP server",
                error_msg
            )
        except ssl.SSLError as e:
            self._connected = False
            raise AdapterError(
                AdapterErrorCode.SSL_ERROR,
                "SSL/TLS error",
                str(e)
            )
        except TimeoutError:
            self._connected = False
            raise AdapterError(
                AdapterErrorCode.TIMEOUT,
                "Connection timed out"
            )
        except Exception as e:
            self._connected = False
            raise AdapterError(
                AdapterErrorCode.CONNECTION_FAILED,
                "Failed to connect to IMAP server",
                str(e)
            )

    def disconnect(self) -> None:
        """Close connection to IMAP server."""
        if self._imap:
            try:
                self._imap.logout()
            except (imaplib.IMAP4.error, OSError) as exc:
                logger.debug("IMAP logout failed: %s", exc)
            self._imap = None
        self._connected = False
        self._current_mailbox = None

    def _ensure_connected(self) -> None:
        """Ensure we are connected."""
        if not self._connected or not self._imap:
            raise AdapterError(
                AdapterErrorCode.CONNECTION_FAILED,
                "Not connected to server"
            )

    def list_mailboxes(self) -> List[MailboxInfo]:
        """
        List available mailboxes.
        
        Returns:
            List of mailbox information
        """
        self._ensure_connected()
        
        try:
            status, data = self._imap.list()
            if status != 'OK':
                raise AdapterError(
                    AdapterErrorCode.PROTOCOL_ERROR,
                    "Failed to list mailboxes"
                )
            
            mailboxes = []
            for item in data:
                if item is None:
                    continue
                
                # Parse mailbox list response
                # Format: (flags) "delimiter" "name"
                match = re.match(
                    rb'\(([^)]*)\)\s+"([^"]+)"\s+"?([^"]+)"?',
                    item
                )
                if not match:
                    continue
                
                flags_str = match.group(1).decode('utf-8', errors='replace')
                delimiter = match.group(2).decode('utf-8', errors='replace')
                name = match.group(3).decode('utf-8', errors='replace')
                
                flags = [f.strip() for f in flags_str.split() if f.strip()]
                
                # Get message counts
                message_count = 0
                unread_count = 0
                try:
                    status, select_data = self._imap.select(f'"{name}"', readonly=True)
                    if status == 'OK':
                        message_count = int(select_data[0])
                        # Get unread count
                        status, search_data = self._imap.search(None, 'UNSEEN')
                        if status == 'OK' and search_data[0]:
                            unread_count = len(search_data[0].split())
                except (imaplib.IMAP4.error, ValueError, TypeError) as exc:
                    logger.debug("Could not read counts for mailbox %s: %s", name, exc)
                
                mailboxes.append(MailboxInfo(
                    name=name,
                    message_count=message_count,
                    unread_count=unread_count,
                    delimiter=delimiter,
                    flags=flags
                ))
            
            return mailboxes
            
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError(
                AdapterErrorCode.PROTOCOL_ERROR,
                "Failed to list mailboxes",
                str(e)
            )

    def _select_mailbox(self, mailbox: str) -> None:
        """Select a mailbox if not already selected."""
        if self._current_mailbox != mailbox:
            status, data = self._imap.select(f'"{mailbox}"')
            if status != 'OK':
                raise AdapterError(
                    AdapterErrorCode.MAILBOX_NOT_FOUND,
                    f"Mailbox not found: {mailbox}"
                )
            self._current_mailbox = mailbox

    def list_messages(
        self,
        mailbox: str,
        filter_read: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[MessageMetadata]:
        """
        List messages in a mailbox.
        
        Args:
            mailbox: Mailbox name
            filter_read: Filter by read status
            limit: Maximum messages to return
            offset: Messages to skip
            
        Returns:
            List of message metadata
        """
        self._ensure_connected()
        
        try:
            self._select_mailbox(mailbox)
            
            # Build search criteria
            if filter_read is True:
                criteria = 'SEEN'
            elif filter_read is False:
                criteria = 'UNSEEN'
            else:
                criteria = 'ALL'
            
            status, data = self._imap.search(None, criteria)
            if status != 'OK':
                raise AdapterError(
                    AdapterErrorCode.PROTOCOL_ERROR,
                    "Failed to search messages"
                )
            
            message_ids = data[0].split()
            if not message_ids:
                return []
            
            # Reverse to get newest first, then apply offset and limit
            message_ids = list(reversed(message_ids))
            message_ids = message_ids[offset:offset + limit]
            
            messages = []
            for msg_id in message_ids:
                msg_metadata = None
                try:
                    msg_metadata = self._fetch_message_headers(msg_id, mailbox)
                except Exception as exc:
                    logger.debug("Skipping message %s in %s: %s", msg_id, mailbox, exc)
                if msg_metadata:
                    messages.append(msg_metadata)
            
            return messages
            
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError(
                AdapterErrorCode.PROTOCOL_ERROR,
                "Failed to list messages",
                str(e)
            )

    def _fetch_message_headers(
        self,
        msg_id: bytes,
        mailbox: str
    ) -> Optional[MessageMetadata]:
        """Fetch message headers only."""
        status, data = self._imap.fetch(
            msg_id,
            '(FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])'
        )
        if status != 'OK' or not data or not data[0]:
            return None
        
        # Parse response
        response_part = data[0]
        if isinstance(response_part, tuple):
            flags_data = response_part[0]
            header_data = response_part[1]
        else:
            return None
        
        # Parse flags
        flags_match = re.search(rb'FLAGS \(([^)]*)\)', flags_data)
        flags = []
        if flags_match:
            flags = flags_match.group(1).decode('utf-8', errors='replace').split()
        is_read = '\\Seen' in flags
        
        # Parse size
        size_match = re.search(rb'RFC822\.SIZE (\d+)', flags_data)
        size = int(size_match.group(1)) if size_match else 0
        
        # Parse headers
        msg = email.message_from_bytes(header_data)
        
        from_addr = self._decode_header(msg.get('From', ''))
        subject = self._decode_header(msg.get('Subject', ''))
        date_str = msg.get('Date', '')
        message_id = msg.get('Message-ID', msg_id.decode('utf-8', errors='replace'))
        
        # Parse date
        date = None
        if date_str:
            try:
                date_tuple = email.utils.parsedate_to_datetime(date_str)
                date = date_tuple
            except (TypeError, ValueError, OverflowError) as exc:
                logger.debug("Invalid message date header %r: %s", date_str, exc)
                date = datetime.now()
        else:
            date = datetime.now()
        
        # Get attachment info (requires fetching structure)
        attachments = self._get_attachment_info(msg_id)
        
        return MessageMetadata(
            message_id=message_id.strip('<>'),
            from_address=from_addr,
            subject=subject,
            date=date,
            is_read=is_read,
            mailbox=mailbox,
            size=size,
            attachments=attachments
        )

    def _get_attachment_info(self, msg_id: bytes) -> List[AttachmentMetadata]:
        """Get attachment information for a message."""
        attachments = []
        
        try:
            status, data = self._imap.fetch(msg_id, '(BODYSTRUCTURE)')
            if status != 'OK' or not data or not data[0]:
                return attachments
            
            # Parse BODYSTRUCTURE - this is complex, simplified version
            structure = data[0]
            if isinstance(structure, tuple):
                structure = structure[1] if len(structure) > 1 else structure[0]
            
            structure_str = structure.decode('utf-8', errors='replace') if isinstance(structure, bytes) else str(structure)
            
            # Find attachment parts (simplified parsing)
            # Look for "attachment" disposition or common attachment types
            part_num = 1
            for match in re.finditer(
                r'"([^"]+)"\s+"([^"]+)"[^)]*?(?:"attachment"|"ATTACHMENT")[^)]*?"([^"]+)"',
                structure_str
            ):
                content_type = f"{match.group(1)}/{match.group(2)}"
                filename = match.group(3)
                
                attachments.append(AttachmentMetadata(
                    attachment_id=str(part_num),
                    filename=filename,
                    size=0,  # Size not easily available from BODYSTRUCTURE
                    content_type=content_type.lower()
                ))
                part_num += 1
                
        except (imaplib.IMAP4.error, UnicodeError, AttributeError, IndexError) as exc:
            logger.debug("Could not parse attachment structure: %s", exc)
        
        return attachments

    def _decode_header(self, header: str) -> str:
        """Decode an email header."""
        if not header:
            return ''
        
        decoded_parts = []
        for part, encoding in email.header.decode_header(header):
            if isinstance(part, bytes):
                try:
                    decoded_parts.append(part.decode(encoding or 'utf-8', errors='replace'))
                except (LookupError, UnicodeDecodeError):
                    decoded_parts.append(part.decode('utf-8', errors='replace'))
            else:
                decoded_parts.append(part)
        
        return ' '.join(decoded_parts)

    def fetch_message(self, message_id: str) -> MessageMetadata:
        """
        Fetch full message including body.
        
        Args:
            message_id: Message ID
            
        Returns:
            Full message metadata
        """
        self._ensure_connected()
        
        try:
            # Search for message by Message-ID header
            status, data = self._imap.search(None, f'HEADER Message-ID "<{message_id}>"')
            if status != 'OK' or not data[0]:
                # Try without angle brackets
                status, data = self._imap.search(None, f'HEADER Message-ID "{message_id}"')
                if status != 'OK' or not data[0]:
                    raise AdapterError(
                        AdapterErrorCode.MESSAGE_NOT_FOUND,
                        f"Message not found: {message_id}"
                    )
            
            msg_num = data[0].split()[0]
            
            # Fetch full message
            status, data = self._imap.fetch(msg_num, '(FLAGS RFC822)')
            if status != 'OK' or not data or not data[0]:
                raise AdapterError(
                    AdapterErrorCode.MESSAGE_NOT_FOUND,
                    f"Failed to fetch message: {message_id}"
                )
            
            response_part = data[0]
            if isinstance(response_part, tuple):
                flags_data = response_part[0]
                msg_data = response_part[1]
            else:
                raise AdapterError(
                    AdapterErrorCode.INVALID_RESPONSE,
                    "Invalid message response"
                )
            
            # Parse flags
            flags_match = re.search(rb'FLAGS \(([^)]*)\)', flags_data)
            flags = []
            if flags_match:
                flags = flags_match.group(1).decode('utf-8', errors='replace').split()
            is_read = '\\Seen' in flags
            
            # Parse message
            msg = email.message_from_bytes(msg_data)
            
            from_addr = self._decode_header(msg.get('From', ''))
            subject = self._decode_header(msg.get('Subject', ''))
            date_str = msg.get('Date', '')
            
            # Parse date
            date = datetime.now()
            if date_str:
                try:
                    date = email.utils.parsedate_to_datetime(date_str)
                except (TypeError, ValueError, OverflowError) as exc:
                    logger.debug("Invalid message date header %r: %s", date_str, exc)
            
            # Get body
            body_preview, body_html = self._extract_body(msg)
            
            # Get attachments
            attachments = self._extract_attachments_info(msg)
            
            return MessageMetadata(
                message_id=message_id,
                from_address=from_addr,
                subject=subject,
                date=date,
                is_read=is_read,
                mailbox=self._current_mailbox or 'INBOX',
                size=len(msg_data),
                attachments=attachments,
                body_preview=body_preview[:500] if body_preview else '',
                body_html=body_html
            )
            
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError(
                AdapterErrorCode.PROTOCOL_ERROR,
                "Failed to fetch message",
                str(e)
            )

    def _extract_body(self, msg: email.message.Message) -> Tuple[str, str]:
        """Extract plain text and HTML body from message."""
        body_plain = ''
        body_html = ''
        
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                disposition = str(part.get('Content-Disposition', ''))
                
                if 'attachment' in disposition:
                    continue
                
                if content_type == 'text/plain' and not body_plain:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or 'utf-8'
                        body_plain = payload.decode(charset, errors='replace')
                
                elif content_type == 'text/html' and not body_html:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or 'utf-8'
                        body_html = payload.decode(charset, errors='replace')
        else:
            content_type = msg.get_content_type()
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or 'utf-8'
                text = payload.decode(charset, errors='replace')
                if content_type == 'text/html':
                    body_html = text
                else:
                    body_plain = text
        
        return body_plain, body_html

    def _extract_attachments_info(
        self,
        msg: email.message.Message
    ) -> List[AttachmentMetadata]:
        """Extract attachment information from message."""
        attachments = []
        part_num = 0
        
        for part in msg.walk():
            disposition = str(part.get('Content-Disposition', ''))
            
            if 'attachment' in disposition or part.get_filename():
                filename = part.get_filename()
                if filename:
                    filename = self._decode_header(filename)
                else:
                    filename = f'attachment_{part_num}'
                
                content_type = part.get_content_type()
                payload = part.get_payload(decode=True)
                size = len(payload) if payload else 0
                
                attachments.append(AttachmentMetadata(
                    attachment_id=str(part_num),
                    filename=filename,
                    size=size,
                    content_type=content_type
                ))
            
            part_num += 1
        
        return attachments

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
            attachment_id: Attachment ID (part number)
            stream_callback: Callback to receive data
        """
        self._ensure_connected()
        
        try:
            # Search for message
            status, data = self._imap.search(None, f'HEADER Message-ID "<{message_id}>"')
            if status != 'OK' or not data[0]:
                status, data = self._imap.search(None, f'HEADER Message-ID "{message_id}"')
                if status != 'OK' or not data[0]:
                    raise AdapterError(
                        AdapterErrorCode.MESSAGE_NOT_FOUND,
                        f"Message not found: {message_id}"
                    )
            
            msg_num = data[0].split()[0]
            
            # Fetch full message
            status, data = self._imap.fetch(msg_num, '(RFC822)')
            if status != 'OK' or not data or not data[0]:
                raise AdapterError(
                    AdapterErrorCode.MESSAGE_NOT_FOUND,
                    "Failed to fetch message"
                )
            
            msg_data = data[0][1] if isinstance(data[0], tuple) else data[0]
            msg = email.message_from_bytes(msg_data)
            
            # Find attachment by part number
            target_part = int(attachment_id)
            part_num = 0
            
            for part in msg.walk():
                disposition = str(part.get('Content-Disposition', ''))
                
                if 'attachment' in disposition or part.get_filename():
                    if part_num == target_part:
                        payload = part.get_payload(decode=True)
                        if payload:
                            # Stream in chunks
                            chunk_size = 65536
                            for i in range(0, len(payload), chunk_size):
                                stream_callback(payload[i:i + chunk_size])
                            return
                    part_num += 1
            
            raise AdapterError(
                AdapterErrorCode.ATTACHMENT_NOT_FOUND,
                f"Attachment not found: {attachment_id}"
            )
            
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError(
                AdapterErrorCode.PROTOCOL_ERROR,
                "Failed to fetch attachment",
                str(e)
            )

    def mark_as_read(self, message_id: str, read: bool = True) -> None:
        """
        Mark message as read or unread.
        
        Args:
            message_id: Message ID
            read: True for read, False for unread
        """
        self._ensure_connected()
        
        try:
            # Search for message
            status, data = self._imap.search(None, f'HEADER Message-ID "<{message_id}>"')
            if status != 'OK' or not data[0]:
                status, data = self._imap.search(None, f'HEADER Message-ID "{message_id}"')
                if status != 'OK' or not data[0]:
                    raise AdapterError(
                        AdapterErrorCode.MESSAGE_NOT_FOUND,
                        f"Message not found: {message_id}"
                    )
            
            msg_num = data[0].split()[0]
            
            if read:
                self._imap.store(msg_num, '+FLAGS', '\\Seen')
            else:
                self._imap.store(msg_num, '-FLAGS', '\\Seen')
                
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError(
                AdapterErrorCode.PROTOCOL_ERROR,
                "Failed to mark message",
                str(e)
            )

    def supports_search(self) -> bool:
        """IMAP supports server-side search."""
        return True

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
            query: Search query (searches subject and from)
            limit: Maximum results
            
        Returns:
            List of matching messages
        """
        self._ensure_connected()
        
        try:
            self._select_mailbox(mailbox)
            
            # Search in subject and from
            # IMAP search syntax
            search_criteria = f'OR SUBJECT "{query}" FROM "{query}"'
            
            status, data = self._imap.search(None, search_criteria)
            if status != 'OK':
                return []
            
            message_ids = data[0].split()
            if not message_ids:
                return []
            
            # Reverse and limit
            message_ids = list(reversed(message_ids))[:limit]
            
            messages = []
            for msg_id in message_ids:
                msg_metadata = None
                try:
                    msg_metadata = self._fetch_message_headers(msg_id, mailbox)
                except Exception as exc:
                    logger.debug("Search: skipping message %s: %s", msg_id, exc)
                if msg_metadata:
                    messages.append(msg_metadata)
            
            return messages
            
        except Exception as e:
            raise AdapterError(
                AdapterErrorCode.PROTOCOL_ERROR,
                "Search failed",
                str(e)
            )
