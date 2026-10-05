"""Apply Geov Format style / symbology to QgsVectorLayer."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsGraduatedSymbolRenderer,
    QgsRendererCategory,
    QgsRendererRange,
    QgsSingleSymbolRenderer,
    QgsSymbol,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .geov_format import DEFAULT_MINI_STYLE


def _hex_to_qcolor(hex_str: str, alpha_f: float = 1.0):
    from qgis.PyQt.QtGui import QColor

    m = re.match(r"^#?([0-9a-f]{6})$", str(hex_str or "").strip(), re.IGNORECASE)
    if not m:
        c = QColor(DEFAULT_MINI_STYLE["color"])
    else:
        c = QColor(f"#{m.group(1)}")
    c.setAlphaF(max(0.0, min(1.0, alpha_f)))
    return c


def _mini_style_to_colors(style: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    s = {**DEFAULT_MINI_STYLE, **(style or {})}
    return {
        "fill": _hex_to_qcolor(s["color"], s.get("fillOpacity", 0.5)),
        "stroke": _hex_to_qcolor(s["strokeColor"], s.get("opacity", 0.85)),
        "stroke_width": float(s.get("strokeWidth", 2)),
        "point_size": float(s.get("size", 6)),
    }


def _symbol_from_mini_style(layer: QgsVectorLayer, style: Optional[Dict[str, Any]]) -> QgsSymbol:
    colors = _mini_style_to_colors(style)
    symbol = QgsSymbol.defaultSymbol(layer.geometryType())
    gtype = QgsWkbTypes.geometryType(layer.wkbType())

    if gtype == QgsWkbTypes.GeometryType.PointGeometry:
        symbol.setColor(colors["fill"])
        symbol.setSize(colors["point_size"])
    elif gtype == QgsWkbTypes.GeometryType.LineGeometry:
        symbol.setColor(colors["stroke"])
        symbol.setWidth(colors["stroke_width"])
    else:
        symbol.setColor(colors["fill"])
        for i in range(symbol.symbolLayerCount()):
            sl = symbol.symbolLayer(i)
            if sl and hasattr(sl, "setStrokeColor"):
                sl.setStrokeColor(colors["stroke"])
            if sl and hasattr(sl, "setStrokeWidth"):
                sl.setStrokeWidth(colors["stroke_width"])
    return symbol


def apply_geov_symbology(
    layer: QgsVectorLayer,
    style: Optional[Dict[str, Any]],
    symbology: Optional[Dict[str, Any]],
) -> None:
    if not layer or not layer.isValid():
        return

    if symbology and symbology.get("type") == "categorized":
        field = symbology.get("field") or ""
        if field and symbology.get("categories"):
            categories = []
            for cat in symbology["categories"]:
                sym = QgsSymbol.defaultSymbol(layer.geometryType())
                sym.setColor(_hex_to_qcolor(cat.get("color", "#3182ce")))
                label = cat.get("label") or str(cat.get("value", ""))
                value = cat.get("value", "")
                categories.append(QgsRendererCategory(value, sym, label))
            if categories:
                base = _symbol_from_mini_style(layer, style)
                renderer = QgsCategorizedSymbolRenderer(field, categories)
                renderer.setSourceSymbol(base)
                layer.setRenderer(renderer)
                return

    if symbology and symbology.get("type") == "graduated":
        field = symbology.get("field") or ""
        breaks = symbology.get("breaks") or []
        class_colors = symbology.get("classColors") or []
        if field and breaks and class_colors:
            ranges = []
            lower = float("-inf")
            for i, upper in enumerate(breaks):
                color = class_colors[i] if i < len(class_colors) else class_colors[-1]
                sym = QgsSymbol.defaultSymbol(layer.geometryType())
                sym.setColor(_hex_to_qcolor(color))
                label = f"{lower} – {upper}" if lower > float("-inf") else f"≤ {upper}"
                ranges.append(QgsRendererRange(lower, float(upper), sym, label, True))
                lower = float(upper)
            if len(class_colors) > len(breaks):
                sym = QgsSymbol.defaultSymbol(layer.geometryType())
                sym.setColor(_hex_to_qcolor(class_colors[-1]))
                ranges.append(
                    QgsRendererRange(lower, float("inf"), sym, f"> {lower}", True)
                )
            if ranges:
                renderer = QgsGraduatedSymbolRenderer(field, ranges)
                renderer.setSourceSymbol(_symbol_from_mini_style(layer, style))
                layer.setRenderer(renderer)
                return

    symbol = _symbol_from_mini_style(layer, style)
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
