"""QGIS map → GEOV package (uses geov_format.py spec from miniGISimple)."""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional, Sequence

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsGraduatedSymbolRenderer,
    QgsLayerTreeLayer,
    QgsProject,
    QgsRectangle,
    QgsRuleBasedRenderer,
    QgsSimpleFillSymbolLayer,
    QgsSimpleLineSymbolLayer,
    QgsSimpleMarkerSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsSymbol,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .geov_format import (
    DEFAULT_MINI_STYLE,
    DEFAULT_SYMBOLOGY,
    GEOV_REQUIRES_VECTOR_MESSAGE,
    build_geov_zip_bytes_from_layers,
    geov_download_file_name,
    normalize_geov_project_name,
    unique_layer_file_name,
)

WGS84 = "EPSG:4326"

__all__ = [
    "GEOV_REQUIRES_VECTOR_MESSAGE",
    "build_geov_zip_bytes",
    "geov_download_file_name",
    "normalize_geov_project_name",
]


def geometry_type_from_layer(layer: QgsVectorLayer) -> str:
    gt = QgsWkbTypes.geometryType(layer.wkbType())
    if gt == QgsWkbTypes.GeometryType.PointGeometry:
        return "point"
    if gt == QgsWkbTypes.GeometryType.LineGeometry:
        return "line"
    if gt == QgsWkbTypes.GeometryType.PolygonGeometry:
        return "polygon"
    return "mixed"


def _qcolor_to_hex(qcolor) -> str:
    return f"#{qcolor.red():02x}{qcolor.green():02x}{qcolor.blue():02x}"


def _qvariant_to_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return value
    return str(value)


def _symbol_to_mini_style(symbol: QgsSymbol) -> Dict[str, Any]:
    """Map QGIS symbol → MiniGISimple createDefaultStyle() shape."""
    style = dict(DEFAULT_MINI_STYLE)
    color = symbol.color()
    style["color"] = _qcolor_to_hex(color)
    style["opacity"] = float(color.alphaF()) if color.alphaF() > 0 else DEFAULT_MINI_STYLE["opacity"]

    sym_type = symbol.type()
    if sym_type == QgsSymbol.SymbolType.Marker:
        style["size"] = float(symbol.size())
        for i in range(symbol.symbolLayerCount()):
            sl = symbol.symbolLayer(i)
            if isinstance(sl, QgsSimpleMarkerSymbolLayer):
                style["color"] = _qcolor_to_hex(sl.color())
                style["strokeColor"] = _qcolor_to_hex(sl.strokeColor())
                style["size"] = float(sl.size())
                style["opacity"] = float(sl.color().alphaF())
                break
    elif sym_type == QgsSymbol.SymbolType.Line:
        style["strokeColor"] = _qcolor_to_hex(color)
        style["strokeWidth"] = float(symbol.width())
        for i in range(symbol.symbolLayerCount()):
            sl = symbol.symbolLayer(i)
            if isinstance(sl, QgsSimpleLineSymbolLayer):
                style["strokeColor"] = _qcolor_to_hex(sl.color())
                style["strokeWidth"] = float(sl.width())
                style["opacity"] = float(sl.color().alphaF())
                break
    elif sym_type == QgsSymbol.SymbolType.Fill:
        for i in range(symbol.symbolLayerCount()):
            sl = symbol.symbolLayer(i)
            if isinstance(sl, QgsSimpleFillSymbolLayer):
                fill = sl.fillColor()
                stroke = sl.strokeColor()
                style["color"] = _qcolor_to_hex(fill)
                style["strokeColor"] = _qcolor_to_hex(stroke)
                style["strokeWidth"] = float(sl.strokeWidth())
                style["fillOpacity"] = float(fill.alphaF())
                style["opacity"] = float(stroke.alphaF())
                break
    return style


def _symbology_from_categorized(renderer: QgsCategorizedSymbolRenderer) -> Dict[str, Any]:
    sym = dict(DEFAULT_SYMBOLOGY)
    sym["type"] = "categorized"
    sym["field"] = renderer.classAttribute() or ""
    sym["categories"] = []
    for cat in renderer.categories():
        val = cat.value()
        sym["categories"].append(
            {
                "value": "" if val is None else str(val),
                "label": cat.label() or ("" if val is None else str(val)),
                "color": _qcolor_to_hex(cat.symbol().color()),
            }
        )
    return sym


def _graduated_mode_name(renderer: QgsGraduatedSymbolRenderer) -> str:
    mapping = {
        QgsGraduatedSymbolRenderer.Mode.EqualInterval: "equalInterval",
        QgsGraduatedSymbolRenderer.Mode.Quantile: "quantile",
        QgsGraduatedSymbolRenderer.Mode.Jenks: "naturalBreaks",
        QgsGraduatedSymbolRenderer.Mode.StdDev: "equalInterval",
        QgsGraduatedSymbolRenderer.Mode.Pretty: "equalInterval",
    }
    return mapping.get(renderer.mode(), "equalInterval")


def _symbology_from_graduated(renderer: QgsGraduatedSymbolRenderer) -> Dict[str, Any]:
    sym = dict(DEFAULT_SYMBOLOGY)
    sym["type"] = "graduated"
    sym["field"] = renderer.classAttribute() or ""
    sym["mode"] = _graduated_mode_name(renderer)
    sym["classes"] = len(renderer.ranges()) or sym["classes"]
    sym["breaks"] = [float(r.upperValue()) for r in renderer.ranges()]
    sym["classColors"] = [_qcolor_to_hex(r.symbol().color()) for r in renderer.ranges()]
    return sym


