"""
GeoInbox - Message Cache

Local caching of message headers for offline browsing.
"""

from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from .database import DatabaseManager


class MessageCache:
    """
    Cache for email message headers.
    
    Provides offline browsing capability with configurable TTL.
    """
    
    def __init__(self, db: Optional[DatabaseManager] = None):
        """
        Initialize the message cache.
        
        Args:
            db: Database manager instance
        """
        self._db = db or DatabaseManager()
    
    def get_ttl_hours(self) -> int:
        """Get the configured cache TTL in hours."""
        return int(self._db.get_setting('cache_ttl', '24'))
    
    def is_cache_valid(self, cached_at: str) -> bool:
        """
        Check if a cached entry is still valid.
        
        Args:
            cached_at: ISO format timestamp of when entry was cached
            
        Returns:
            True if cache entry is still valid
        """
        try:
            cached_time = datetime.fromisoformat(cached_at)
            ttl = timedelta(hours=self.get_ttl_hours())
            return datetime.now() - cached_time < ttl
        except (ValueError, TypeError):
            return False
    
    def cache_message(
        self,
        server_id: int,
        message_id: str,
        mailbox: str,
        from_address: str,
        subject: str,
        date: str,
        size: int,
        is_read: bool,
        is_trusted: bool,
        attachments: List[Dict[str, Any]]
    ) -> None:
        """
        Cache a message header.
        
        Args:
            server_id: Email server ID
            message_id: Message ID
            mailbox: Mailbox name
            from_address: Sender address
            subject: Message subject
            date: Message date (ISO format)
            size: Message size in bytes
            is_read: Read status
            is_trusted: Trust status
            attachments: List of attachment metadata dicts
        """
        self._db.cache_message(
            message_id=message_id,
            server_id=server_id,
            mailbox=mailbox,
            from_address=from_address,
            subject=subject,
            date=date,
            size=size,
            is_read=is_read,
            is_trusted=is_trusted,
            attachments=attachments
        )
    
    def cache_messages(
        self,
        server_id: int,
        mailbox: str,
        messages: List[Dict[str, Any]]
    ) -> None:
        """
        Cache multiple message headers.
        
        Args:
            server_id: Email server ID
            mailbox: Mailbox name
            messages: List of message metadata dicts
        """
        for msg in messages:
            self.cache_message(
                server_id=server_id,
                message_id=msg.get('message_id', ''),
                mailbox=mailbox,
                from_address=msg.get('from_address', ''),
                subject=msg.get('subject', ''),
                date=msg.get('date', ''),
                size=msg.get('size', 0),
                is_read=msg.get('is_read', False),
                is_trusted=msg.get('is_trusted', False),
                attachments=msg.get('attachments', [])
            )
    
    def get_cached_messages(
        self,
        server_id: int,
        mailbox: str,
        limit: int = 100,
        include_expired: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Get cached messages for a mailbox.
        
        Args:
            server_id: Email server ID
            mailbox: Mailbox name
            limit: Maximum messages to return
            include_expired: Include expired cache entries
            
        Returns:
            List of cached message dicts
        """
        messages = self._db.get_cached_messages(server_id, mailbox, limit)
        
        if include_expired:
            return messages
        
        # Filter out expired entries
        valid_messages = []
        for msg in messages:
            cached_at = msg.get('cached_at', '')
            if self.is_cache_valid(cached_at):
                valid_messages.append(msg)
        
        return valid_messages
    
    def update_read_status(self, message_id: str, is_read: bool) -> None:
        """
        Update the read status of a cached message.
        
        Args:
            message_id: Message ID
            is_read: New read status
        """
        # Re-cache with updated status
        messages = self._db.get_cached_messages(0, '', 1000)  # Get all
        for msg in messages:
            if msg.get('message_id') == message_id:
                self._db.cache_message(
                    message_id=message_id,
                    server_id=msg.get('server_id', 0),
                    mailbox=msg.get('mailbox', ''),
                    from_address=msg.get('from_address', ''),
                    subject=msg.get('subject', ''),
                    date=msg.get('date', ''),
                    size=msg.get('size', 0),
                    is_read=is_read,
                    is_trusted=msg.get('is_trusted', False),
                    attachments=msg.get('attachments', [])
                )
                break
    
    def clear_cache(self, server_id: Optional[int] = None) -> int:
        """
        Clear the message cache.
        
        Args:
            server_id: Optional server ID to clear cache for
            
        Returns:
            Number of entries cleared
        """
        return self._db.clear_message_cache(server_id=server_id)
    
    def clear_expired(self) -> int:
        """
        Clear expired cache entries.
        
        Returns:
            Number of entries cleared
        """
        ttl_hours = self.get_ttl_hours()
        cutoff = datetime.now() - timedelta(hours=ttl_hours)
        return self._db.clear_message_cache(older_than=cutoff)
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.
        
        Returns:
            Dictionary with cache stats
        """
        all_messages = self._db.get_cached_messages(0, '', 10000)
        
        total = len(all_messages)
        valid = sum(1 for m in all_messages if self.is_cache_valid(m.get('cached_at', '')))
        expired = total - valid
        
        # Group by server
        by_server = {}
        for msg in all_messages:
            server_id = msg.get('server_id', 0)
            if server_id not in by_server:
                by_server[server_id] = 0
            by_server[server_id] += 1
        
        return {
            'total_entries': total,
            'valid_entries': valid,
            'expired_entries': expired,
            'ttl_hours': self.get_ttl_hours(),
            'by_server': by_server
        }
