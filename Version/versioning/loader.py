"""
GeoInbox - Data Loader

Load and normalize GIS data from various formats for versioning.
"""

from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass, field

from qgis.core import (
    QgsVectorLayer, QgsFeature, QgsGeometry, QgsFields, QgsField,
    QgsCoordinateReferenceSystem, QgsRectangle, QgsProject,
    QgsCoordinateTransform, QgsCoordinateTransformContext
)


@dataclass
class NormalizedFeature:
    """Normalized representation of a GIS feature."""
    feature_id: str
    geometry: QgsGeometry
    attributes: Dict[str, Any]
    crs: QgsCoordinateReferenceSystem
    bbox: QgsRectangle
    source_layer_name: str
    source_path: str
    original_fid: Optional[int] = None


@dataclass
class LoadedDataset:
    """A loaded and normalized dataset."""
    name: str
    source_path: str
    source_format: str
    crs: QgsCoordinateReferenceSystem
    fields: QgsFields
    features: List[NormalizedFeature] = field(default_factory=list)
    geometry_type: str = ''
    
    @property
    def feature_count(self) -> int:
        return len(self.features)
    
    @property
    def bbox(self) -> Optional[QgsRectangle]:
        if not self.features:
            return None
        
        combined = QgsRectangle()
        for f in self.features:
            combined.combineExtentWith(f.bbox)
        return combined


