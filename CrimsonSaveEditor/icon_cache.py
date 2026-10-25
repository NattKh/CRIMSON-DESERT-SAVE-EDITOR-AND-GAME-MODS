from __future__ import annotations

import logging
import os
import sys
from typing import Callable, Dict, Optional

from PySide6.QtGui import QPixmap
from PySide6.QtCore import QSize, Qt

log = logging.getLogger(__name__)

ICON_SIZE = 32

def _get_local_icons_dir():
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "icons_local")


class IconCache:

    def __init__(self, icon_urls_path: Optional[str] = None):
        self._pixmaps: Dict[int, QPixmap] = {}
        self._local_dir = _get_local_icons_dir()
        os.makedirs(self._local_dir, exist_ok=True)

    def has_icon(self, item_key: int) -> bool:
        return os.path.isfile(os.path.join(self._local_dir, f"{item_key}.webp"))

    def get_pixmap(self, item_key: int) -> Optional[QPixmap]:
        if item_key in self._pixmaps:
            return self._pixmaps[item_key]

        local_path = os.path.join(self._local_dir, f"{item_key}.webp")
        if os.path.isfile(local_path):
            px = QPixmap(local_path)
            if not px.isNull():
                self._pixmaps[item_key] = px
                return px

        return None

    def request_icon(self, item_key: int, callback: Callable[[int, QPixmap], None]) -> None:
        if item_key in self._pixmaps:
            callback(item_key, self._pixmaps[item_key])
            return

        local_path = os.path.join(self._local_dir, f"{item_key}.webp")
        if os.path.isfile(local_path):
            px = QPixmap(local_path)
            if not px.isNull():
                self._pixmaps[item_key] = px
                callback(item_key, px)
                return

        # Icons are deliberately local-only in this build.
        return

    def preload_keys(self, keys: list, callback: Callable[[int, QPixmap], None]) -> None:
        for key in keys:
            if key not in self._pixmaps:
                self.request_icon(key, callback)

    def get_merc_pixmap(self, char_key: int) -> Optional[QPixmap]:
        cache_key = f"merc_{char_key}"
        if cache_key in self._pixmaps:
            return self._pixmaps[cache_key]

        merc_dir = os.path.join(os.path.dirname(self._local_dir), "icons_mercenary")
        local_path = os.path.join(merc_dir, f"{char_key}.webp")
        if os.path.isfile(local_path):
            px = QPixmap(local_path)
            if not px.isNull():
                self._pixmaps[cache_key] = px
                return px
        return None

    @property
    def coverage(self) -> int:
        try:
            return len([f for f in os.listdir(self._local_dir) if f.endswith('.webp')])
        except Exception:
            return 0
