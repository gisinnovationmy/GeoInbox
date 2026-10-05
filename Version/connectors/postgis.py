"""
GeoInbox - PostGIS Connector

Connector for PostGIS data sources using QGIS APIs.
"""

from typing import List, Optional, Dict, Any, Iterator

from qgis.core import (
    QgsVectorLayer, QgsFeature, QgsGeometry, QgsFields,
    QgsCoordinateReferenceSystem, QgsFeatureRequest, QgsProject,
    QgsDataSourceUri, QgsVectorFileWriter
)

from .base import HostConnector, LayerInfo
from ..utils.errors import ConnectorError, ConnectorErrorCode
from ..utils.security import log_debug_exception


class PostGISConnector(HostConnector):
    """
    Connector for PostGIS data sources.
    
    Uses QGIS vector layer APIs for data access with PostgreSQL
    transaction support.
    """
    
    def __init__(self):
        """Initialize the PostGIS connector."""
        super().__init__()
        self._uri: Optional[QgsDataSourceUri] = None
        self._layers: Dict[str, QgsVectorLayer] = {}
        self._conn_info: Dict[str, str] = {}
    
    def get_connector_type(self) -> str:
        """Get the connector type."""
        return 'postgis'
    
    def connect(self, connection_string: str, **kwargs) -> None:
        """
        Connect to a PostGIS database.
        
        Args:
            connection_string: PostgreSQL connection string or QGIS connection name
            **kwargs: Additional options (host, port, database, username, password, schema)
        """
        self._uri = QgsDataSourceUri()
        
        # Check if it's a QGIS saved connection name
        if not any(x in connection_string for x in ['host=', 'dbname=', '://']):
            # Assume it's a saved connection name
            from qgis.core import QgsProviderRegistry
            provider_metadata = QgsProviderRegistry.instance().providerMetadata('postgres')
            if provider_metadata:
                connections = provider_metadata.connections()
                if connection_string in connections:
                    conn = connections[connection_string]
                    self._uri = QgsDataSourceUri(conn.uri())
                else:
                    raise ConnectorError(
                        ConnectorErrorCode.CONNECTION_FAILED,
                        f"PostgreSQL connection not found: {connection_string}"
                    )
        else:
            # Parse connection string
            self._uri.setConnection(
                kwargs.get('host', 'localhost'),
                str(kwargs.get('port', 5432)),
                kwargs.get('database', ''),
                kwargs.get('username', ''),
                kwargs.get('password', '')
            )
        
        # Store connection info
        self._conn_info = {
            'host': self._uri.host(),
            'port': self._uri.port(),
            'database': self._uri.database(),
            'username': self._uri.username(),
            'schema': kwargs.get('schema', 'public')
        }
        
        # Test connection by listing tables
        try:
            self._list_tables()
            self._connected = True
        except Exception as e:
            raise ConnectorError(
                ConnectorErrorCode.CONNECTION_FAILED,
                "Failed to connect to PostGIS database",
                str(e)
            )
    
    def disconnect(self) -> None:
        """Disconnect from the PostGIS database."""
        if self._in_transaction:
            self.rollback_transaction()
        
        # Close all layers
        for layer in self._layers.values():
            del layer
        self._layers.clear()
        
        self._connected = False
        self._uri = None
        self._conn_info = {}
    
    def _ensure_connected(self) -> None:
        """Ensure we are connected."""
        if not self._connected or not self._uri:
            raise ConnectorError(
                ConnectorErrorCode.CONNECTION_FAILED,
                "Not connected to PostGIS database"
            )
    
    def _list_tables(self) -> List[str]:
        """List available tables in the schema."""
        schema = self._conn_info.get('schema', 'public')
        
        # Create a temporary layer to query geometry_columns
        uri = QgsDataSourceUri(self._uri)
        uri.setDataSource(schema, 'geometry_columns', None)
        
        # Use QGIS provider to list tables
        from qgis.core import QgsProviderRegistry
        provider = QgsProviderRegistry.instance().providerMetadata('postgres')
        
        if not provider:
            return []
        
        # Get tables with geometry
        tables = []
        try:
            conn = provider.createConnection(self._uri.uri(), {})
            if conn:
                for table_info in conn.tables(schema):
                    if table_info.geometryColumn():
                        tables.append(table_info.tableName())
        except Exception as exc:
            log_debug_exception("PostGIS table listing failed", exc)
        
        return tables
    
    def _get_layer(self, layer_name: str) -> QgsVectorLayer:
        """Get or create a QGIS vector layer for the given table."""
        if layer_name in self._layers:
            return self._layers[layer_name]
        
        schema = self._conn_info.get('schema', 'public')
        
        uri = QgsDataSourceUri(self._uri)
        uri.setDataSource(schema, layer_name, 'geom')  # Assume 'geom' column
        
        layer = QgsVectorLayer(uri.uri(), layer_name, "postgres")
        
        if not layer.isValid():
            # Try with 'geometry' column
            uri.setDataSource(schema, layer_name, 'geometry')
            layer = QgsVectorLayer(uri.uri(), layer_name, "postgres")
        
        if not layer.isValid():
            raise ConnectorError(
                ConnectorErrorCode.LAYER_NOT_FOUND,
                f"Layer not found or invalid: {layer_name}"
            )
        
        self._layers[layer_name] = layer
        return layer
    
    def list_layers(self) -> List[LayerInfo]:
        """List available layers in the PostGIS database."""
        self._ensure_connected()
        
        tables = self._list_tables()
        layers = []
        
        for table_name in tables:
            try:
                layer = self._get_layer(table_name)
                
                layers.append(LayerInfo(
                    name=table_name,
                    geometry_type=QgsWkbTypes.displayString(layer.wkbType()) if hasattr(layer, 'wkbType') else 'Unknown',
                    feature_count=layer.featureCount(),
                    crs=layer.crs(),
                    fields=layer.fields()
                ))
            except Exception as exc:
                log_debug_exception(f"PostGIS layer metadata failed for {table_name}", exc)
                continue
        
        return layers
    
    def get_layer(self, layer_name: str) -> LayerInfo:
        """Get information about a specific layer."""
        self._ensure_connected()
        
        layer = self._get_layer(layer_name)
        
        from qgis.core import QgsWkbTypes
        
        return LayerInfo(
            name=layer_name,
            geometry_type=QgsWkbTypes.displayString(layer.wkbType()),
            feature_count=layer.featureCount(),
            crs=layer.crs(),
            fields=layer.fields()
        )
    
    def get_features(
        self,
        layer_name: str,
        feature_ids: Optional[List[Any]] = None,
        bbox: Optional[tuple] = None,
        limit: Optional[int] = None
    ) -> Iterator[QgsFeature]:
        """Get features from a layer."""
        self._ensure_connected()
        
        layer = self._get_layer(layer_name)
        
        request = QgsFeatureRequest()
        
        if feature_ids:
            request.setFilterFids(feature_ids)
        
        if bbox:
            from qgis.core import QgsRectangle
            rect = QgsRectangle(bbox[0], bbox[1], bbox[2], bbox[3])
            request.setFilterRect(rect)
        
        if limit:
            request.setLimit(limit)
        
        for feature in layer.getFeatures(request):
            yield feature
    
    def get_feature(self, layer_name: str, feature_id: Any) -> Optional[QgsFeature]:
        """Get a single feature by ID."""
        self._ensure_connected()
        
        layer = self._get_layer(layer_name)
        request = QgsFeatureRequest().setFilterFid(feature_id)
        
        for feature in layer.getFeatures(request):
            return feature
        
        return None
    
    def begin_transaction(self) -> None:
        """Begin a transaction."""
        self._ensure_connected()
        
        if self._in_transaction:
            raise ConnectorError(
                ConnectorErrorCode.TRANSACTION_FAILED,
                "Transaction already active"
            )
        
        # Start editing on all loaded layers
        for layer in self._layers.values():
            if not layer.isEditable():
                layer.startEditing()
        
        self._in_transaction = True
    
    def commit_transaction(self) -> None:
        """Commit the current transaction."""
        self._ensure_connected()
        
        if not self._in_transaction:
            raise ConnectorError(
                ConnectorErrorCode.COMMIT_FAILED,
                "No active transaction"
            )
        
        try:
            # Commit all layers
            for layer in self._layers.values():
                if layer.isEditable():
                    if not layer.commitChanges():
                        errors = layer.commitErrors()
                        raise ConnectorError(
                            ConnectorErrorCode.COMMIT_FAILED,
                            "Failed to commit changes",
                            "; ".join(errors)
                        )
            
            self._in_transaction = False
            
        except ConnectorError:
            raise
        except Exception as e:
            raise ConnectorError(
                ConnectorErrorCode.COMMIT_FAILED,
                "Failed to commit transaction",
                str(e)
            )
    
    def rollback_transaction(self) -> None:
        """Rollback the current transaction."""
        if not self._in_transaction:
            return
        
        try:
            for layer in self._layers.values():
                if layer.isEditable():
                    layer.rollBack()
        except Exception as exc:
            log_debug_exception("PostGIS rollback failed", exc)
        finally:
            self._in_transaction = False
    
    def insert_features(
        self,
        layer_name: str,
        features: List[QgsFeature]
    ) -> List[Any]:
        """Insert new features into a layer."""
        self._ensure_connected()
        
        layer = self._get_layer(layer_name)
        
        if not layer.isEditable():
            layer.startEditing()
        
        new_ids = []
        
        try:
            for feature in features:
                success, new_features = layer.dataProvider().addFeatures([feature])
                if success and new_features:
                    new_ids.append(new_features[0].id())
                else:
                    raise ConnectorError(
                        ConnectorErrorCode.TRANSACTION_FAILED,
                        "Failed to insert feature"
                    )
            
            if not self._in_transaction:
                layer.commitChanges()
            
            return new_ids
            
        except Exception as e:
            layer.rollBack()
            raise ConnectorError(
                ConnectorErrorCode.TRANSACTION_FAILED,
                "Failed to insert features",
                str(e)
            )
    
    def update_features(
        self,
        layer_name: str,
        updates: List[Dict[str, Any]]
    ) -> int:
        """Update existing features."""
        self._ensure_connected()
        
        layer = self._get_layer(layer_name)
        
        if not layer.isEditable():
            layer.startEditing()
        
        updated_count = 0
        
        try:
            for update in updates:
                fid = update.get('id')
                if fid is None:
                    continue
                
                if 'geometry' in update and update['geometry']:
                    geom = update['geometry']
                    if isinstance(geom, QgsGeometry):
                        layer.changeGeometry(fid, geom)
                    elif isinstance(geom, str):
                        layer.changeGeometry(fid, QgsGeometry.fromWkt(geom))
                
                if 'attributes' in update and update['attributes']:
                    attrs = update['attributes']
                    for field_name, value in attrs.items():
                        field_idx = layer.fields().indexFromName(field_name)
                        if field_idx >= 0:
                            layer.changeAttributeValue(fid, field_idx, value)
                
                updated_count += 1
            
            if not self._in_transaction:
                layer.commitChanges()
            
            return updated_count
            
        except Exception as e:
            layer.rollBack()
            raise ConnectorError(
                ConnectorErrorCode.TRANSACTION_FAILED,
                "Failed to update features",
                str(e)
            )
    
    def delete_features(
        self,
        layer_name: str,
        feature_ids: List[Any]
    ) -> int:
        """Delete features from a layer."""
        self._ensure_connected()
        
        layer = self._get_layer(layer_name)
        
        if not layer.isEditable():
            layer.startEditing()
        
        try:
            success = layer.deleteFeatures(feature_ids)
            
            if not success:
                raise ConnectorError(
                    ConnectorErrorCode.TRANSACTION_FAILED,
                    "Failed to delete features"
                )
            
            if not self._in_transaction:
                layer.commitChanges()
            
            return len(feature_ids)
            
        except Exception as e:
            layer.rollBack()
            raise ConnectorError(
                ConnectorErrorCode.TRANSACTION_FAILED,
                "Failed to delete features",
                str(e)
            )
    
    def get_primary_key_field(self, layer_name: str) -> Optional[str]:
        """Get the primary key field name for a layer."""
        self._ensure_connected()
        
        layer = self._get_layer(layer_name)
        pk_attrs = layer.primaryKeyAttributes()
        
        if pk_attrs:
            return layer.fields().at(pk_attrs[0]).name()
        
        return None
    
    def export_features_to_geojson(
        self,
        layer_name: str,
        output_path: str,
        feature_ids: Optional[List[Any]] = None
    ) -> str:
        """Export features to GeoJSON for backup."""
        self._ensure_connected()
        
        layer = self._get_layer(layer_name)
        
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GeoJSON"
        options.fileEncoding = "UTF-8"
        
        if feature_ids:
            request = QgsFeatureRequest().setFilterFids(feature_ids)
            features = list(layer.getFeatures(request))
            
            from qgis.core import QgsMemoryProviderUtils
            mem_layer = QgsMemoryProviderUtils.createMemoryLayer(
                layer_name,
                layer.fields(),
                layer.wkbType(),
                layer.crs()
            )
            mem_layer.dataProvider().addFeatures(features)
            
            error = QgsVectorFileWriter.writeAsVectorFormatV3(
                mem_layer,
                output_path,
                QgsProject.instance().transformContext(),
                options
            )
        else:
            error = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer,
                output_path,
                QgsProject.instance().transformContext(),
                options
            )
        
        if error[0] != QgsVectorFileWriter.NoError:
            raise ConnectorError(
                ConnectorErrorCode.TRANSACTION_FAILED,
                f"Failed to export to GeoJSON: {error[1]}"
            )
        
        return output_path
