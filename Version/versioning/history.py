"""
GeoInbox - Version History

Manages version records and undo/redo functionality.
"""

import json
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass

from qgis.core import QgsVectorLayer, QgsFeature, QgsProject

from ..storage.database import DatabaseManager
from ..connectors.base import HostConnector
from .loader import DataLoader


@dataclass
class VersionRecord:
    """A version record from the database."""
    version_id: str
    timestamp: datetime
    user: str
    client_source: str
    host_layer: str
    host_type: str
    host_path: str
    operation_type: str
    feature_mappings: Dict[str, Any]
    field_mappings: Dict[str, Any]
    diff_summary: Dict[str, Any]
    status: str
    backup_reference: str
    undo_of: Optional[str]
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'VersionRecord':
        """Create from database dict."""
        return cls(
            version_id=data.get('version_id', ''),
            timestamp=datetime.fromisoformat(data['timestamp']) if data.get('timestamp') else datetime.now(),
            user=data.get('user', ''),
            client_source=data.get('client_source', ''),
            host_layer=data.get('host_layer', ''),
            host_type=data.get('host_type', ''),
            host_path=data.get('host_path', ''),
            operation_type=data.get('operation_type', ''),
            feature_mappings=data.get('feature_mappings', {}),
            field_mappings=data.get('field_mappings', {}),
            diff_summary=data.get('diff_summary', {}),
            status=data.get('status', ''),
            backup_reference=data.get('backup_reference', ''),
            undo_of=data.get('undo_of')
        )


class UndoStack:
    """
    Manages undo/redo operations for version history.
    """
    
    def __init__(self, max_size: int = 50):
        """Initialize the undo stack."""
        self._undo_stack: List[str] = []  # version_ids
        self._redo_stack: List[str] = []
        self._max_size = max_size
    
    def push(self, version_id: str) -> None:
        """Push a version to the undo stack."""
        self._undo_stack.append(version_id)
        self._redo_stack.clear()  # Clear redo on new action
        
        # Limit stack size
        while len(self._undo_stack) > self._max_size:
            self._undo_stack.pop(0)
    
    def can_undo(self) -> bool:
        """Check if undo is available."""
        return len(self._undo_stack) > 0
    
    def can_redo(self) -> bool:
        """Check if redo is available."""
        return len(self._redo_stack) > 0
    
    def pop_undo(self) -> Optional[str]:
        """Pop the last version for undo."""
        if not self._undo_stack:
            return None
        version_id = self._undo_stack.pop()
        self._redo_stack.append(version_id)
        return version_id
    
    def pop_redo(self) -> Optional[str]:
        """Pop the last undone version for redo."""
        if not self._redo_stack:
            return None
        version_id = self._redo_stack.pop()
        self._undo_stack.append(version_id)
        return version_id
    
    def clear(self) -> None:
        """Clear both stacks."""
        self._undo_stack.clear()
        self._redo_stack.clear()


