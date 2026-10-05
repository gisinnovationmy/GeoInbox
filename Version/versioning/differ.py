"""
GeoInbox - Diff Engine

Compare features to detect attribute and geometry differences.
"""

from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass
from enum import Enum

from qgis.core import QgsGeometry

from .loader import NormalizedFeature
from .matcher import FeatureMatch


class DiffType(Enum):
    """Types of differences."""
    NO_CHANGE = "no_change"
    ATTRIBUTE_ONLY = "attribute_only"
    GEOMETRY_ONLY = "geometry_only"
    BOTH = "both"
    NEW = "new"
    DELETED = "deleted"


@dataclass
class AttributeDiff:
    """Difference in a single attribute."""
    field_name: str
    client_value: Any
    host_value: Any
    
    @property
    def is_different(self) -> bool:
        return self.client_value != self.host_value


@dataclass
class GeometryDiff:
    """Difference in geometry."""
    client_geometry: QgsGeometry
    host_geometry: QgsGeometry
    distance: float  # Distance between centroids
    area_diff: float  # Difference in area (if polygon)
    is_different: bool


@dataclass
class FeatureDiff:
    """Complete diff for a feature match."""
    match: FeatureMatch
    diff_type: DiffType
    attribute_diffs: List[AttributeDiff]
    geometry_diff: Optional[GeometryDiff]
    
    @property
    def has_changes(self) -> bool:
        return self.diff_type != DiffType.NO_CHANGE
    
    @property
    def changed_fields(self) -> List[str]:
        return [d.field_name for d in self.attribute_diffs if d.is_different]


@dataclass
class DiffSummary:
    """Summary of all differences between datasets."""
    total_features: int
    unchanged: int
    attribute_only: int
    geometry_only: int
    both_changed: int
    new_features: int
    deleted_features: int
    feature_diffs: List[FeatureDiff]
    
    @property
    def total_changes(self) -> int:
        return self.attribute_only + self.geometry_only + self.both_changed
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'total_features': self.total_features,
            'unchanged': self.unchanged,
            'attribute_only': self.attribute_only,
            'geometry_only': self.geometry_only,
            'both_changed': self.both_changed,
            'new_features': self.new_features,
            'deleted_features': self.deleted_features,
            'total_changes': self.total_changes
        }


