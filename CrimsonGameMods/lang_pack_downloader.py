
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)

_MY_DIR = os.path.dirname(os.path.abspath(__file__))

GITHUB_BASE = "https://raw.githubusercontent.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR/main/locale"
MANIFEST_URL = f"{GITHUB_BASE}/manifest.json"

_UA = "CrimsonSaveEditor/lang-pack"

SUPPORTED_LANGS = (
    "ja", "ko", "zh", "zh-tw", "de", "es", "es-mx",
    "fr", "it", "pl", "pt-br", "ru", "tr",
)

NATIVE_NAMES: Dict[str, str] = {
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "zh": "简体中文",
    "zh-tw": "繁體中文",
    "de": "Deutsch",
    "es": "Español",
    "es-mx": "Español (México)",
    "fr": "Français",
    "it": "Italiano",
    "pl": "Polski",
    "pt-br": "Português (Brasil)",
    "ru": "Русский",
    "tr": "Türkçe",
}

ENGLISH_NAMES: Dict[str, str] = {
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese (Simplified)",
    "zh-tw": "Chinese (Traditional)",
    "de": "German",
    "es": "Spanish",
    "es-mx": "Spanish (Mexico)",
    "fr": "French",
    "it": "Italian",
    "pl": "Polish",
    "pt-br": "Portuguese (Brazil)",
    "ru": "Russian",
    "tr": "Turkish",
}


def _system_user_locale_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "CrimsonSaveEditor" / "locale"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CrimsonSaveEditor" / "locale"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "CrimsonSaveEditor" / "locale"
    return Path.home() / ".local" / "share" / "CrimsonSaveEditor" / "locale"


def _exe_locale_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(os.path.dirname(os.path.abspath(sys.executable))) / "locale"
    return Path(_MY_DIR) / "locale"


def _meipass_locale_dir() -> Optional[Path]:
    mei = getattr(sys, "_MEIPASS", None)
    return Path(mei) / "locale" if mei else None


def _dist_locale_dir() -> Path:
    return Path(_MY_DIR) / "dist" / "locale"


def search_dirs() -> List[Path]:
    out: List[Path] = []
    seen: set = set()

    for candidate in (
        _system_user_locale_dir(),
        _exe_locale_dir(),
        _meipass_locale_dir(),
        Path(_MY_DIR) / "locale",
        _dist_locale_dir(),
    ):
        if candidate is None:
            continue
        try:
            key = str(candidate).rstrip("\\/").lower()
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


USER_LOCALE_DIR: Path = _system_user_locale_dir()


def local_pack_path(lang: str) -> Path:
    return USER_LOCALE_DIR / f"{lang}.json"


def local_names_path(lang: str) -> Path:
    return USER_LOCALE_DIR / f"names_{lang}.json"


_ALIASES: Dict[str, List[str]] = {
    "ja": ["ja", "ja_JP"],
    "ko": ["ko", "ko_KR"],
    "zh": ["zh", "zh_CN"],
    "zh-tw": ["zh-tw", "zh_TW"],
    "de": ["de", "de_DE"],
    "fr": ["fr", "fr_FR"],
    "es": ["es", "es_ES"],
    "it": ["it", "it_IT"],
    "pl": ["pl", "pl_PL"],
    "ru": ["ru", "ru_RU"],
    "tr": ["tr", "tr_TR"],
    "pt-br": ["pt-br", "pt_BR"],
}


def _candidate_filenames(lang: str) -> List[str]:
    variants = _ALIASES.get(lang, [lang])
    return [f"{v}.json" for v in variants]


def is_pack_local(lang: str) -> bool:
    if not lang or lang == "en":
        return True
    names = _candidate_filenames(lang)
    for d in search_dirs():
        try:
            for name in names:
                p = d / name
                if p.is_file() and p.stat().st_size > 0:
                    return True
        except OSError:
            continue
    return False


def has_names_pack(lang: str) -> bool:
    if lang == "en":
        return True
    target = f"names_{lang}.json"
    for d in search_dirs():
        try:
            if (d / target).is_file():
                return True
        except OSError:
            pass
    for alias in _ALIASES.get(lang, []):
        if alias == lang:
            continue
        for d in search_dirs():
            try:
                if (d / f"names_{alias}.json").is_file():
                    return True
            except OSError:
                pass
    return False


