"""
GeoInbox - Matching Engine

Smart and manual matching between client and host features.
"""

from typing import List, Optional, Dict, Any, Tuple, Set
from dataclasses import dataclass
from enum import Enum

from qgis.core import QgsGeometry, QgsPointXY

from .loader import NormalizedFeature, LoadedDataset


class MatchType(Enum):
    """Types of feature matching."""
    UNIQUE_ID = "unique_id"
    ATTRIBUTE = "attribute"
    SPATIAL = "spatial"
    BBOX = "bbox"
    MANUAL = "manual"
    UNMATCHED = "unmatched"


@dataclass
class FeatureMatch:
    """A match between a client and host feature."""
    client_feature: NormalizedFeature
    host_feature: Optional[NormalizedFeature]
    match_type: MatchType
    confidence: float  # 0.0 to 1.0
    match_details: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.match_details is None:
            self.match_details = {}


@dataclass
class MatchResult:
    """Result of a matching operation."""
    matches: List[FeatureMatch]
    unmatched_client: List[NormalizedFeature]
    unmatched_host: List[NormalizedFeature]
    
    @property
    def matched_count(self) -> int:
        return len([m for m in self.matches if m.host_feature is not None])
    
    @property
    def total_client(self) -> int:
        return len(self.matches) + len(self.unmatched_client)
    
    @property
    def match_rate(self) -> float:
        total = self.total_client
        if total == 0:
            return 0.0
        return self.matched_count / total