def extract_mini_style_and_symbology(
    layer: QgsVectorLayer,
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Single / categorized / graduated symbology; rule-based → single style only."""
    renderer = layer.renderer()

    if isinstance(renderer, QgsCategorizedSymbolRenderer):
        base = renderer.sourceSymbol() or QgsSymbol.defaultSymbol(layer.geometryType())
        return _symbol_to_mini_style(base), _symbology_from_categorized(renderer)

    if isinstance(renderer, QgsGraduatedSymbolRenderer):
        base = renderer.sourceSymbol() or QgsSymbol.defaultSymbol(layer.geometryType())
        return _symbol_to_mini_style(base), _symbology_from_graduated(renderer)

    if isinstance(renderer, QgsRuleBasedRenderer):
        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        return _symbol_to_mini_style(symbol), None

    symbol = None
    if isinstance(renderer, QgsSingleSymbolRenderer):
        symbol = renderer.symbol()
    elif renderer is not None and hasattr(renderer, "symbol"):
        symbol = renderer.symbol()
    if not symbol:
        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
    return _symbol_to_mini_style(symbol), None


def _layer_tree_visible(layer_id: str) -> bool:
    node = QgsProject.instance().layerTreeRoot().findLayer(layer_id)
    if isinstance(node, QgsLayerTreeLayer):
        return node.isVisible()
    return True


def _transform_context():
    return QgsProject.instance().transformContext()


def _feature_to_dict(feat: QgsFeature, geom_wgs84: QgsGeometry) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    for field in feat.fields():
        name = field.name()
        if name.startswith("__"):
            continue
        props[name] = _qvariant_to_json(feat.attribute(name))
    geometry = json.loads(geom_wgs84.asJson())
    return {"type": "Feature", "geometry": geometry, "properties": props}


def features_in_extent_geojson(
    layer: QgsVectorLayer,
    extent: QgsRectangle,
    map_crs: QgsCoordinateReferenceSystem,
) -> Dict[str, Any]:
    layer_crs = layer.crs()
    to_layer = QgsCoordinateTransform(map_crs, layer_crs, _transform_context())
    layer_extent = to_layer.transformBoundingBox(extent)
    if layer_extent.isEmpty():
        return {"type": "FeatureCollection", "features": []}

    clip_geom = QgsGeometry.fromRect(layer_extent)
    dest = QgsCoordinateReferenceSystem(WGS84)
    to_wgs84 = QgsCoordinateTransform(layer_crs, dest, _transform_context())

    features_out: List[Dict[str, Any]] = []
    for feat in layer.getFeatures():
        geom = feat.geometry()
        if not geom or geom.isEmpty():
            continue
        if not geom.boundingBox().intersects(layer_extent):
            continue
        clipped = geom.intersection(clip_geom)
        if clipped.isEmpty():
            continue
        clipped_wgs = QgsGeometry(clipped)
        clipped_wgs.transform(to_wgs84)
        features_out.append(_feature_to_dict(feat, clipped_wgs))

    return {"type": "FeatureCollection", "features": features_out}


def camera_from_map_extent(
    extent: QgsRectangle, map_crs: QgsCoordinateReferenceSystem
) -> Dict[str, float]:
    """GEOV camera object (latitude, longitude, zoom, bearing, tilt)."""
    dest = QgsCoordinateReferenceSystem(WGS84)
    transform = QgsCoordinateTransform(map_crs, dest, _transform_context())
    wgs_extent = transform.transformBoundingBox(extent)
    center = wgs_extent.center()
    width_deg = max(wgs_extent.width(), 1e-9)
    zoom = max(1.0, min(22.0, math.log(360.0 / width_deg, 2)))
    return {
        "latitude": center.y(),
        "longitude": center.x(),
        "zoom": zoom,
        "bearing": 0.0,
        "tilt": 0.0,
    }


def collect_geov_export_layers(
    extent: QgsRectangle,
    map_crs: QgsCoordinateReferenceSystem,
    layer_ids: Optional[Sequence[str]] = None,
    visible_layers_only: bool = True,
) -> List[Dict[str, Any]]:
    project = QgsProject.instance()
    if layer_ids is None:
        layer_ids = [
            lid
            for lid, lyr in project.mapLayers().items()
            if isinstance(lyr, QgsVectorLayer)
        ]

    used: set[str] = set()
    out: List[Dict[str, Any]] = []

    for lid in layer_ids:
        layer = project.mapLayer(lid)
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            continue
        if visible_layers_only and not _layer_tree_visible(lid):
            continue

        geojson = features_in_extent_geojson(layer, extent, map_crs)
        if not geojson.get("features"):
            continue

        mini_style, symbology = extract_mini_style_and_symbology(layer)
        layer_file = unique_layer_file_name(layer.name(), used)
        out.append(
            {
                "name": layer.name(),
                "visible": _layer_tree_visible(lid),
                "style": mini_style,
                "geometryType": geometry_type_from_layer(layer),
                "symbology": symbology,
                "geojson": geojson,
                "layerFile": layer_file,
            }
        )
    return out


def build_geov_zip_bytes(
    project_name: str,
    extent: QgsRectangle,
    map_crs: QgsCoordinateReferenceSystem,
    layer_ids: Optional[Sequence[str]] = None,
    visible_layers_only: bool = True,
) -> bytes:
    export_layers = collect_geov_export_layers(
        extent, map_crs, layer_ids=layer_ids, visible_layers_only=visible_layers_only
    )
    camera = camera_from_map_extent(extent, map_crs)
    return build_geov_zip_bytes_from_layers(project_name, camera, export_layers)
