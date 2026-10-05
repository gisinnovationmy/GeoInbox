"""
GeoInbox - Commit Workflow

Handles committing changes from client to host with backup and transaction support.
"""

import uuid
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass

from qgis.core import QgsFeature, QgsGeometry

from .loader import NormalizedFeature, LoadedDataset
from .matcher import FeatureMatch, MatchResult
from .differ import FeatureDiff, DiffSummary, DiffType
from ..connectors.base import HostConnector
from ..storage.sanitizer import get_backup_path
from ..storage.database import DatabaseManager
from ..utils.errors import ConnectorError


@dataclass
class CommitOperation:
    """A single operation in a commit."""
    operation_type: str  # 'insert', 'update', 'delete'
    feature_id: str
    client_feature: Optional[NormalizedFeature]
    host_feature: Optional[NormalizedFeature]
    attributes: Dict[str, Any]
    geometry: Optional[QgsGeometry]


@dataclass
class CommitResult:
    """Result of a commit operation."""
    success: bool
    version_id: str
    timestamp: datetime
    operations_count: int
    inserted: int
    updated: int
    deleted: int
    backup_path: Optional[str]
    error_message: Optional[str] = None


class Committer:
    """
    Handles committing changes from client dataset to host.
    
    Workflow:
    1. Create backup of affected host features
    2. Begin transaction
    3. Apply changes (insert/update/delete)
    4. Commit transaction
    5. Create version record
    """
    
    def __init__(self, db: Optional[DatabaseManager] = None):
        """Initialize the committer."""
        self._db = db or DatabaseManager()
    
    def commit(
        self,
        connector: HostConnector,
        layer_name: str,
        diff_summary: DiffSummary,
        client_source: str,
        user: str,
        field_mappings: Optional[Dict[str, str]] = None
    ) -> CommitResult:
        """
        Commit changes to the host dataset.
        
        Args:
            connector: Host connector (GeoPackage or PostGIS)
            layer_name: Target layer name
            diff_summary: Computed differences
            client_source: Source identifier (email ID or gisimple URI)
            user: User performing the commit
            field_mappings: Optional field name mappings (client -> host)
            
        Returns:
            CommitResult with operation details
        """
        version_id = str(uuid.uuid4())
        timestamp = datetime.now()
        backup_path = None
        
        # Get features to backup (all affected host features)
        affected_host_ids = []
        for diff in diff_summary.feature_diffs:
            if diff.match.host_feature:
                affected_host_ids.append(diff.match.host_feature.original_fid)
        
        try:
            # Step 1: Create backup
            if affected_host_ids:
                backup_path = self._create_backup(
                    connector,
                    layer_name,
                    affected_host_ids,
                    client_source,
                    version_id
                )
            
            # Step 2: Begin transaction
            connector.begin_transaction()
            
            # Step 3: Apply changes
            inserted = 0
            updated = 0
            deleted = 0
            
            for diff in diff_summary.feature_diffs:
                if diff.diff_type == DiffType.NEW:
                    # Insert new feature
                    self._insert_feature(
                        connector,
                        layer_name,
                        diff.match.client_feature,
                        field_mappings
                    )
                    inserted += 1
                    
                elif diff.diff_type in {DiffType.ATTRIBUTE_ONLY, DiffType.GEOMETRY_ONLY, DiffType.BOTH}:
                    # Update existing feature
                    self._update_feature(
                        connector,
                        layer_name,
                        diff,
                        field_mappings
                    )
                    updated += 1
                    
                elif diff.diff_type == DiffType.DELETED:
                    # Delete feature
                    if diff.match.host_feature:
                        self._delete_feature(
                            connector,
                            layer_name,
                            diff.match.host_feature
                        )
                        deleted += 1
            
            # Step 4: Commit transaction
            connector.commit_transaction()
            
            # Step 5: Create version record
            self._create_version_record(
                version_id=version_id,
                timestamp=timestamp,
                user=user,
                client_source=client_source,
                layer_name=layer_name,
                connector=connector,
                diff_summary=diff_summary,
                field_mappings=field_mappings,
                backup_path=backup_path
            )
            
            return CommitResult(
                success=True,
                version_id=version_id,
                timestamp=timestamp,
                operations_count=inserted + updated + deleted,
                inserted=inserted,
                updated=updated,
                deleted=deleted,
                backup_path=backup_path
            )
            
        except Exception as e:
            # Rollback on error
            connector.rollback_transaction()
            
            return CommitResult(
                success=False,
                version_id=version_id,
                timestamp=timestamp,
                operations_count=0,
                inserted=0,
                updated=0,
                deleted=0,
                backup_path=backup_path,
                error_message=str(e)
            )
    
    def _create_backup(
        self,
        connector: HostConnector,
        layer_name: str,
        feature_ids: List[int],
        client_source: str,
        version_id: str
    ) -> str:
        """Create a GeoJSON backup of affected features."""
        # Extract sender from client_source
        if client_source.startswith('gisimpleserver://'):
            sender = 'gisimple'
        elif '@' in client_source:
            sender = client_source.split('@')[0]
        else:
            sender = 'local'
        
        backup_path = get_backup_path(layer_name, sender, version_id)
        
        connector.export_features_to_geojson(
            layer_name,
            str(backup_path),
            feature_ids
        )
        
        return str(backup_path)
    
    def _insert_feature(
        self,
        connector: HostConnector,
        layer_name: str,
        client_feature: NormalizedFeature,
        field_mappings: Optional[Dict[str, str]] = None
    ) -> None:
        """Insert a new feature."""
        # Create QgsFeature
        feature = QgsFeature()
        feature.setGeometry(client_feature.geometry)
        
        # Map attributes
        attrs = self._map_attributes(
            client_feature.attributes,
            field_mappings
        )
        
        # Set attributes (connector will handle field mapping)
        layer_info = connector.get_layer(layer_name)
        for field in layer_info.fields:
            field_name = field.name()
            if field_name in attrs:
                feature.setAttribute(field_name, attrs[field_name])
        
        connector.insert_features(layer_name, [feature])
    
    def _update_feature(
        self,
        connector: HostConnector,
        layer_name: str,
        diff: FeatureDiff,
        field_mappings: Optional[Dict[str, str]] = None
    ) -> None:
        """Update an existing feature."""
        host_feature = diff.match.host_feature
        client_feature = diff.match.client_feature
        
        update = {
            'id': host_feature.original_fid
        }
        
        # Update geometry if changed
        if diff.diff_type in {DiffType.GEOMETRY_ONLY, DiffType.BOTH}:
            update['geometry'] = client_feature.geometry
        
        # Update attributes if changed
        if diff.diff_type in {DiffType.ATTRIBUTE_ONLY, DiffType.BOTH}:
            changed_attrs = {}
            for attr_diff in diff.attribute_diffs:
                if attr_diff.is_different:
                    # Map field name if needed
                    host_field = attr_diff.field_name
                    if field_mappings and attr_diff.field_name in field_mappings:
                        host_field = field_mappings[attr_diff.field_name]
                    changed_attrs[host_field] = attr_diff.client_value
            
            if changed_attrs:
                update['attributes'] = changed_attrs
        
        connector.update_features(layer_name, [update])
    
    def _delete_feature(
        self,
        connector: HostConnector,
        layer_name: str,
        host_feature: NormalizedFeature
    ) -> None:
        """Delete a feature."""
        connector.delete_features(layer_name, [host_feature.original_fid])
    
    def _map_attributes(
        self,
        attributes: Dict[str, Any],
        field_mappings: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Map attribute names from client to host."""
        if not field_mappings:
            return attributes
        
        mapped = {}
        for client_field, value in attributes.items():
            host_field = field_mappings.get(client_field, client_field)
            mapped[host_field] = value
        
        return mapped
    
    def _create_version_record(
        self,
        version_id: str,
        timestamp: datetime,
        user: str,
        client_source: str,
        layer_name: str,
        connector: HostConnector,
        diff_summary: DiffSummary,
        field_mappings: Optional[Dict[str, str]],
        backup_path: Optional[str]
    ) -> None:
        """Create a version record in the database."""
        # Determine operation type
        if diff_summary.geometry_only > 0 or diff_summary.both_changed > 0:
            operation_type = 'geometry-update'
        else:
            operation_type = 'attribute-only'
        
        # Build feature mappings
        feature_mappings = {}
        for diff in diff_summary.feature_diffs:
            if diff.match.client_feature and diff.match.host_feature:
                feature_mappings[diff.match.client_feature.feature_id] = {
                    'host_id': diff.match.host_feature.feature_id,
                    'match_type': diff.match.match_type.value if diff.match.match_type else None
                }
        
        self._db.save_version_record(
            version_id=version_id,
            user=user,
            client_source=client_source,
            host_layer=layer_name,
            host_type=connector.get_connector_type(),
            host_path='',  # Will be set by connector
            operation_type=operation_type,
            feature_mappings=feature_mappings,
            field_mappings=field_mappings or {},
            diff_summary=diff_summary.to_dict(),
            status='committed',
            backup_reference=backup_path or ''
        )