class MatchingEngine:
    """
    Engine for matching features between client and host datasets.
    
    Matching strategies (in priority order):
    1. Unique ID field match
    2. Attribute match on key fields
    3. Spatial nearest match
    4. Bounding box overlap
    """
    
    def __init__(self):
        """Initialize the matching engine."""
        self._id_field: Optional[str] = None
        self._key_fields: List[str] = []
        self._spatial_tolerance: float = 10.0  # meters
        self._bbox_threshold: float = 0.75  # 75% overlap
        self._case_sensitive: bool = False
    
    def configure(
        self,
        id_field: Optional[str] = None,
        key_fields: Optional[List[str]] = None,
        spatial_tolerance: float = 10.0,
        bbox_threshold: float = 0.75,
        case_sensitive: bool = False
    ):
        """
        Configure matching parameters.
        
        Args:
            id_field: Field to use for unique ID matching
            key_fields: Fields to use for attribute matching
            spatial_tolerance: Distance tolerance for spatial matching (meters)
            bbox_threshold: Minimum overlap ratio for bbox matching (0-1)
            case_sensitive: Whether attribute matching is case-sensitive
        """
        self._id_field = id_field
        self._key_fields = key_fields or []
        self._spatial_tolerance = spatial_tolerance
        self._bbox_threshold = bbox_threshold
        self._case_sensitive = case_sensitive
    
    def smart_match(
        self,
        client: LoadedDataset,
        host: LoadedDataset
    ) -> MatchResult:
        """
        Perform smart matching using all available strategies.
        
        Args:
            client: Client dataset (incoming data)
            host: Host dataset (target to update)
            
        Returns:
            MatchResult with all matches and unmatched features
        """
        matches = []
        matched_host_ids: Set[str] = set()
        unmatched_client = []
        
        for client_feature in client.features:
            match = self._find_best_match(client_feature, host, matched_host_ids)
            
            if match:
                matches.append(match)
                if match.host_feature:
                    matched_host_ids.add(match.host_feature.feature_id)
            else:
                unmatched_client.append(client_feature)
        
        # Find unmatched host features
        unmatched_host = [
            f for f in host.features
            if f.feature_id not in matched_host_ids
        ]
        
        return MatchResult(
            matches=matches,
            unmatched_client=unmatched_client,
            unmatched_host=unmatched_host
        )
    
    def _find_best_match(
        self,
        client_feature: NormalizedFeature,
        host: LoadedDataset,
        already_matched: Set[str]
    ) -> Optional[FeatureMatch]:
        """Find the best match for a client feature."""
        available_hosts = [
            f for f in host.features
            if f.feature_id not in already_matched
        ]
        
        if not available_hosts:
            return FeatureMatch(
                client_feature=client_feature,
                host_feature=None,
                match_type=MatchType.UNMATCHED,
                confidence=0.0
            )
        
        # Try matching strategies in order
        
        # 1. Unique ID match
        if self._id_field:
            match = self._match_by_id(client_feature, available_hosts)
            if match:
                return match
        
        # 2. Attribute match
        if self._key_fields:
            match = self._match_by_attributes(client_feature, available_hosts)
            if match:
                return match
        
        # 3. Spatial match
        match = self._match_by_spatial(client_feature, available_hosts)
        if match:
            return match
        
        # 4. Bbox overlap match
        match = self._match_by_bbox(client_feature, available_hosts)
        if match:
            return match
        
        # No match found
        return FeatureMatch(
            client_feature=client_feature,
            host_feature=None,
            match_type=MatchType.UNMATCHED,
            confidence=0.0
        )
    
    def _match_by_id(
        self,
        client_feature: NormalizedFeature,
        host_features: List[NormalizedFeature]
    ) -> Optional[FeatureMatch]:
        """Match by unique ID field."""
        client_id = client_feature.attributes.get(self._id_field)
        if client_id is None:
            return None
        
        for host_feature in host_features:
            host_id = host_feature.attributes.get(self._id_field)
            if host_id is not None and self._values_equal(client_id, host_id):
                return FeatureMatch(
                    client_feature=client_feature,
                    host_feature=host_feature,
                    match_type=MatchType.UNIQUE_ID,
                    confidence=1.0,
                    match_details={'field': self._id_field, 'value': client_id}
                )
        
        return None
    
    def _match_by_attributes(
        self,
        client_feature: NormalizedFeature,
        host_features: List[NormalizedFeature]
    ) -> Optional[FeatureMatch]:
        """Match by key attribute fields."""
        best_match = None
        best_score = 0.0
        
        for host_feature in host_features:
            matching_fields = 0
            total_fields = len(self._key_fields)
            
            for field in self._key_fields:
                client_val = client_feature.attributes.get(field)
                host_val = host_feature.attributes.get(field)
                
                if client_val is not None and host_val is not None:
                    if self._values_equal(client_val, host_val):
                        matching_fields += 1
            
            if total_fields > 0:
                score = matching_fields / total_fields
                if score > best_score and score >= 0.5:  # At least 50% match
                    best_score = score
                    best_match = host_feature
        
        if best_match:
            return FeatureMatch(
                client_feature=client_feature,
                host_feature=best_match,
                match_type=MatchType.ATTRIBUTE,
                confidence=best_score,
                match_details={'fields': self._key_fields, 'score': best_score}
            )
        
        return None
    
    def _match_by_spatial(
        self,
        client_feature: NormalizedFeature,
        host_features: List[NormalizedFeature]
    ) -> Optional[FeatureMatch]:
        """Match by spatial proximity."""
        client_geom = client_feature.geometry
        if client_geom.isEmpty():
            return None
        
        client_centroid = client_geom.centroid().asPoint()
        
        best_match = None
        best_distance = float('inf')
        
        for host_feature in host_features:
            host_geom = host_feature.geometry
            if host_geom.isEmpty():
                continue
            
            host_centroid = host_geom.centroid().asPoint()
            distance = client_centroid.distance(host_centroid)
            
            if distance < best_distance and distance <= self._spatial_tolerance:
                best_distance = distance
                best_match = host_feature
        
        if best_match:
            confidence = 1.0 - (best_distance / self._spatial_tolerance)
            return FeatureMatch(
                client_feature=client_feature,
                host_feature=best_match,
                match_type=MatchType.SPATIAL,
                confidence=max(0.0, confidence),
                match_details={'distance': best_distance}
            )
        
        return None
    
    def _match_by_bbox(
        self,
        client_feature: NormalizedFeature,
        host_features: List[NormalizedFeature]
    ) -> Optional[FeatureMatch]:
        """Match by bounding box overlap."""
        client_bbox = client_feature.bbox
        if client_bbox.isEmpty():
            return None
        
        best_match = None
        best_overlap = 0.0
        
        for host_feature in host_features:
            host_bbox = host_feature.bbox
            if host_bbox.isEmpty():
                continue
            
            # Calculate intersection
            intersection = client_bbox.intersect(host_bbox)
            if intersection.isEmpty():
                continue
            
            # Calculate overlap ratio
            intersection_area = intersection.area()
            client_area = client_bbox.area()
            host_area = host_bbox.area()
            
            if client_area > 0 and host_area > 0:
                overlap = intersection_area / min(client_area, host_area)
                
                if overlap > best_overlap and overlap >= self._bbox_threshold:
                    best_overlap = overlap
                    best_match = host_feature
        
        if best_match:
            return FeatureMatch(
                client_feature=client_feature,
                host_feature=best_match,
                match_type=MatchType.BBOX,
                confidence=best_overlap,
                match_details={'overlap': best_overlap}
            )
        
        return None
    
    def _values_equal(self, val1: Any, val2: Any) -> bool:
        """Compare two values for equality."""
        if val1 is None or val2 is None:
            return val1 is val2
        
        # String comparison
        if isinstance(val1, str) and isinstance(val2, str):
            if self._case_sensitive:
                return val1 == val2
            return val1.lower() == val2.lower()
        
        # Numeric comparison
        try:
            return float(val1) == float(val2)
        except (ValueError, TypeError):
            pass
        
        return val1 == val2
    
    def manual_match(
        self,
        client_feature: NormalizedFeature,
        host_feature: NormalizedFeature
    ) -> FeatureMatch:
        """
        Create a manual match between two features.
        
        Args:
            client_feature: Client feature
            host_feature: Host feature to match to
            
        Returns:
            FeatureMatch with manual type
        """
        return FeatureMatch(
            client_feature=client_feature,
            host_feature=host_feature,
            match_type=MatchType.MANUAL,
            confidence=1.0,
            match_details={'manual': True}
        )
    
    def unmatch(self, match: FeatureMatch) -> FeatureMatch:
        """
        Remove a match, marking the client feature as unmatched.
        
        Args:
            match: Existing match to remove
            
        Returns:
            New FeatureMatch with no host feature
        """
        return FeatureMatch(
            client_feature=match.client_feature,
            host_feature=None,
            match_type=MatchType.UNMATCHED,
            confidence=0.0
        )
