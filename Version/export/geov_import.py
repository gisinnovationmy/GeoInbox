"""Import Geov Format: extract to download folder and load into QGIS."""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsLayerTreeGroup,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.utils import iface

from .geov_format import parse_geov_zip, sanitize_geov_file_base, strip_internal_properties
from .geov_qgis_style import apply_geov_symbology

WGS84 = "EPSG:4326"


@dataclass
class GeovImportResult:
    extract_dir: Path
    layer_count: int
    project_name: str


def extract_geov_to_folder(geov_path: Path, download_root: Path) -> Path:
    """Copy .geov and unpack project.json + layers/*.geojson under download_root/geov/."""
    geov_path = Path(geov_path).resolve()
    if not geov_path.is_file():
        raise FileNotFoundError(f"File not found: {geov_path}")

    data = geov_path.read_bytes()
    parsed = parse_geov_zip(data)
    base = sanitize_geov_file_base(parsed.get("projectName") or geov_path.stem)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = download_root / "geov" / f"{base}_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)

    shutil.copy2(geov_path, dest / geov_path.name)

    with zipfile.ZipFile(geov_path, "r") as zf:
        zf.extractall(dest)

    return dest


def _write_layer_geojson_files(extract_dir: Path, parsed: dict) -> List[dict]:
    """Ensure layers/*.geojson on disk; return layer entries with geojsonPath."""
    layers_dir = extract_dir / "layers"
    layers_dir.mkdir(parents=True, exist_ok=True)
    out = []
    used_names: set[str] = set()

    for layer in parsed.get("layers") or []:
        entry = layer.get("entryName") or layer.get("name") or "layer"
        safe = sanitize_geov_file_base(entry).lower()
        fname = f"{safe}.geojson"
        n = 2
        while fname in used_names:
            fname = f"{safe}_{n}.geojson"
            n += 1
        used_names.add(fname)

        path = layers_dir / fname
        geojson = strip_internal_properties(layer.get("geojson") or {})
        path.write_text(json.dumps(geojson, separators=(",", ":")), encoding="utf-8")
        out.append({**layer, "geojsonPath": path})
    return out


def _find_toplevel_group(root, name: str) -> Optional[QgsLayerTreeGroup]:
    for child in root.children():
        if isinstance(child, QgsLayerTreeGroup) and child.name() == name:
            return child
    return None


def load_geov_layers_into_qgis(
    extract_dir: Path,
    parsed: dict,
) -> List[QgsVectorLayer]:
    layer_entries = _write_layer_geojson_files(extract_dir, parsed)
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    geov_group_name = f"Geov: {parsed.get('projectName', 'import')}"
    project_group = _find_toplevel_group(root, geov_group_name)
    if project_group is None:
        project_group = root.insertGroup(0, geov_group_name)

    created: List[QgsVectorLayer] = []
    for entry in layer_entries:
        path = entry["geojsonPath"]
        name = entry.get("name") or path.stem
        layer = QgsVectorLayer(str(path), name, "ogr")
        if not layer.isValid():
            continue
        layer.setCrs(QgsCoordinateReferenceSystem(WGS84))
        apply_geov_symbology(layer, entry.get("style"), entry.get("symbology"))
        project.addMapLayer(layer, False)
        project_group.addLayer(layer)
        node = root.findLayer(layer.id())
        if node is not None:
            node.setItemVisibilityChecked(entry.get("visible") is not False)
        created.append(layer)

    return created


def _zoom_to_layers(layers: List[QgsVectorLayer], map_view: Optional[dict]) -> None:
    if not iface or not layers:
        return
    canvas = iface.mapCanvas()
    dest_crs = canvas.mapSettings().destinationCrs()
    wgs = QgsCoordinateReferenceSystem(WGS84)
    transform = QgsCoordinateTransform(wgs, dest_crs, QgsProject.instance().transformContext())

    extent = QgsRectangle()
    for layer in layers:
        ext = layer.extent()
        if ext.isEmpty():
            continue
        ext = transform.transformBoundingBox(ext)
        if extent.isEmpty():
            extent = ext
        else:
            extent.combineExtentWith(ext)

    if not extent.isEmpty():
        extent.scale(1.1)
        canvas.setExtent(extent)
        canvas.refresh()


def import_geov_file(
    geov_path: Path,
    download_root: Path,
    zoom_to_data: bool = True,
) -> GeovImportResult:
    geov_path = Path(geov_path)
    data = geov_path.read_bytes()
    parsed = parse_geov_zip(data)
    extract_dir = extract_geov_to_folder(geov_path, download_root)
    layers = load_geov_layers_into_qgis(extract_dir, parsed)
    if not layers:
        raise ValueError("Geov Format package has no layers that could be loaded in QGIS.")
    if zoom_to_data:
        _zoom_to_layers(layers, parsed.get("mapView"))
    return GeovImportResult(
        extract_dir=extract_dir,
        layer_count=len(layers),
        project_name=str(parsed.get("projectName") or geov_path.stem),
    )


def collect_geov_paths_from_urls(urls) -> List[Path]:
    from ..utils.mime_drop import collect_geov_paths_from_urls as _collect

    return _collect(urls)