class VersionHistory:
    """
    Manages version history and undo/redo operations.
    """
    
    def __init__(self, db: Optional[DatabaseManager] = None):
        """Initialize version history."""
        self._db = db or DatabaseManager()
        self._undo_stack = UndoStack()
        self._loader = DataLoader()
    
    def get_history(
        self,
        host_layer: Optional[str] = None,
        limit: int = 100
    ) -> List[VersionRecord]:
        """
        Get version history records.
        
        Args:
            host_layer: Optional filter by layer name
            limit: Maximum records to return
            
        Returns:
            List of version records
        """
        records = self._db.get_version_records(host_layer, limit)
        return [VersionRecord.from_dict(r) for r in records]
    
    def get_version(self, version_id: str) -> Optional[VersionRecord]:
        """Get a specific version record."""
        record = self._db.get_version_record(version_id)
        return VersionRecord.from_dict(record) if record else None
    
    def register_commit(self, version_id: str) -> None:
        """Register a new commit in the undo stack."""
        self._undo_stack.push(version_id)
    
    def can_undo(self) -> bool:
        """Check if undo is available."""
        return self._undo_stack.can_undo()
    
    def can_redo(self) -> bool:
        """Check if redo is available."""
        return self._undo_stack.can_redo()
    
    def undo(self, connector: HostConnector, user: str) -> Optional[str]:
        """
        Undo the last commit by restoring from backup.
        
        Args:
            connector: Host connector
            user: User performing the undo
            
        Returns:
            New version ID for the undo operation, or None if failed
        """
        version_id = self._undo_stack.pop_undo()
        if not version_id:
            return None
        
        record = self.get_version(version_id)
        if not record:
            return None
        
        return self._restore_from_backup(connector, record, user, is_undo=True)
    
    def redo(self, connector: HostConnector, user: str) -> Optional[str]:
        """
        Redo the last undone commit.
        
        Args:
            connector: Host connector
            user: User performing the redo
            
        Returns:
            New version ID for the redo operation, or None if failed
        """
        version_id = self._undo_stack.pop_redo()
        if not version_id:
            return None
        
        record = self.get_version(version_id)
        if not record:
            return None
        
        # For redo, we need to re-apply the original changes
        # This requires the original diff to be stored
        # For now, we'll use the backup of the undo operation
        return self._restore_from_backup(connector, record, user, is_undo=False)
    
    def _restore_from_backup(
        self,
        connector: HostConnector,
        record: VersionRecord,
        user: str,
        is_undo: bool
    ) -> Optional[str]:
        """
        Restore features from a backup file.
        
        Args:
            connector: Host connector
            record: Version record with backup reference
            user: User performing the operation
            is_undo: True for undo, False for redo
            
        Returns:
            New version ID
        """
        import uuid
        
        backup_path = record.backup_reference
        if not backup_path or not Path(backup_path).exists():
            return None
        
        try:
            # Load backup data
            backup_dataset = self._loader.load(backup_path)
            
            # Begin transaction
            connector.begin_transaction()
            
            # Get current features to backup before restore
            layer_info = connector.get_layer(record.host_layer)
            
            # Delete current features that were affected
            affected_ids = list(record.feature_mappings.values())
            host_fids = [
                m.get('host_id') for m in affected_ids
                if isinstance(m, dict) and m.get('host_id')
            ]
            
            if host_fids:
                # Get original FIDs
                original_fids = []
                for feature in connector.get_features(record.host_layer):
                    if str(feature.id()) in host_fids or feature.id() in host_fids:
                        original_fids.append(feature.id())
                
                if original_fids:
                    connector.delete_features(record.host_layer, original_fids)
            
            # Insert features from backup
            for norm_feature in backup_dataset.features:
                feature = QgsFeature()
                feature.setGeometry(norm_feature.geometry)
                
                # Set attributes
                for field in layer_info.fields:
                    field_name = field.name()
                    if field_name in norm_feature.attributes:
                        feature.setAttribute(field_name, norm_feature.attributes[field_name])
                
                connector.insert_features(record.host_layer, [feature])
            
            # Commit transaction
            connector.commit_transaction()
            
            # Create new version record for the undo/redo
            new_version_id = str(uuid.uuid4())
            
            operation = 'undo' if is_undo else 'redo'
            
            self._db.save_version_record(
                version_id=new_version_id,
                user=user,
                client_source=f"{operation}:{record.version_id}",
                host_layer=record.host_layer,
                host_type=record.host_type,
                host_path=record.host_path,
                operation_type=operation,
                feature_mappings={},
                field_mappings={},
                diff_summary={'restored_from': record.version_id},
                status='committed',
                backup_reference='',
                undo_of=record.version_id if is_undo else None
            )
            
            # Update original record status
            self._db.update_version_status(
                record.version_id,
                'rolled_back' if is_undo else 'reapplied'
            )
            
            return new_version_id
            
        except Exception as e:
            connector.rollback_transaction()
            return None
    
    def get_backup_content(self, version_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the content of a backup file for a version.
        
        Args:
            version_id: Version ID
            
        Returns:
            Parsed GeoJSON content or None
        """
        record = self.get_version(version_id)
        if not record or not record.backup_reference:
            return None
        
        backup_path = Path(record.backup_reference)
        if not backup_path.exists():
            return None
        
        try:
            with open(backup_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None
    
    def cleanup_old_backups(self, days: int = 30) -> int:
        """
        Remove backup files older than specified days.
        
        Args:
            days: Age threshold in days
            
        Returns:
            Number of files removed
        """
        from datetime import timedelta
        
        cutoff = datetime.now() - timedelta(days=days)
        records = self.get_history(limit=10000)
        
        removed = 0
        for record in records:
            if record.timestamp < cutoff and record.backup_reference:
                backup_path = Path(record.backup_reference)
                if backup_path.exists():
                    try:
                        backup_path.unlink()
                        removed += 1
                    except Exception as exc:
                        from ..utils.security import log_debug_exception
                        log_debug_exception(f"Failed to remove backup {backup_path}", exc)
        
        return removed
