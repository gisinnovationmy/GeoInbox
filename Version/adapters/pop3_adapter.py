"""
GeoInbox - POP3 Adapter

POP3 email protocol adapter using Python's poplib.
Supports SSL/TLS.
"""

import poplib
import email
import email.header
import email.utils
import ssl
from typing import List, Optional, Callable
from datetime import datetime

from .base import (
    EmailAdapter,
    AdapterConfig,
    MessageMetadata,
    AttachmentMetadata,
    MailboxInfo
)
from ..utils.errors import AdapterError, AdapterErrorCode
from ..utils.security import log_debug_exception


class POP3Adapter(EmailAdapter):
    """POP3 email protocol adapter."""

    def __init__(self):
        """Initialize the POP3 adapter."""
        super().__init__()
        self._pop: Optional[poplib.POP3_SSL] = None
        self._messages_cache: List[MessageMetadata] = []

    def get_protocol_name(self) -> str:
        """Get the protocol name."""
        return 'pop3'

    def supports_mailboxes(self) -> bool:
        """POP3 does not support multiple mailboxes."""
        return False

    def supports_mark_as_read(self) -> bool:
        """POP3 does not support marking as read (only delete)."""
        return False

    def connect(self, config: AdapterConfig) -> None:
        """
        Connect to POP3 server.
        
        Args:
            config: Connection configuration
            
        Raises:
            AdapterError: If connection fails
        """
        self._config = config
        
        try:
            if config.use_ssl:
                context = ssl.create_default_context()
                self._pop = poplib.POP3_SSL(
                    config.host,
                    config.port,
                    context=context,
                    timeout=config.timeout
                )
            else:
                self._pop = poplib.POP3(
                    config.host,
                    config.port,
                    timeout=config.timeout
                )
                if config.use_tls:
                    context = ssl.create_default_context()
                    self._pop.stls(context=context)
            
            # Authenticate
            self._pop.user(config.username)
            self._pop.pass_(config.password)
            
            self._connected = True
            
        except poplib.error_proto as e:
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
                "Failed to connect to POP3 server",
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
                "Failed to connect to POP3 server",
                str(e)
            )

    def disconnect(self) -> None:
        """Close connection to POP3 server."""
        if self._pop:
            try:
                self._pop.quit()
            except Exception as exc:
                log_debug_exception("POP3 quit failed during disconnect", exc)
            self._pop = None
        self._connected = False
        self._messages_cache.clear()

    def _ensure_connected(self) -> None:
        """Ensure we are connected."""
        if not self._connected or not self._pop:
            raise AdapterError(
                AdapterErrorCode.CONNECTION_FAILED,
                "Not connected to server"
            )

    def list_mailboxes(self) -> List[MailboxInfo]:
        """
        List available mailboxes.
        POP3 only has one mailbox (INBOX).
        """
        self._ensure_connected()
        
        try:
            # Get message count
            count, size = self._pop.stat()
            
            return [MailboxInfo(
                name='INBOX',
                message_count=count,
                unread_count=count,  # POP3 doesn't track read status
                delimiter='/',
                flags=[]
            )]
            
        except Exception as e:
            raise AdapterError(
                AdapterErrorCode.PROTOCOL_ERROR,
                "Failed to get mailbox info",
                str(e)
            )

    def list_messages(
        self,
        mailbox: str,
        filter_read: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[MessageMetadata]:
        """
        List messages in mailbox.
        POP3 ignores mailbox parameter and filter_read.
        """
        self._ensure_connected()
        
        try:
            # Get message list
            response, msg_list, octets = self._pop.list()
            
            messages = []
            msg_nums = []
            
            for item in msg_list:
                if isinstance(item, bytes):
                    item = item.decode('utf-8', errors='replace')
                parts = item.split()
                if len(parts) >= 2:
                    msg_nums.append((int(parts[0]), int(parts[1])))
            
            # Reverse to get newest first, apply offset and limit
            msg_nums = list(reversed(msg_nums))
            msg_nums = msg_nums[offset:offset + limit]
            
            for msg_num, size in msg_nums:
                try:
                    msg_metadata = self._fetch_message_headers(msg_num, size)
                    if msg_metadata:
                        messages.append(msg_metadata)
                except Exception as exc:
                    log_debug_exception(f"POP3 header fetch failed for message {msg_num}", exc)
                    continue
            
            self._messages_cache = messages
            return messages
            
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError(
                AdapterErrorCode.PROTOCOL_ERROR,
                "Failed to list messages",
                str(e)
            )

    def _fetch_message_headers(self, msg_num: int, size: int) -> Optional[MessageMetadata]:
        """Fetch message headers only using TOP command."""
        try:
            # TOP retrieves headers + n lines of body
            response, lines, octets = self._pop.top(msg_num, 0)
            
            # Parse headers
            header_data = b'\r\n'.join(lines)
            msg = email.message_from_bytes(header_data)
            
            from_addr = self._decode_header(msg.get('From', ''))
            subject = self._decode_header(msg.get('Subject', ''))
            date_str = msg.get('Date', '')
            message_id = msg.get('Message-ID', str(msg_num))
            
            # Parse date
            date = datetime.now()
            if date_str:
                try:
                    date = email.utils.parsedate_to_datetime(date_str)
                except (TypeError, ValueError, OverflowError):
                    pass
            
            return MessageMetadata(
                message_id=message_id.strip('<>') if message_id else str(msg_num),
                from_address=from_addr,
                subject=subject,
                date=date,
                is_read=False,  # POP3 doesn't track read status
                mailbox='INBOX',
                size=size,
                attachments=[]  # Will be populated on full fetch
            )
            
        except Exception:
            return None

    def _decode_header(self, header: str) -> str:
        """Decode an email header."""
        if not header:
            return ''
        
        decoded_parts = []
        for part, encoding in email.header.decode_header(header):
            if isinstance(part, bytes):
                try:
                    decoded_parts.append(part.decode(encoding or 'utf-8', errors='replace'))
                except Exception:
                    decoded_parts.append(part.decode('utf-8', errors='replace'))
            else:
                decoded_parts.append(part)
        
        return ' '.join(decoded_parts)

    def fetch_message(self, message_id: str) -> MessageMetadata:
        """
        Fetch full message including body.
        
        Args:
            message_id: Message ID or message number
            
        Returns:
            Full message metadata
        """
        self._ensure_connected()
        
        try:
            # Find message number
            msg_num = self._find_message_num(message_id)
            if msg_num is None:
                raise AdapterError(
                    AdapterErrorCode.MESSAGE_NOT_FOUND,
                    f"Message not found: {message_id}"
                )
            
            # Fetch full message
            response, lines, octets = self._pop.retr(msg_num)
            msg_data = b'\r\n'.join(lines)
            msg = email.message_from_bytes(msg_data)
            
            from_addr = self._decode_header(msg.get('From', ''))
            subject = self._decode_header(msg.get('Subject', ''))
            date_str = msg.get('Date', '')
            
            # Parse date
            date = datetime.now()
            if date_str:
                try:
                    date = email.utils.parsedate_to_datetime(date_str)
                except (TypeError, ValueError, OverflowError):
                    pass
            
            # Get body
            body_preview, body_html = self._extract_body(msg)
            
            # Get attachments
            attachments = self._extract_attachments_info(msg)
            
            return MessageMetadata(
                message_id=message_id,
                from_address=from_addr,
                subject=subject,
                date=date,
                is_read=False,
                mailbox='INBOX',
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

    def _find_message_num(self, message_id: str) -> Optional[int]:
        """Find message number by message ID."""
        # Try to parse as integer first
        try:
            return int(message_id)
        except ValueError:
            pass
        
        # Search through cached messages
        for i, msg in enumerate(self._messages_cache, 1):
            if msg.message_id == message_id:
                return i
        
        return None

    def _extract_body(self, msg: email.message.Message) -> tuple:
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

    def _extract_attachments_info(self, msg: email.message.Message) -> List[AttachmentMetadata]:
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
            msg_num = self._find_message_num(message_id)
            if msg_num is None:
                raise AdapterError(
                    AdapterErrorCode.MESSAGE_NOT_FOUND,
                    f"Message not found: {message_id}"
                )
            
            # Fetch full message
            response, lines, octets = self._pop.retr(msg_num)
            msg_data = b'\r\n'.join(lines)
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
        POP3 does not support marking as read.
        This method does nothing.
        """
        pass
