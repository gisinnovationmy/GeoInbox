"""
Resolve dropped files from Explorer, Outlook, and other Windows drag sources.

Outlook (classic and many M365 builds) does not populate QMimeData.urls(); it uses
CFSTR_FILEDESCRIPTOR / FileContents via Qt's application/x-qt-windows-mime formats.
"""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path
from typing import List, Optional
from urllib.parse import unquote, urlparse

from qgis.core import QgsMessageLog, Qgis

# Win32 FILEDESCRIPTORW (FILEGROUPDESCRIPTORW entries)
_FILEDESCRIPTORW_SIZE = 592
_FDW_FILENAME_OFFSET = 72
_FDW_FILENAME_BYTES = 520  # WCHAR[260]


def is_probable_geov_package(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() in (".geov", ".zip"):
        return True
    try:
        head = path.read_bytes()[:4]
    except OSError:
        return False
    return head[:2] == b"PK"


def collect_geov_paths_from_urls(urls) -> List[Path]:
    paths: List[Path] = []
    for url in urls:
        local = url.toLocalFile() if hasattr(url, "toLocalFile") else str(url)
        if not local:
            continue
        p = Path(local)
        if is_probable_geov_package(p):
            paths.append(p)
    return paths


def _paths_from_text_uri_list(text: str) -> List[Path]:
    paths: List[Path] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("file:"):
            parsed = urlparse(line)
            local = unquote(parsed.path)
            if sys.platform == "win32" and local.startswith("/") and len(local) > 2 and local[2] == ":":
                local = local[1:]
            p = Path(local)
            if is_probable_geov_package(p):
                paths.append(p)
    return paths


def _descriptor_format(mime) -> Optional[str]:
    for fmt in mime.formats():
        lower = fmt.lower()
        if "filegroupdescriptorw" in lower or "filegroupdescriptor" in lower:
            return fmt
    return None


def _file_contents_format(mime, index: int) -> Optional[str]:
    indexed = f'index={index}'
    for fmt in mime.formats():
        lower = fmt.lower()
        if "filecontents" not in lower:
            continue
        if index == 0 and indexed not in lower:
            return fmt
        if indexed in lower:
            return fmt
    candidate = f'application/x-qt-windows-mime;value="FileContents";index={index}'
    if mime.hasFormat(candidate):
        return candidate
    if index == 0:
        plain = 'application/x-qt-windows-mime;value="FileContents"'
        if mime.hasFormat(plain):
            return plain
    return None


def _read_name_at(descriptor_bytes: bytes, off: int) -> str:
    end = off + _FDW_FILENAME_OFFSET + _FDW_FILENAME_BYTES
    if end > len(descriptor_bytes):
        return ""
    chunk = descriptor_bytes[off + _FDW_FILENAME_OFFSET : end]
    return chunk.decode("utf-16-le", errors="ignore").strip("\x00").strip()


def _decode_descriptor_names(descriptor_bytes: bytes) -> List[str]:
    if len(descriptor_bytes) < 4:
        return []
    count = struct.unpack_from("<I", descriptor_bytes, 0)[0]
    count = max(0, min(int(count), 32))
    if count == 0:
        return []

    # FILEDESCRIPTORW is 592 bytes; some sources use 344-byte FILEDESCRIPTOR.
    stride = _FILEDESCRIPTORW_SIZE
    if count > 1:
        payload = len(descriptor_bytes) - 4
        if payload % count == 0:
            stride = payload // count
        elif payload // count in (344, 592):
            stride = payload // count

    names: List[str] = []
    for i in range(count):
        off = 4 + i * stride
        name = _read_name_at(descriptor_bytes, off)
        if not name:
            name = f"attachment_{i + 1}.geov"
        names.append(Path(name).name)
    return names


def extract_windows_file_group_drop(mime, dest_dir: Path) -> List[Path]:
    """Materialize Outlook / OLE file drops into dest_dir."""
    if sys.platform != "win32":
        return []

    desc_fmt = _descriptor_format(mime)
    if not desc_fmt:
        return []

    dest_dir.mkdir(parents=True, exist_ok=True)
    descriptor = bytes(mime.data(desc_fmt))
    names = _decode_descriptor_names(descriptor)
    if not names:
        return []

    content_formats = [f for f in mime.formats() if "filecontents" in f.lower()]

    written: List[Path] = []
    for i, name in enumerate(names):
        content_fmt = _file_contents_format(mime, i)
        if not content_fmt and i < len(content_formats):
            content_fmt = content_formats[i]
        if not content_fmt:
            continue
        payload = bytes(mime.data(content_fmt))
        if not payload:
            continue
        safe = re.sub(r'[\\/:*?"<>|]', "_", name) or f"attachment_{i + 1}"
        if "." not in Path(safe).suffix and payload[:2] == b"PK":
            safe = f"{safe}.geov"
        out = dest_dir / safe
        if out.exists():
            stem, suf = out.stem, out.suffix
            n = 2
            while out.exists():
                out = dest_dir / f"{stem}_{n}{suf}"
                n += 1
        out.write_bytes(payload)
        written.append(out)
    return written


def mime_might_contain_geov(mime) -> bool:
    """Used in dragEnterEvent — accept before we know the file extension."""
    if mime.hasUrls() and collect_geov_paths_from_urls(mime.urls()):
        return True
    if mime.hasText():
        if _paths_from_text_uri_list(mime.text()):
            return True
    if sys.platform == "win32":
        if _descriptor_format(mime):
            return True
        for fmt in mime.formats():
            if "filecontents" in fmt.lower():
                return True
    return False


def resolve_geov_paths_from_mime(mime, scratch_dir: Optional[Path]) -> List[Path]:
    """Return local .geov (or ZIP) paths from a QDropEvent mime payload."""
    paths = collect_geov_paths_from_urls(mime.urls()) if mime.hasUrls() else []
    if not paths and mime.hasText():
        paths = _paths_from_text_uri_list(mime.text())

    if not paths and scratch_dir and sys.platform == "win32":
        extracted = extract_windows_file_group_drop(mime, scratch_dir)
        paths = [p for p in extracted if is_probable_geov_package(p)]
        if extracted and not paths:
            QgsMessageLog.logMessage(
                "GeoInbox: Outlook drop contained files but none looked like Geov Format "
                f"({', '.join(p.name for p in extracted)}).",
                "GeoInbox",
                Qgis.Warning,
            )

    if not paths and mime.formats():
        QgsMessageLog.logMessage(
            "GeoInbox: unsupported drop formats: " + ", ".join(mime.formats()[:12]),
            "GeoInbox",
            Qgis.Info,
        )
    return paths