class DiffEngine:
    """
    Engine for computing differences between matched features.
    """
    
    def __init__(self):
        """Initialize the diff engine."""
        self._geometry_tolerance: float = 0.001  # For geometry comparison
        self._ignore_fields: List[str] = []
    
    def configure(
        self,
        geometry_tolerance: float = 0.001,
        ignore_fields: Optional[List[str]] = None
    ):
        """
        Configure diff parameters.
        
        Args:
            geometry_tolerance: Tolerance for geometry equality
            ignore_fields: Fields to ignore in attribute comparison
        """
        self._geometry_tolerance = geometry_tolerance
        self._ignore_fields = ignore_fields or []
    
    def compute_diff(
        self,
        matches: List[FeatureMatch],
        unmatched_client: List[NormalizedFeature],
        unmatched_host: List[NormalizedFeature]
    ) -> DiffSummary:
        """
        Compute differences for all matches.
        
        Args:
            matches: List of feature matches
            unmatched_client: Unmatched client features (new)
            unmatched_host: Unmatched host features (deleted)
            
        Returns:
            DiffSummary with all differences
        """
        feature_diffs = []
        unchanged = 0
        attribute_only = 0
        geometry_only = 0
        both_changed = 0
        
        # Process matches
        for match in matches:
            if match.host_feature is None:
                # Unmatched - treat as new
                diff = FeatureDiff(
                    match=match,
                    diff_type=DiffType.NEW,
                    attribute_diffs=[],
                    geometry_diff=None
                )
            else:
                diff = self._compute_feature_diff(match)
                
                if diff.diff_type == DiffType.NO_CHANGE:
                    unchanged += 1
                elif diff.diff_type == DiffType.ATTRIBUTE_ONLY:
                    attribute_only += 1
                elif diff.diff_type == DiffType.GEOMETRY_ONLY:
                    geometry_only += 1
                elif diff.diff_type == DiffType.BOTH:
                    both_changed += 1
            
            feature_diffs.append(diff)
        
        # Add unmatched client features as new
        for client_feature in unmatched_client:
            diff = FeatureDiff(
                match=FeatureMatch(
                    client_feature=client_feature,
                    host_feature=None,
                    match_type=None,
                    confidence=0.0
                ),
                diff_type=DiffType.NEW,
                attribute_diffs=[],
                geometry_diff=None
            )
            feature_diffs.append(diff)
        
        # Add unmatched host features as deleted
        for host_feature in unmatched_host:
            diff = FeatureDiff(
                match=FeatureMatch(
                    client_feature=None,
                    host_feature=host_feature,
                    match_type=None,
                    confidence=0.0
                ),
                diff_type=DiffType.DELETED,
                attribute_diffs=[],
                geometry_diff=None
            )
            feature_diffs.append(diff)
        
        total = len(matches) + len(unmatched_client) + len(unmatched_host)
        
        return DiffSummary(
            total_features=total,
            unchanged=unchanged,
            attribute_only=attribute_only,
            geometry_only=geometry_only,
            both_changed=both_changed,
            new_features=len(unmatched_client),
            deleted_features=len(unmatched_host),
            feature_diffs=feature_diffs
        )
    
    def _compute_feature_diff(self, match: FeatureMatch) -> FeatureDiff:
        """Compute diff for a single matched feature pair."""
        client = match.client_feature
        host = match.host_feature
        
        # Compare attributes
        attr_diffs = self._compare_attributes(client, host)
        has_attr_changes = any(d.is_different for d in attr_diffs)
        
        # Compare geometry
        geom_diff = self._compare_geometry(client, host)
        has_geom_changes = geom_diff.is_different if geom_diff else False
        
        # Determine diff type
        if has_attr_changes and has_geom_changes:
            diff_type = DiffType.BOTH
        elif has_attr_changes:
            diff_type = DiffType.ATTRIBUTE_ONLY
        elif has_geom_changes:
            diff_type = DiffType.GEOMETRY_ONLY
        else:
            diff_type = DiffType.NO_CHANGE
        
        return FeatureDiff(
            match=match,
            diff_type=diff_type,
            attribute_diffs=attr_diffs,
            geometry_diff=geom_diff
        )
    
    def _compare_attributes(
        self,
        client: NormalizedFeature,
        host: NormalizedFeature
    ) -> List[AttributeDiff]:
        """Compare attributes between two features."""
        diffs = []
        
        # Get all field names
        all_fields = set(client.attributes.keys()) | set(host.attributes.keys())
        
        for field in all_fields:
            if field in self._ignore_fields:
                continue
            
            client_val = client.attributes.get(field)
            host_val = host.attributes.get(field)
            
            diffs.append(AttributeDiff(
                field_name=field,
                client_value=client_val,
                host_value=host_val
            ))
        
        return diffs
    
    def _compare_geometry(
        self,
        client: NormalizedFeature,
        host: NormalizedFeature
    ) -> Optional[GeometryDiff]:
        """Compare geometries between two features."""
        client_geom = client.geometry
        host_geom = host.geometry
        
        if client_geom.isEmpty() and host_geom.isEmpty():
            return GeometryDiff(
                client_geometry=client_geom,
                host_geometry=host_geom,
                distance=0.0,
                area_diff=0.0,
                is_different=False
            )
        
        if client_geom.isEmpty() or host_geom.isEmpty():
            return GeometryDiff(
                client_geometry=client_geom,
                host_geometry=host_geom,
                distance=float('inf'),
                area_diff=float('inf'),
                is_different=True
            )
        
        # Calculate distance between centroids
        client_centroid = client_geom.centroid().asPoint()
        host_centroid = host_geom.centroid().asPoint()
        distance = client_centroid.distance(host_centroid)
        
        # Calculate area difference (for polygons)
        client_area = client_geom.area()
        host_area = host_geom.area()
        area_diff = abs(client_area - host_area)
        
        # Check if geometries are equal within tolerance
        is_different = not client_geom.equals(host_geom)
        
        # Also check using distance if equals fails
        if is_different and distance < self._geometry_tolerance:
            # Additional check using buffer comparison
            is_different = not client_geom.buffer(
                self._geometry_tolerance, 5
            ).contains(host_geom)
        
        return GeometryDiff(
            client_geometry=client_geom,
            host_geometry=host_geom,
            distance=distance,
            area_diff=area_diff,
            is_different=is_different
        )
    
    def get_changed_features(
        self,
        diff_summary: DiffSummary
    ) -> List[FeatureDiff]:
        """Get only features with changes."""
        return [d for d in diff_summary.feature_diffs if d.has_changes]
    
    def get_features_by_type(
        self,
        diff_summary: DiffSummary,
        diff_type: DiffType
    ) -> List[FeatureDiff]:
        """Get features filtered by diff type."""
        return [d for d in diff_summary.feature_diffs if d.diff_type == diff_type]
