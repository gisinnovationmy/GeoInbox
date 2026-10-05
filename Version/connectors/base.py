"""
GeoInbox - Base Connector

Abstract base class for host data connectors.
Defines the interface for GeoPackage and PostGIS connectors.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any, Iterator
from dataclasses import dataclass

from qgis.core import QgsFeature, QgsGeometry, QgsCoordinateReferenceSystem, QgsFields


@dataclass
class LayerInfo:
    """Information about a layer in the host data source."""
    name: str
    geometry_type: str
    feature_count: int
    crs: QgsCoordinateReferenceSystem
    fields: QgsFields
    extent: Optional[tuple] = None  # (xmin, ymin, xmax, ymax)


class HostConnector(ABC):
    """
    Abstract base class for host data connectors.
    
    Provides a unified interface for accessing and modifying
    GeoPackage and PostGIS data sources with transaction support.
    """
    
    def __init__(self):
        """Initialize the connector."""
        self._connected = False
        self._in_transaction = False
    
    @property
    def is_connected(self) -> bool:
        """Check if connector is connected."""
        return self._connected
    
    @property
    def in_transaction(self) -> bool:
        """Check if a transaction is active."""
        return self._in_transaction
    
    @abstractmethod
    def connect(self, connection_string: str, **kwargs) -> None:
        """
        Connect to the data source.
        
        Args:
            connection_string: Connection string or file path
            **kwargs: Additional connection parameters
            
        Raises:
            ConnectorError: If connection fails
        """
        pass
    
    @abstractmethod
    def disconnect(self) -> None:
        """
        Disconnect from the data source.
        
        Should rollback any active transaction.
        """
        pass
    
    @abstractmethod
    def list_layers(self) -> List[LayerInfo]:
        """
        List available layers in the data source.
        
        Returns:
            List of layer information
        """
        pass
    
    @abstractmethod
    def get_layer(self, layer_name: str) -> LayerInfo:
        """
        Get information about a specific layer.
        
        Args:
            layer_name: Name of the layer
            
        Returns:
            Layer information
            
        Raises:
            ConnectorError: If layer not found
        """
        pass
    
    @abstractmethod
    def get_features(
        self,
        layer_name: str,
        feature_ids: Optional[List[Any]] = None,
        bbox: Optional[tuple] = None,
        limit: Optional[int] = None
    ) -> Iterator[QgsFeature]:
        """
        Get features from a layer.
        
        Args:
            layer_name: Name of the layer
            feature_ids: Optional list of feature IDs to fetch
            bbox: Optional bounding box filter (xmin, ymin, xmax, ymax)
            limit: Optional maximum number of features
            
        Yields:
            QgsFeature objects
        """
        pass
    
    @abstractmethod
    def get_feature(self, layer_name: str, feature_id: Any) -> Optional[QgsFeature]:
        """
        Get a single feature by ID.
        
        Args:
            layer_name: Name of the layer
            feature_id: Feature ID
            
        Returns:
            QgsFeature or None if not found
        """
        pass
    
    @abstractmethod
    def begin_transaction(self) -> None:
        """
        Begin a transaction.
        
        Raises:
            ConnectorError: If transaction cannot be started
        """
        pass
    
    @abstractmethod
    def commit_transaction(self) -> None:
        """
        Commit the current transaction.
        
        Raises:
            ConnectorError: If commit fails
        """
        pass
    
    @abstractmethod
    def rollback_transaction(self) -> None:
        """
        Rollback the current transaction.
        
        Should not raise exceptions.
        """
        pass
    
    @abstractmethod
    def insert_features(
        self,
        layer_name: str,
        features: List[QgsFeature]
    ) -> List[Any]:
        """
        Insert new features into a layer.
        
        Args:
            layer_name: Name of the layer
            features: Features to insert
            
        Returns:
            List of new feature IDs
            
        Raises:
            ConnectorError: If insert fails
        """
        pass
    
    @abstractmethod
    def update_features(
        self,
        layer_name: str,
        updates: List[Dict[str, Any]]
    ) -> int:
        """
        Update existing features.
        
        Args:
            layer_name: Name of the layer
            updates: List of update dicts with 'id', 'geometry', 'attributes'
            
        Returns:
            Number of features updated
            
        Raises:
            ConnectorError: If update fails
        """
        pass
    
    @abstractmethod
    def delete_features(
        self,
        layer_name: str,
        feature_ids: List[Any]
    ) -> int:
        """
        Delete features from a layer.
        
        Args:
            layer_name: Name of the layer
            feature_ids: IDs of features to delete
            
        Returns:
            Number of features deleted
            
        Raises:
            ConnectorError: If delete fails
        """
        pass
    
    def get_connector_type(self) -> str:
        """
        Get the connector type name.
        
        Returns:
            Connector type (e.g., 'geopackage', 'postgis')
        """
        return 'unknown'
    
    def supports_transactions(self) -> bool:
        """
        Check if this connector supports transactions.
        
        Returns:
            True if transactions are supported
        """
        return True
    
    def get_primary_key_field(self, layer_name: str) -> Optional[str]:
        """
        Get the primary key field name for a layer.
        
        Args:
            layer_name: Name of the layer
            
        Returns:
            Field name or None if not available
        """
        return None
