"""Reuse expensive parses across GUI handlers.

``build_result_from_raw`` fully decodes the 6.4 MB save blob (~5 s). The
result only carries structure — object/field offsets into the blob — while
every consumer re-reads values from the current bytes at those offsets. One
parse therefore stays valid until ``SaveData.decompressed_blob`` is wholesale
replaced; ``SaveData.__setattr__`` bumps ``parse_epoch`` on every replacement
(see models.py), which is the cache invalidation signal used here.

``game_map.json`` is ~22 MB of static game data; it is parsed once per process
into small name lookup tables.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Optional


class ParsedResultCache:
    """Hold the last full parse for exactly one SaveData generation.

    Keyed by SaveData identity plus its ``parse_epoch``; in-place byte edits
    keep the entry (offsets stay valid), replacing the blob evicts it. Holds a
    strong reference to the SaveData so id-reuse can never alias a new save
    onto a stale parse.
    """

    def __init__(self) -> None:
        self._save_ref: Any = None
        self._epoch: int = -1
        self._result: Any = None

    def get(self, save_data) -> Optional[Any]:
        if save_data is None:
            return None
        if (
            self._save_ref is save_data
            and self._epoch == getattr(save_data, "parse_epoch", 0)
        ):
            return self._result
        return None

    def store(self, save_data, result) -> None:
        if save_data is None or result is None:
            return
        self._save_ref = save_data
        self._epoch = getattr(save_data, "parse_epoch", 0)
        self._result = result

    def clear(self) -> None:
        self._save_ref = None
        self._epoch = -1
        self._result = None


_GAME_MAP_LOCK = threading.Lock()
_GAME_MAP_NAMES: Optional[dict] = None


def load_game_map_names() -> dict:
    """Parse game_map.json once and return small int->name lookup tables."""
    global _GAME_MAP_NAMES
    cached = _GAME_MAP_NAMES
    if cached is not None:
        return cached
    with _GAME_MAP_LOCK:
        if _GAME_MAP_NAMES is not None:
            return _GAME_MAP_NAMES
        names = {
            'factions': {},
            'factionnodes': {},
            'characters': {},
            'sublevels': {},
        }
        try:
            base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
            gm_path = os.path.join(base, 'game_map.json')
            if os.path.isfile(gm_path):
                with open(gm_path, 'r', encoding='utf-8') as f:
                    gm = json.load(f)
                for k, v in gm.get('factions', {}).items():
                    if isinstance(v, dict):
                        names['factions'][int(k)] = v.get('name', '')
                for k, v in gm.get('factionnodes', {}).items():
                    if isinstance(v, dict):
                        names['factionnodes'][int(k)] = v.get('name', '')
                for k, v in gm.get('characters', {}).items():
                    if isinstance(v, dict):
                        names['characters'][int(k)] = v.get('name', '')
                    elif isinstance(v, str):
                        names['characters'][int(k)] = v
                for k, v in gm.get('sublevels', {}).items():
                    if isinstance(v, dict):
                        names['sublevels'][int(k)] = v.get('name', f'SubLevel_{k}')
                    else:
                        names['sublevels'][int(k)] = str(v)
        except Exception:
            pass
        _GAME_MAP_NAMES = names
        return names
