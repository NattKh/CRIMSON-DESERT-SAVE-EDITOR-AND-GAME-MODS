"""Resolve character and companion names from the installed game client.

The bundled character_catalog.json lags behind game updates. The client's
characterinfo table is authoritative: 58 KB header plus a body that maps
every character key to its internal name. Parsing takes about a second, so the
result is cached next to the executable (or source folder) and reused.
"""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import sys

log = logging.getLogger(__name__)

CACHE_FILENAME = "character_client_names.json"
INTERNAL_DIR = "gamedata/binarystaticinfo__/bin"
HEADER_NAME = "characterinfo.staticinfoheader"
BODY_NAME = "characterinfo.staticinfobody"

_MOUNT_TOKENS = (
    "riding_horse", "riding_", "horse", "mount", "dragon", "wyvern",
    "warmachine", "machine", "ship", "boat", "balloon", "camel",
    "elephant", "deer", "bear", "wolf", "warthog", "ibex",
)
_PET_PREFIXES = ("animal_", "pet_", "companion_")
_MERC_PREFIXES = ("nhm_", "npc_", "mon_", "mercenary_", "unique_")


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _cache_path() -> str:
    return os.path.join(_app_dir(), CACHE_FILENAME)


def extract_character_names(game_path: str) -> dict[int, str]:
    """Read key -> internal_name from the installed client's characterinfo."""
    import crimson_rs

    header = crimson_rs.extract_file(game_path, "0008", INTERNAL_DIR, HEADER_NAME)
    body = crimson_rs.extract_file(game_path, "0008", INTERNAL_DIR, BODY_NAME)
    if len(header) < 2:
        return {}
    count = struct.unpack_from("<H", header, 0)[0]
    names: dict[int, str] = {}
    for index in range(count):
        base = 2 + index * 8
        if base + 8 > len(header):
            break
        _key, offset = struct.unpack_from("<II", header, base)
        if offset + 8 > len(body):
            continue
        record_key, name_length = struct.unpack_from("<II", body, offset)
        if not record_key or name_length > 512:
            continue
        start = offset + 8
        if start + name_length > len(body):
            continue
        names[int(record_key)] = body[start:start + name_length].decode("ascii", "replace")
    return names


def load_character_names(game_path: str = "") -> dict[int, str]:
    """Return cached client names, extracting them once if a game path is given."""
    cached = _read_cache()
    if cached:
        return cached
    if not game_path or not os.path.isdir(game_path):
        return {}
    try:
        names = extract_character_names(game_path)
    except Exception as exc:
        log.warning("Could not read characterinfo from %s: %s", game_path, exc)
        return {}
    if names:
        _write_cache(names)
    return names


def _read_cache() -> dict[int, str]:
    try:
        with open(_cache_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return {int(key): str(value) for key, value in data.items() if str(value)}
    except (OSError, ValueError, TypeError):
        return {}


def _write_cache(names: dict[int, str]) -> None:
    try:
        with open(_cache_path(), "w", encoding="utf-8") as handle:
            json.dump({str(key): value for key, value in names.items()}, handle)
    except OSError:
        pass


def prettify_internal_name(internal: str) -> str:
    """Turn an internal character name into a readable label."""
    text = internal or ""
    lowered = text.lower()
    for prefix in (
        "nhm_unique_", "nhm_", "npc_", "mon_", "boss_",
        "animal_", "pet_", "companion_", "riding_",
    ):
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            break
    text = re.sub(r"_(clone|ai)$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"_\d+$", "", text)
    text = text.replace("_", " ").strip()
    if not text:
        return internal or ""
    return text.title()


def classify_internal_name(internal: str) -> str:
    """Classify a character by its internal name: Mount, Pet, Mercenary, Companion."""
    lowered = (internal or "").lower()
    if any(token in lowered for token in _MOUNT_TOKENS):
        return "Mount"
    if lowered.startswith(_PET_PREFIXES):
        return "Pet"
    if lowered.startswith(_MERC_PREFIXES):
        return "Mercenary"
    return "Companion"