class DataLoader:
    """
    Load GIS data from various formats and normalize for versioning.
    
    Supported formats:
    - GeoJSON
    - GeoPackage
    - KML
    - Shapefile
    """
    
    SUPPORTED_EXTENSIONS = {'.geojson', '.json', '.gpkg', '.kml', '.shp'}
    
    def __init__(self):
        """Initialize the data loader."""
        self._loaded_datasets: Dict[str, LoadedDataset] = {}
    
    def load(
        self,
        source_path: str,
        layer_name: Optional[str] = None,
        crs_override: Optional[QgsCoordinateReferenceSystem] = None
    ) -> LoadedDataset:
        """
        Load a dataset from file.
        
        Args:
            source_path: Path to the source file
            layer_name: Optional layer name (for multi-layer formats like GPKG)
            crs_override: Optional CRS to use if source has none
            
        Returns:
            LoadedDataset with normalized features
        """
        path = Path(source_path)
        
        if not path.exists():
            raise ValueError(f"File not found: {source_path}")
        
        ext = path.suffix.lower()
        
        if ext not in self.SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported format: {ext}")
        
        # Load based on format
        if ext in {'.geojson', '.json'}:
            return self._load_geojson(path, crs_override)
        elif ext == '.gpkg':
            return self._load_geopackage(path, layer_name, crs_override)
        elif ext == '.kml':
            return self._load_kml(path, crs_override)
        elif ext == '.shp':
            return self._load_shapefile(path, crs_override)
        else:
            raise ValueError(f"Unsupported format: {ext}")
    
    def _load_geojson(
        self,
        path: Path,
        crs_override: Optional[QgsCoordinateReferenceSystem] = None
    ) -> LoadedDataset:
        """Load a GeoJSON file."""
        layer = QgsVectorLayer(str(path), path.stem, "ogr")
        
        if not layer.isValid():
            raise ValueError(f"Failed to load GeoJSON: {path}")
        
        return self._normalize_layer(layer, str(path), 'geojson', crs_override)
    
    def _load_geopackage(
        self,
        path: Path,
        layer_name: Optional[str] = None,
        crs_override: Optional[QgsCoordinateReferenceSystem] = None
    ) -> LoadedDataset:
        """Load a GeoPackage file."""
        if layer_name:
            uri = f"{path}|layername={layer_name}"
        else:
            # Load first layer
            uri = str(path)
        
        layer = QgsVectorLayer(uri, layer_name or path.stem, "ogr")
        
        if not layer.isValid():
            raise ValueError(f"Failed to load GeoPackage: {path}")
        
        return self._normalize_layer(layer, str(path), 'geopackage', crs_override)
    
    def _load_kml(
        self,
        path: Path,
        crs_override: Optional[QgsCoordinateReferenceSystem] = None
    ) -> LoadedDataset:
        """Load a KML file."""
        layer = QgsVectorLayer(str(path), path.stem, "ogr")
        
        if not layer.isValid():
            raise ValueError(f"Failed to load KML: {path}")
        
        return self._normalize_layer(layer, str(path), 'kml', crs_override)
    
    def _load_shapefile(
        self,
        path: Path,
        crs_override: Optional[QgsCoordinateReferenceSystem] = None
    ) -> LoadedDataset:
        """Load a shapefile."""
        layer = QgsVectorLayer(str(path), path.stem, "ogr")
        
        if not layer.isValid():
            raise ValueError(f"Failed to load shapefile: {path}")
        
        # Check if CRS is defined
        if not layer.crs().isValid() and not crs_override:
            raise ValueError(
                f"Shapefile has no CRS defined. Please provide crs_override."
            )
        
        return self._normalize_layer(layer, str(path), 'shapefile', crs_override)
    
    def _normalize_layer(
        self,
        layer: QgsVectorLayer,
        source_path: str,
        source_format: str,
        crs_override: Optional[QgsCoordinateReferenceSystem] = None
    ) -> LoadedDataset:
        """Normalize a QGIS vector layer to our internal format."""
        crs = crs_override if crs_override and crs_override.isValid() else layer.crs()
        
        from qgis.core import QgsWkbTypes
        geom_type = QgsWkbTypes.displayString(layer.wkbType())
        
        dataset = LoadedDataset(
            name=layer.name(),
            source_path=source_path,
            source_format=source_format,
            crs=crs,
            fields=layer.fields(),
            geometry_type=geom_type
        )
        
        # Normalize features
        for feature in layer.getFeatures():
            geom = feature.geometry()
            
            # Get bounding box
            bbox = geom.boundingBox() if geom and not geom.isEmpty() else QgsRectangle()
            
            # Get attributes as dict
            attrs = {}
            for field in layer.fields():
                attrs[field.name()] = feature[field.name()]
            
            # Generate feature ID
            fid = feature.id()
            feature_id = str(fid)
            
            # Check for common ID fields
            for id_field in ['id', 'fid', 'gid', 'objectid', 'ogc_fid']:
                if id_field in attrs and attrs[id_field] is not None:
                    feature_id = str(attrs[id_field])
                    break
            
            normalized = NormalizedFeature(
                feature_id=feature_id,
                geometry=QgsGeometry(geom),
                attributes=attrs,
                crs=crs,
                bbox=bbox,
                source_layer_name=layer.name(),
                source_path=source_path,
                original_fid=fid
            )
            
            dataset.features.append(normalized)
        
        return dataset
    
    def reproject_dataset(
        self,
        dataset: LoadedDataset,
        target_crs: QgsCoordinateReferenceSystem
    ) -> LoadedDataset:
        """
        Reproject a dataset to a target CRS.
        
        Args:
            dataset: Source dataset
            target_crs: Target CRS
            
        Returns:
            New dataset with reprojected geometries
        """
        if dataset.crs == target_crs:
            return dataset
        
        transform = QgsCoordinateTransform(
            dataset.crs,
            target_crs,
            QgsProject.instance()
        )
        
        reprojected = LoadedDataset(
            name=dataset.name,
            source_path=dataset.source_path,
            source_format=dataset.source_format,
            crs=target_crs,
            fields=dataset.fields,
            geometry_type=dataset.geometry_type
        )
        
        for feature in dataset.features:
            geom = QgsGeometry(feature.geometry)
            geom.transform(transform)
            
            reprojected.features.append(NormalizedFeature(
                feature_id=feature.feature_id,
                geometry=geom,
                attributes=feature.attributes.copy(),
                crs=target_crs,
                bbox=geom.boundingBox(),
                source_layer_name=feature.source_layer_name,
                source_path=feature.source_path,
                original_fid=feature.original_fid
            ))
        
        return reprojected
    
    def get_field_names(self, dataset: LoadedDataset) -> List[str]:
        """Get list of field names in a dataset."""
        return [field.name() for field in dataset.fields]
    
    def get_common_fields(
        self,
        dataset1: LoadedDataset,
        dataset2: LoadedDataset
    ) -> List[str]:
        """Get field names common to both datasets."""
        fields1 = set(self.get_field_names(dataset1))
        fields2 = set(self.get_field_names(dataset2))
        return list(fields1 & fields2)
