"""Read GeoInbox download folder from settings database."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from storage.database import get_database_path


def get_download_folder() -> Optional[Path]:
    db_path = get_database_path()
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = 'download_folder'")
            row = cur.fetchone()
            if row and row[0]:
                return Path(row[0])
            cur.execute("SELECT value FROM settings WHERE key = 'working_folder'")
            row = cur.fetchone()
            if row and row[0]:
                return Path(row[0])
    except sqlite3.Error:
        return None
    return None