def _atomic_write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=dest.name + ".",
        suffix=".part",
        dir=str(dest.parent),
    )
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except TypeError:
            if tmp.exists():
                tmp.unlink()
        raise


def _http_get(url: str, timeout: float = 20.0) -> bytes:
    # OFFLINE BUILD: downloads removed.
    raise OSError("Online sync removed (offline build)")


def _download_one(
    url: str,
    dest: Path,
    label: str,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> bool:
    try:
        # OFFLINE BUILD: downloads removed — ship language packs in the locale folder.
        log.info("lang_pack: download of %s skipped (offline build)", label)
        return False
        data = b""
        if len(data) < 20:
            log.warning("lang_pack: %s too small (%d bytes), rejecting.", url, len(data))
            return False
        try:
            json.loads(data.decode("utf-8"))
        except Exception as e:
            log.warning("lang_pack: %s not valid JSON: %s", url, e)
            return False
        _atomic_write(dest, data)
        if progress_cb:
            try:
                progress_cb(label, len(data), len(data))
            except Exception:
                pass
        return True
    except urllib.error.HTTPError as e:
        log.info("lang_pack: %s -> HTTP %d", url, e.code)
        return False
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log.info("lang_pack: %s -> %s", url, e)
        return False
    except Exception as e:
        log.warning("lang_pack: %s unexpected error: %s", url, e)
        return False


def download_pack(
    lang: str,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> bool:
    if not lang or lang == "en":
        return True
    if lang not in SUPPORTED_LANGS:
        log.warning("lang_pack: %s is not in SUPPORTED_LANGS", lang)

    USER_LOCALE_DIR.mkdir(parents=True, exist_ok=True)

    ui_ok = _download_one(
        f"{GITHUB_BASE}/{lang}.json",
        local_pack_path(lang),
        label=f"{lang}.json",
        progress_cb=progress_cb,
    )
    names_url = f"{GITHUB_BASE}/names_{lang}.json"
    _download_one(
        names_url,
        local_names_path(lang),
        label=f"names_{lang}.json",
        progress_cb=progress_cb,
    )
    return ui_ok


def list_remote_packs() -> List[str]:
    try:
        data = _http_get(MANIFEST_URL, timeout=10.0)
        parsed = json.loads(data.decode("utf-8"))
        if isinstance(parsed, dict):
            langs = list(parsed.keys())
        elif isinstance(parsed, list):
            langs = list(parsed)
        else:
            langs = []
        if langs:
            return [l for l in langs if l and l != "en"]
    except Exception as e:
        log.info("lang_pack: manifest unavailable (%s), using built-in list.", e)
    return list(SUPPORTED_LANGS)


def get_remote_manifest() -> Dict[str, Dict[str, object]]:
    try:
        data = _http_get(MANIFEST_URL, timeout=10.0)
        parsed = json.loads(data.decode("utf-8"))
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    synthetic: Dict[str, Dict[str, object]] = {}
    for code in SUPPORTED_LANGS:
        synthetic[code] = {
            "native": NATIVE_NAMES.get(code, code),
            "english": ENGLISH_NAMES.get(code, code),
            "sha256": "",
            "size": 0,
            "has_names": True,
        }
    return synthetic


def local_pack_native_name(lang: str) -> str:
    if lang == "en":
        return "English"
    for d in search_dirs():
        for name in _candidate_filenames(lang):
            p = d / name
            if not p.is_file():
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                name_val = data.get("_language_name")
                if name_val:
                    return str(name_val)
            except Exception:
                continue
    return NATIVE_NAMES.get(lang, lang)


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = [
    "GITHUB_BASE",
    "MANIFEST_URL",
    "SUPPORTED_LANGS",
    "NATIVE_NAMES",
    "ENGLISH_NAMES",
    "USER_LOCALE_DIR",
    "search_dirs",
    "local_pack_path",
    "local_names_path",
    "is_pack_local",
    "has_names_pack",
    "download_pack",
    "list_remote_packs",
    "get_remote_manifest",
    "local_pack_native_name",
    "compute_sha256",
]
