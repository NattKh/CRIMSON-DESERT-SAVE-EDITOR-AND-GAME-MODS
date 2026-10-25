"""Resolve buff and passive labels from the installed game client.

ItemBuffs used to download buff_names_community.json from GitHub. That file
is months behind 2.01, and the offline build dropped the sync. Every buff
and equip-passive already has a string_key in buffinfo / skill — the same
source DMM uses. This module reads those tables from the live install.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

_STRUCTURAL_PREFIXES = (
    "Equip_Socket_Passive_",
    "Equip_Set_Passive_",
    "Equip_Passive_",
    "Item_Stat_AbyssGear_",
    "Item_Skill_AbyssGear_",
    "Item_Stat_",
    "Item_Skill_",
    "BuffLevel_",
    "Passive_",
    "Socket_",
)

_EXACT_LABELS = {
    "BuffLevel_HP": "Max Health",
    "BuffLevel_MP": "Max Spirit",
    "BuffLevel_SP": "Max Stamina",
    "BuffLevel_HPRegen": "Health Regen",
}

_DISGUISE_NAMES = {
    "Ent": "Shadowleaf",
}

_LABEL_FIXES = {
    "Stamina Use Increase Rate": "Stamina Regen Rate",
    "Stamina Use Decrease Rate": "Stamina Cost Rate",
}

_WORD_FIXES = {
    "Hp": "HP",
    "Mp": "MP",
    "Sp": "SP",
    "Dobule": "Double",
    "DDD": "Attack Power",
    "DPV": "Defense Power",
    "PV": "Defense",
    "DOT": "Damage Over Time",
    "DHIT": "Accuracy",
    "ADR": "Damage Reduction",
    "CC": "Crowd Control",
    "SOUNDATTACK": "Sound Attack",
    "Imumune": "Immune",
}


def _disguise_label(string_key: str) -> str:
    marker = "Stealth_Animal_"
    idx = string_key.find(marker)
    if idx < 0:
        return ""
    rest = string_key[idx + len(marker):]
    if not rest:
        return ""
    creature = rest.rsplit("_", 1)[-1]
    if not creature:
        return ""
    shown = _DISGUISE_NAMES.get(creature, _humanise_words(creature))
    return f"Animal Disguise ({shown})"


def humanise_string_key(string_key: str) -> str:
    """Turn BuffLevel_AttackSpeedRate into 'Attack Speed Rate'."""
    if not string_key:
        return ""
    disguise = _disguise_label(string_key)
    if disguise:
        return disguise
    exact = _EXACT_LABELS.get(string_key)
    if exact:
        return exact
    text = string_key
    while True:
        before = text
        for prefix in _STRUCTURAL_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        if text == before:
            break
    cut = text.rfind("_")
    if cut > 0:
        head, tail = text[:cut], text[cut + 1:]
        if head.endswith("ByMaterialKey"):
            base = head[: -len("ByMaterialKey")]
            if base.startswith("Add"):
                base = base[3:]
            return f"{_humanise_words(base)} ({_humanise_words(tail)})"
    core = text
    lv = text.rfind("_LV")
    if lv >= 0 and text[lv + 3:].isdigit():
        core = text[:lv]
    out = _humanise_words(core)
    out = _LABEL_FIXES.get(out, out)
    return out or string_key


def _humanise_words(text: str) -> str:
    out: list[str] = []
    word: list[str] = []
    prev_lower = False
    for ch in text:
        if ch == "_":
            if word:
                out.append("".join(word))
                word = []
            prev_lower = False
            continue
        if ch.isupper() and prev_lower:
            if word:
                out.append("".join(word))
                word = []
        word.append(ch)
        prev_lower = ch.islower() or ch.isdigit()
    if word:
        out.append("".join(word))
    fixed = [_WORD_FIXES.get(w, w) for w in out if w]
    return " ".join(fixed)


def _extract_table(game_path: str, stem: str, table_name: str) -> list[dict]:
    import dmm_parser
    from table_layout import INTERNAL_DIR

    body = dmm_parser.extract_file(
        game_path, "0008", INTERNAL_DIR, f"{stem}.staticinfobody")
    header = dmm_parser.extract_file(
        game_path, "0008", INTERNAL_DIR, f"{stem}.staticinfoheader")
    return list(dmm_parser.parse_table(table_name, body, header))


def load_buff_names_from_game(game_path: str) -> dict[int, str]:
    """All buffinfo keys → humanised labels from the installed client."""
    names: dict[int, str] = {}
    for rec in _extract_table(game_path, "buffinfo", "buff_info"):
        try:
            key = int(rec.get("key") or 0)
        except (TypeError, ValueError):
            continue
        if key <= 0:
            continue
        label = humanise_string_key(str(rec.get("string_key") or ""))
        if label:
            names[key] = label
    return names


def load_passive_names_from_game(game_path: str) -> dict[int, str]:
    """Equip-passive skill keys → humanised labels from the installed client."""
    names: dict[int, str] = {}
    for rec in _extract_table(game_path, "skill", "skill_info"):
        string_key = str(rec.get("string_key") or "")
        if not string_key.startswith((
            "Equip_Passive_",
            "Equip_Socket_Passive_",
            "Equip_Set_Passive_",
        )):
            continue
        try:
            key = int(rec.get("key") or 0)
        except (TypeError, ValueError):
            continue
        if key <= 0:
            continue
        label = humanise_string_key(string_key)
        if label:
            names[key] = label
    return names


def load_client_name_maps(game_path: Optional[str]) -> tuple[dict[int, str], dict[int, str]]:
    """Return (buff_names, passive_names). Empty maps if the client is missing."""
    if not game_path:
        return {}, {}
    try:
        buffs = load_buff_names_from_game(game_path)
        passives = load_passive_names_from_game(game_path)
        log.info("Client names: %d buffs, %d equip passives from %s",
                 len(buffs), len(passives), game_path)
        return buffs, passives
    except Exception as exc:
        log.warning("Client name load failed: %s", exc)
        return {}, {}
