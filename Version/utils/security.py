"""
GeoInbox security helpers (Bandit-friendly URL / SQL / OS utilities).
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess  # nosec B404
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_LOG = logging.getLogger("GeoInbox")

_SQLITE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_URL_SCHEMES = frozenset(("http", "https"))


def quote_sqlite_identifier(name: str) -> str:
    """Quote a SQLite table/column name after validating it."""
    if not _SQLITE_IDENT_RE.match(name or ""):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return '"' + name.replace('"', '""') + '"'


def urlopen_allowed(url: Any, *args: Any, **kwargs: Any):
    """urllib.request.urlopen restricted to http/https."""
    if isinstance(url, urllib.request.Request):
        target = url.full_url
    else:
        target = str(url)
    scheme = urlparse(target).scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(f"Disallowed URL scheme: {scheme!r}")
    return urllib.request.urlopen(url, *args, **kwargs)  # nosec B310


def log_debug_exception(message: str, exc: BaseException | None = None) -> None:
    """Log suppressed exceptions at debug level (avoids bare except/pass)."""
    if exc is not None:
        _LOG.debug(message, exc_info=exc)
    else:
        _LOG.debug(message, exc_info=True)


def open_path_in_file_manager(path: Path) -> None:
    """Open a folder in the OS file manager (validated path)."""
    folder = Path(path).resolve()
    if folder.is_file():
        folder = folder.parent
    if not folder.is_dir():
        raise NotADirectoryError(folder)

    system = platform.system()
    if system == "Windows":
        os.startfile(str(folder))  # nosec B606
        return

    if system == "Darwin":
        opener = shutil.which("open")
        if not opener:
            raise OSError("'open' executable not found")
        subprocess.run([opener, str(folder)], check=False)  # nosec B603 B607
        return

    opener = shutil.which("xdg-open")
    if not opener:
        raise OSError("'xdg-open' executable not found")
    subprocess.run([opener, str(folder)], check=False)  # nosec B603 B607
