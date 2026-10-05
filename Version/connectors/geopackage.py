"""
GeoInbox - GeoPackage Connector

Connector for GeoPackage data sources using QGIS APIs.
"""

import sqlite3
from pathlib import Path
from typing import List, Optional, Dict, Any, Iterator

from qgis.core import (
    QgsVectorLayer, QgsFeature, QgsGeometry, QgsFields, QgsField,
    QgsCoordinateReferenceSystem, QgsFeatureRequest, QgsProject,
    QgsVectorFileWriter, QgsWkbTypes
)
from qgis.PyQt.QtCore import QVariant

from .base import HostConnector, LayerInfo
from ..utils.errors import ConnectorError, ConnectorErrorCode
from ..utils.security import quote_sqlite_identifier, log_debug_exception


class GeoPackageConnector(HostConnector):
    """
    Connector for GeoPackage (.gpkg) data sources.
    
    Uses QGIS vector layer APIs for data access and SQLite
    for transaction support.
    """
    
    def __init__(self):
        """Initialize the GeoPackage connector."""
        super().__init__()
        self._path: Optional[Path] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._layers: Dict[str, QgsVectorLayer] = {}
    
    def get_connector_type(self) -> str:
        """Get the connector type."""
        return 'geopackage'
    
    def connect(self, connection_string: str, **kwargs) -> None:
        """
        Connect to a GeoPackage file.
        
        Args:
            connection_string: Path to the .gpkg file
            **kwargs: Additional options
        """
        self._path = Path(connection_string)
        
        if not self._path.exists():
            raise ConnectorError(
                ConnectorErrorCode.CONNECTION_FAILED,
                f"GeoPackage file not found: {self._path}"
            )
        
        if not self._path.suffix.lower() == '.gpkg':
            raise ConnectorError(
                ConnectorErrorCode.CONNECTION_FAILED,
                "File is not a GeoPackage (.gpkg)"
            )
        
        try:
            # Open SQLite connection for transactions
            self._conn = sqlite3.connect(str(self._path))
            self._conn.row_factory = sqlite3.Row
            
            # Verify it's a valid GeoPackage
            cursor = self._conn.cursor()
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='gpkg_contents'
            """)
            if not cursor.fetchone():
                raise ConnectorError(
                    ConnectorErrorCode.CONNECTION_FAILED,
                    "Not a valid GeoPackage (missing gpkg_contents)"
                )
            
            self._connected = True
            
        except sqlite3.Error as e:
            raise ConnectorError(
                ConnectorErrorCode.CONNECTION_FAILED,
                "Failed to open GeoPackage",
                str(e)
            )
    
    def disconnect(self) -> None:
        """Disconnect from the GeoPackage."""
        if self._in_transaction:
            self.rollback_transaction()
        
        # Close all layers
        for layer in self._layers.values():
            del layer
        self._layers.clear()
        
        # Close SQLite connection
        if self._conn:
            self._conn.close()
            self._conn = None
        
        self._connected = False
        self._path = None
    
    def _ensure_connected(self) -> None:
        """Ensure we are connected."""
        if not self._connected or not self._conn:
            raise ConnectorError(
                ConnectorErrorCode.CONNECTION_FAILED,
                "Not connected to GeoPackage"
            )
    
    def _get_layer(self, layer_name: str) -> QgsVectorLayer:
        """Get or create a QGIS vector layer for the given table."""
        if layer_name in self._layers:
            return self._layers[layer_name]
        
        uri = f"{self._path}|layername={layer_name}"
        layer = QgsVectorLayer(uri, layer_name, "ogr")
        
        if not layer.isValid():
            raise ConnectorError(
                ConnectorErrorCode.LAYER_NOT_FOUND,
                f"Layer not found or invalid: {layer_name}"
            )
        
        self._layers[layer_name] = layer
        return layer
    
    def list_layers(self) -> List[LayerInfo]:
        """List available layers in the GeoPackage."""
        self._ensure_connected()
        
        try:
            cursor = self._conn.cursor()
            cursor.execute("""
                SELECT table_name, data_type, identifier, srs_id
                FROM gpkg_contents
                WHERE data_type IN ('features', 'tiles')
            """)
            
            layers = []
            for row in cursor.fetchall():
                table_name = row['table_name']
                
                # Get feature count
                count_sql = (
                    f"SELECT COUNT(*) FROM {quote_sqlite_identifier(table_name)}"  # nosec B608
                )
                cursor.execute(count_sql)
                count = cursor.fetchone()[0]
                
                # Get geometry type from gpkg_geometry_columns
                geom_type = 'Unknown'
                cursor.execute("""
                    SELECT geometry_type_name FROM gpkg_geometry_columns
                    WHERE table_name = ?
                """, (table_name,))
                geom_row = cursor.fetchone()
                if geom_row:
                    geom_type = geom_row[0]
                
                # Get CRS
                srs_id = row['srs_id']
                crs = QgsCoordinateReferenceSystem()
                if srs_id:
                    cursor.execute("""
                        SELECT definition FROM gpkg_spatial_ref_sys
                        WHERE srs_id = ?
                    """, (srs_id,))
                    srs_row = cursor.fetchone()
                    if srs_row and srs_row[0]:
                        crs.createFromWkt(srs_row[0])
                
                # Get fields using QGIS layer
                layer = self._get_layer(table_name)
                fields = layer.fields()
                
                layers.append(LayerInfo(
                    name=table_name,
                    geometry_type=geom_type,
                    feature_count=count,
                    crs=crs,
                    fields=fields
                ))
            
            return layers
            
        except sqlite3.Error as e:
            raise ConnectorError(
                ConnectorErrorCode.LAYER_NOT_FOUND,
                "Failed to list layers",
                str(e)
            )
    
    def get_layer(self, layer_name: str) -> LayerInfo:
        """Get information about a specific layer."""
        self._ensure_connected()
        
        layers = self.list_layers()
        for layer in layers:
            if layer.name == layer_name:
                return layer
        
        raise ConnectorError(
            ConnectorErrorCode.LAYER_NOT_FOUND,
            f"Layer not found: {layer_name}"
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
        
        try:
            self._conn.execute("BEGIN TRANSACTION")
            self._in_transaction = True
        except sqlite3.Error as e:
            raise ConnectorError(
                ConnectorErrorCode.TRANSACTION_FAILED,
                "Failed to begin transaction",
                str(e)
            )
    
    def commit_transaction(self) -> None:
        """Commit the current transaction."""
        self._ensure_connected()
        
        if not self._in_transaction:
            raise ConnectorError(
                ConnectorErrorCode.COMMIT_FAILED,
                "No active transaction"
            )
        
        try:
            self._conn.commit()
            self._in_transaction = False
            
            # Refresh layers to see changes
            for layer in self._layers.values():
                layer.reload()
                
        except sqlite3.Error as e:
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
            self._conn.rollback()
        except Exception as exc:
            log_debug_exception("GeoPackage rollback failed", exc)
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
        """
        Update existing features.
        
        Args:
            layer_name: Name of the layer
            updates: List of dicts with 'id', 'geometry' (optional), 'attributes' (optional)
        """
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
                
                # Update geometry if provided
                if 'geometry' in update and update['geometry']:
                    geom = update['geometry']
                    if isinstance(geom, QgsGeometry):
                        layer.changeGeometry(fid, geom)
                    elif isinstance(geom, str):
                        layer.changeGeometry(fid, QgsGeometry.fromWkt(geom))
                
                # Update attributes if provided
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
        
        try:
            cursor = self._conn.cursor()
            cursor.execute(f'PRAGMA table_info("{layer_name}")')
            
            for row in cursor.fetchall():
                if row[5] == 1:  # pk column
                    return row[1]  # name column
            
            return 'fid'  # Default GeoPackage primary key
            
        except sqlite3.Error:
            return None
    
    def export_features_to_geojson(
        self,
        layer_name: str,
        output_path: str,
        feature_ids: Optional[List[Any]] = None
    ) -> str:
        """
        Export features to GeoJSON for backup.
        
        Args:
            layer_name: Name of the layer
            output_path: Path for output GeoJSON file
            feature_ids: Optional list of feature IDs to export
            
        Returns:
            Path to exported file
        """
        self._ensure_connected()
        
        layer = self._get_layer(layer_name)
        
        # Create a memory layer with selected features
        if feature_ids:
            request = QgsFeatureRequest().setFilterFids(feature_ids)
        else:
            request = QgsFeatureRequest()
        
        # Export using QgsVectorFileWriter
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GeoJSON"
        options.fileEncoding = "UTF-8"
        
        if feature_ids:
            options.filterExtent = None
            # Create temp layer with filtered features
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
