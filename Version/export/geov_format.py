"""
Portable GEOV (ZIP) — Python mirror of miniGISimple app/js/io/geov-format.js.

Layout: project.json + layers/*.geojson
Spec: documentation/02-geov-symbology.html in miniGISimple
"""

from __future__ import annotations

import json
import re
import zipfile
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Set

GEOV_VERSION = "2.0"
DESCRIPTOR = "project.json"
GEOV_SYMBOLOGY_SCHEMA = "miniGISimple-symbology-v1"

GEOV_REQUIRES_VECTOR_MESSAGE = (
    "Add at least one vector layer with features in the map extent to export to Geov."
)

DEFAULT_MINI_STYLE = {
    "color": "#3182ce",
    "strokeColor": "#2c5282",
    "strokeWidth": 2,
    "opacity": 0.85,
    "size": 6,
    "fillOpacity": 0.5,
}

DEFAULT_SYMBOLOGY = {
    "type": "single",
    "field": "",
    "colorScheme": "pastel",
    "classes": 5,
    "mode": "equalInterval",
    "fixedInterval": 10,
    "rampStart": "#e8f4f8",
    "rampEnd": "#5c7cfa",
    "categories": [],
    "breaks": [],
    "classColors": [],
}


def normalize_geov_project_name(input_name: str) -> str:
    s = str(input_name or "").strip()
    if not s:
        return ""
    s = re.sub(r"\.geov$", "", s, flags=re.IGNORECASE).strip()
    return s


def sanitize_geov_file_base(name: str) -> str:
    base = normalize_geov_project_name(name)
    base = re.sub(r'[\\/:*?"<>|]', "_", base)
    base = re.sub(r"\s+", "_", base)
    if not base:
        base = "GeoV_Project"
    if len(base) > 80:
        base = base[:80]
    return base


def geov_download_file_name(project_or_file_name: str) -> str:
    raw = str(project_or_file_name or "").strip()
    if not raw:
        return "GeoV_Project.geov"
    without_ext = re.sub(r"\.geov$", "", raw, flags=re.IGNORECASE).strip()
    base = sanitize_geov_file_base(without_ext or raw)
    return f"{base}.geov"


def derive_layer_display_name(file_name: str) -> str:
    name = str(file_name or "").strip()
    if not name:
        return "layer"
    slash = max(name.rfind("/"), name.rfind("\\"))
    if slash >= 0 and slash < len(name) - 1:
        name = name[slash + 1 :]
    name = re.sub(r"\.(geojson|json|geo)$", "", name, flags=re.IGNORECASE)
    return name or "layer"


def layer_entry_base(layer_name: str) -> str:
    base = derive_layer_display_name(layer_name)
    base = re.sub(r'[\\/:*?"<>|]', "_", base).strip()
    return base or "layer"


def unique_layer_file_name(layer_name: str, used: Set[str]) -> str:
    base = layer_entry_base(layer_name)
    candidate = f"{base.lower()}.geojson"
    if candidate not in used:
        used.add(candidate)
        return candidate
    n = 2
    while True:
        candidate = f"{base}_{n}.geojson".lower()
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1


def _signed_argb_from_css_hex(hex_str: str, alpha: int = 255) -> int:
    m = re.match(r"^#?([0-9a-f]{6})$", str(hex_str or "").strip(), re.IGNORECASE)
    if not m:
        rgb = 0x3182ce
    else:
        rgb = int(m.group(1), 16)
    v = ((alpha & 0xFF) << 24) | (rgb & 0xFFFFFF)
    if v >= 0x80000000:
        v -= 0x100000000
    return v


def mini_style_to_geov(style: Optional[Dict[str, Any]], geometry_type: str) -> Dict[str, Any]:
    """Same as miniStyleToGeov() in geov-format.js."""
    s = style or {}
    gt = geometry_type or "mixed"
    return {
        "fillColor": _signed_argb_from_css_hex(s.get("color") or DEFAULT_MINI_STYLE["color"]),
        "fillOpacity": s.get("fillOpacity", DEFAULT_MINI_STYLE["fillOpacity"]),
        "strokeColor": _signed_argb_from_css_hex(
            s.get("strokeColor") or DEFAULT_MINI_STYLE["strokeColor"]
        ),
        "strokeWidth": s.get("strokeWidth", DEFAULT_MINI_STYLE["strokeWidth"]),
        "strokeOpacity": s.get("opacity", DEFAULT_MINI_STYLE["opacity"]),
        "pointColor": _signed_argb_from_css_hex(s.get("color") or DEFAULT_MINI_STYLE["color"]),
        "pointSize": s.get("size", DEFAULT_MINI_STYLE["size"]),
        "hasPoint": gt in ("point", "mixed"),
        "hasStroke": gt in ("line", "polygon", "mixed"),
        "hasFill": gt in ("polygon", "mixed"),
    }


