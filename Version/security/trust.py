"""
GeoInbox - Trust Evaluator

Evaluates sender trust based on configured trust mode and rules.
"""

import ipaddress
import re
from typing import Optional, List, Set
from enum import Enum

from ..storage.database import DatabaseManager, TrustRule


class TrustMode(Enum):
    """Trust evaluation modes."""
    ACCOUNTS = "accounts"  # Trust only configured accounts
    NETWORK = "network"    # Trust based on CIDR ranges
    ALL = "all"            # Trust all senders


class TrustEvaluator:
    """
    Evaluates whether a sender should be trusted.
    
    Trust modes:
    - ACCOUNTS: Trust senders matching configured email/domain rules
    - NETWORK: Trust senders from configured CIDR ranges
    - ALL: Trust all senders (no confirmation required)
    """
    
    def __init__(self, db: Optional[DatabaseManager] = None):
        """
        Initialize the trust evaluator.
        
        Args:
            db: Database manager instance (creates new if None)
        """
        self._db = db or DatabaseManager()
        self._trusted_emails: Set[str] = set()
        self._trusted_domains: Set[str] = set()
        self._trusted_cidrs: List[ipaddress.IPv4Network] = []
        self._mode = TrustMode.ACCOUNTS
        self._loaded = False
    
    def _load_rules(self) -> None:
        """Load trust rules from database."""
        if self._loaded:
            return
        
        # Load trust mode
        mode_str = self._db.get_setting('trust_mode', 'accounts')
        try:
            self._mode = TrustMode(mode_str)
        except ValueError:
            self._mode = TrustMode.ACCOUNTS
        
        # Load rules
        rules = self._db.get_trust_rules()
        
        self._trusted_emails.clear()
        self._trusted_domains.clear()
        self._trusted_cidrs.clear()
        
        for rule in rules:
            if not rule.enabled:
                continue
            
            if rule.rule_type == 'email':
                self._trusted_emails.add(rule.value.lower())
            elif rule.rule_type == 'domain':
                self._trusted_domains.add(rule.value.lower())
            elif rule.rule_type == 'cidr':
                try:
                    network = ipaddress.ip_network(rule.value, strict=False)
                    self._trusted_cidrs.append(network)
                except ValueError:
                    pass  # Invalid CIDR, skip
        
        self._loaded = True
    
    def reload_rules(self) -> None:
        """Force reload of trust rules."""
        self._loaded = False
        self._load_rules()
    
    @property
    def mode(self) -> TrustMode:
        """Get the current trust mode."""
        self._load_rules()
        return self._mode
    
    def is_trusted(
        self,
        from_address: str,
        server_ip: Optional[str] = None,
        gisimple_trusted: Optional[bool] = None
    ) -> bool:
        """
        Evaluate if a sender is trusted.
        
        Args:
            from_address: The sender's email address
            server_ip: Optional IP address of the sending server
            gisimple_trusted: Optional trust status from GISimple server
            
        Returns:
            True if the sender is trusted
        """
        self._load_rules()
        
        # Trust All mode - always trust
        if self._mode == TrustMode.ALL:
            return True
        
        # If GISimple provides trust status, use it
        if gisimple_trusted is not None:
            return gisimple_trusted
        
        # Network mode - check CIDR ranges
        if self._mode == TrustMode.NETWORK and server_ip:
            try:
                ip = ipaddress.ip_address(server_ip)
                for network in self._trusted_cidrs:
                    if ip in network:
                        return True
            except ValueError:
                pass  # Invalid IP, continue to other checks
        
        # Accounts mode - check email and domain rules
        if self._mode == TrustMode.ACCOUNTS or self._mode == TrustMode.NETWORK:
            # Extract email address from "Name <email>" format
            email = self._extract_email(from_address)
            
            # Check exact email match
            if email.lower() in self._trusted_emails:
                return True
            
            # Check domain match
            domain = self._extract_domain(email)
            if domain and domain.lower() in self._trusted_domains:
                return True
        
        return False
    
    def _extract_email(self, from_address: str) -> str:
        """Extract email address from a From header value."""
        # Handle "Name <email@domain.com>" format
        match = re.search(r'<([^>]+)>', from_address)
        if match:
            return match.group(1)
        
        # Handle plain email
        if '@' in from_address:
            return from_address.strip()
        
        return from_address
    
    def _extract_domain(self, email: str) -> Optional[str]:
        """Extract domain from an email address."""
        if '@' in email:
            return email.split('@')[1]
        return None
    
    def get_trust_info(self, from_address: str) -> dict:
        """
        Get detailed trust information for a sender.
        
        Args:
            from_address: The sender's email address
            
        Returns:
            Dictionary with trust details
        """
        self._load_rules()
        
        email = self._extract_email(from_address)
        domain = self._extract_domain(email)
        
        info = {
            'email': email,
            'domain': domain,
            'mode': self._mode.value,
            'is_trusted': self.is_trusted(from_address),
            'match_type': None,
            'match_value': None
        }
        
        if self._mode == TrustMode.ALL:
            info['match_type'] = 'trust_all'
        elif email.lower() in self._trusted_emails:
            info['match_type'] = 'email'
            info['match_value'] = email.lower()
        elif domain and domain.lower() in self._trusted_domains:
            info['match_type'] = 'domain'
            info['match_value'] = domain.lower()
        
        return info
    
    def add_trusted_email(self, email: str) -> int:
        """
        Add a trusted email address.
        
        Args:
            email: Email address to trust
            
        Returns:
            Rule ID
        """
        rule = TrustRule(rule_type='email', value=email.lower(), enabled=True)
        rule_id = self._db.save_trust_rule(rule)
        self._loaded = False  # Force reload
        return rule_id
    
    def add_trusted_domain(self, domain: str) -> int:
        """
        Add a trusted domain.
        
        Args:
            domain: Domain to trust
            
        Returns:
            Rule ID
        """
        rule = TrustRule(rule_type='domain', value=domain.lower(), enabled=True)
        rule_id = self._db.save_trust_rule(rule)
        self._loaded = False
        return rule_id
    
    def add_trusted_cidr(self, cidr: str) -> int:
        """
        Add a trusted CIDR range.
        
        Args:
            cidr: CIDR notation (e.g., "192.168.1.0/24")
            
        Returns:
            Rule ID
            
        Raises:
            ValueError: If CIDR is invalid
        """
        # Validate CIDR
        ipaddress.ip_network(cidr, strict=False)
        
        rule = TrustRule(rule_type='cidr', value=cidr, enabled=True)
        rule_id = self._db.save_trust_rule(rule)
        self._loaded = False
        return rule_id
    
    def set_mode(self, mode: TrustMode) -> None:
        """
        Set the trust mode.
        
        Args:
            mode: Trust mode to set
        """
        self._db.set_setting('trust_mode', mode.value)
        self._mode = mode
    
    def sync_trusted_emails_from_gisimple(self, emails: List[str]) -> int:
        """
        Sync trusted email addresses from GISimple server.
        
        Adds all provided emails as trusted if they don't already exist.
        This is used to auto-trust all group members when connected to GISimple.
        
        Args:
            emails: List of email addresses to trust
            
        Returns:
            Number of new emails added
        """
        self._load_rules()
        
        added_count = 0
        for email in emails:
            email_lower = email.lower().strip()
            if email_lower and email_lower not in self._trusted_emails:
                self.add_trusted_email(email_lower)
                added_count += 1
        
        return added_count
    
    def get_trusted_emails(self) -> List[str]:
        """
        Get list of all trusted email addresses.
        
        Returns:
            List of trusted email addresses
        """
        self._load_rules()
        return list(self._trusted_emails)
    
    def remove_trusted_email(self, email: str) -> bool:
        """
        Remove a trusted email address.
        
        Args:
            email: Email address to remove
            
        Returns:
            True if removed, False if not found
        """
        email_lower = email.lower().strip()
        rules = self._db.get_trust_rules()
        
        for rule in rules:
            if rule.rule_type == 'email' and rule.value.lower() == email_lower:
                self._db.delete_trust_rule(rule.id)
                self._loaded = False
                return True
        
        return False
    
    def clear_gisimple_trusted_emails(self) -> int:
        """
        Clear all trusted emails that were synced from GISimple.
        
        This is useful when logging out from GISimple to remove
        auto-trusted group members.
        
        Returns:
            Number of emails removed
        """
        # For now, we don't distinguish between manually added and
        # GISimple-synced emails. In a future version, we could add
        # a 'source' field to trust rules to track this.
        # This method is a placeholder for that functionality.
        return 0