def strip_internal_properties(geojson: Dict[str, Any]) -> Dict[str, Any]:
    features = []
    for f in geojson.get("features") or []:
        props = dict(f.get("properties") or {})
        for k in list(props.keys()):
            if str(k).startswith("__"):
                del props[k]
        features.append(
            {"type": "Feature", "geometry": f.get("geometry"), "properties": props}
        )
    return {"type": "FeatureCollection", "features": features}


def serialize_symbology_for_geov(sym: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Same as serializeSymbologyForGeov() in geov-format.js."""
    if not sym or sym.get("type") == "single":
        return None
    if sym.get("type") == "categorized" and not sym.get("categories"):
        return None
    if sym.get("type") == "graduated" and (
        not sym.get("breaks") or not sym.get("classColors")
    ):
        return None
    return {
        "type": sym["type"],
        "field": sym.get("field") or "",
        "colorScheme": sym.get("colorScheme") or "pastel",
        "classes": sym.get("classes", 5),
        "mode": sym.get("mode") or "equalInterval",
        "fixedInterval": sym.get("fixedInterval", 10),
        "rampStart": sym.get("rampStart", DEFAULT_SYMBOLOGY["rampStart"]),
        "rampEnd": sym.get("rampEnd", DEFAULT_SYMBOLOGY["rampEnd"]),
        "categories": [
            {
                "value": c.get("value"),
                "label": c.get("label"),
                "color": c.get("color"),
            }
            for c in (sym.get("categories") or [])
        ],
        "breaks": list(sym.get("breaks") or []),
        "classColors": list(sym.get("classColors") or []),
    }


def build_geov_descriptor(
    project_name: str,
    camera: Dict[str, Any],
    export_layers: Sequence[Dict[str, Any]],
    created_by: str = "GeoInbox",
) -> Dict[str, Any]:
    """
    export_layers items: name, visible, style (mini), geometryType, symbology?, layerFile
    Mirrors buildGeovDescriptor() in geov-format.js.
    """
    has_symbology = False
    layers_array: List[Dict[str, Any]] = []

    for layer in export_layers:
        layer_obj: Dict[str, Any] = {
            "name": layer["name"],
            "visible": layer.get("visible") is not False,
            "style": mini_style_to_geov(
                layer.get("style"), layer.get("geometryType") or "mixed"
            ),
        }
        if layer.get("layerFile"):
            layer_obj["layerFile"] = layer["layerFile"]
        serialized = serialize_symbology_for_geov(layer.get("symbology"))
        if serialized:
            layer_obj["symbology"] = serialized
            has_symbology = True
        layers_array.append(layer_obj)

    provenance: Dict[str, Any] = {
        "createdBy": created_by,
        "packageFormat": "portable-zip-v1",
        "contentType": "application/zip",
    }
    if has_symbology:
        provenance["symbologySchema"] = GEOV_SYMBOLOGY_SCHEMA

    descriptor_name = normalize_geov_project_name(project_name) or "GeoV export"
    return {
        "version": GEOV_VERSION,
        "type": "GeoVProject",
        "name": descriptor_name,
        "camera": camera,
        "layers": layers_array,
        "provenance": provenance,
    }


def build_geov_zip_bytes_from_layers(
    project_name: str,
    camera: Dict[str, Any],
    export_layers: Sequence[Dict[str, Any]],
    created_by: str = "GeoInbox",
) -> bytes:
    """
    Mirrors buildGeovZipBytes() packaging (descriptor + layers/*.geojson).
    Each export layer must include: name, visible, style, geometryType, geojson, layerFile.
    """
    if not export_layers:
        err = ValueError(GEOV_REQUIRES_VECTOR_MESSAGE)
        err.code = "GEOV_NO_DATA"  # type: ignore[attr-defined]
        raise err

    descriptor_name = normalize_geov_project_name(project_name) or "GeoV export"
    descriptor = build_geov_descriptor(descriptor_name, camera, export_layers, created_by)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(DESCRIPTOR, json.dumps(descriptor, indent=2))
        for layer in export_layers:
            geojson = strip_internal_properties(layer["geojson"])
            zf.writestr(
                f"layers/{layer['layerFile']}",
                json.dumps(geojson, separators=(",", ":")),
            )
    return buffer.getvalue()


def argb_int_to_css_hex(value: int) -> str:
    """Same as argbIntToCssHex() in geov-format.js."""
    v = int(value) & 0xFFFFFFFF
    if v >= 0x80000000:
        v -= 0x100000000
    rgb = v & 0xFFFFFF
    return f"#{rgb:06x}"


def geov_style_to_mini(style_obj: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Same as geovStyleToMini() in geov-format.js."""
    if not style_obj:
        return None
    return {
        "color": argb_int_to_css_hex(style_obj.get("fillColor", style_obj.get("pointColor", 0))),
        "strokeColor": argb_int_to_css_hex(style_obj.get("strokeColor", 0)),
        "strokeWidth": style_obj.get("strokeWidth", 2),
        "opacity": style_obj.get("strokeOpacity", style_obj.get("opacity", 0.85)),
        "fillOpacity": style_obj.get("fillOpacity", 0.5),
        "size": style_obj.get("pointSize", 6),
    }


def layer_names_match_portable(descriptor_name: str, name_from_zip_entry: str) -> bool:
    if not descriptor_name or not name_from_zip_entry:
        return False
    if descriptor_name.lower() == name_from_zip_entry.lower():
        return True
    a = layer_entry_base(descriptor_name)
    b = layer_entry_base(name_from_zip_entry)
    if a.lower() == b.lower():
        return True
    return a.lower().replace(" ", "_") == b.lower().replace(" ", "_")


def _is_zip_bytes(data: bytes) -> bool:
    return len(data) >= 4 and data[0] == 0x50 and data[1] == 0x4B


def parse_geov_zip(data: bytes) -> Dict[str, Any]:
    """Same as parseGeovZip() in geov-format.js."""
    if not _is_zip_bytes(data):
        raise ValueError("Not a Geov Format package (expected ZIP / PK header).")

    project_json: Optional[Dict[str, Any]] = None
    layer_geojson: List[Dict[str, Any]] = []

    with zipfile.ZipFile(BytesIO(data), "r") as zf:
        for path in zf.namelist():
            lower = path.lower().replace("\\", "/")
            if lower.endswith("/"):
                continue
            raw = zf.read(path)
            text = raw.decode("utf-8", errors="replace").lstrip("\ufeff").strip()

            if "/" not in lower and lower.endswith(".json"):
                project_json = json.loads(text)
            elif lower.startswith("layers/") and lower.endswith(".geojson"):
                entry_name = derive_layer_display_name(lower[len("layers/") :])
                layer_geojson.append(
                    {"entryName": entry_name, "geojson": json.loads(text)}
                )

    if not layer_geojson:
        raise ValueError("Geov Format package has no layers/*.geojson files.")

    descriptor_layers = (project_json or {}).get("layers") or []
    parsed_layers: List[Dict[str, Any]] = []

    for item in layer_geojson:
        name = item["entryName"]
        visible = True
        style = None
        symbology = None

        for desc in descriptor_layers:
            file_match = bool(
                desc.get("layerFile")
                and layer_names_match_portable(desc["layerFile"], item["entryName"])
            )
            if layer_names_match_portable(desc.get("name", ""), item["entryName"]) or file_match:
                name = desc.get("name") or name
                visible = desc.get("visible") is not False
                style = geov_style_to_mini(desc.get("style"))
                sym = desc.get("symbology")
                if sym and sym.get("type") and sym.get("type") != "single":
                    symbology = {**DEFAULT_SYMBOLOGY, **sym}
                break

        parsed_layers.append(
            {
                "name": name,
                "visible": visible,
                "style": style,
                "symbology": symbology,
                "geojson": item["geojson"],
                "entryName": item["entryName"],
            }
        )

    camera = (project_json or {}).get("camera")
    map_view = None
    if camera:
        map_view = {
            "center": [camera.get("longitude", 0), camera.get("latitude", 0)],
            "zoom": camera.get("zoom", 2),
            "pitch": camera.get("tilt", 0),
            "bearing": camera.get("bearing", 0),
        }

    return {
        "projectName": (project_json or {}).get("name") or "Imported Geov",
        "mapView": map_view,
        "projectJson": project_json,
        "layers": parsed_layers,
    }
