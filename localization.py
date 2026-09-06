
import json
import os
import sys

_MY_DIR = os.path.dirname(os.path.abspath(__file__))
_translations: dict[str, str] = {}
_fallback: dict[str, str] = {}
_current_lang: str = "en"
_names_data: dict = {}


def _get_locale_dir() -> str:
    for base in [
        os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else _MY_DIR,
        getattr(sys, '_MEIPASS', _MY_DIR),
        _MY_DIR,
    ]:
        p = os.path.join(base, 'locale')
        if os.path.isdir(p):
            return p
    p = os.path.join(_MY_DIR, 'locale')
    os.makedirs(p, exist_ok=True)
    return p


def _load_lang_file(lang: str) -> dict[str, str]:
    variants = [f"{lang}.json"]
    if '_' not in lang and '-' not in lang and len(lang) == 2:
        LOCALE_MAP = {
            'ja': 'ja_JP', 'ko': 'ko_KR', 'zh': 'zh_CN', 'de': 'de_DE',
            'fr': 'fr_FR', 'es': 'es_ES', 'it': 'it_IT', 'pl': 'pl_PL',
            'ru': 'ru_RU', 'tr': 'tr_TR',
        }
        if lang in LOCALE_MAP:
            variants.append(f"{LOCALE_MAP[lang]}.json")

    search_dirs = []
    if getattr(sys, 'frozen', False):
        search_dirs.append(os.path.join(os.path.dirname(sys.executable), 'locale'))
    search_dirs.append(os.path.join(getattr(sys, '_MEIPASS', _MY_DIR), 'locale'))
    search_dirs.append(os.path.join(_MY_DIR, 'locale'))

    for d in search_dirs:
        for variant in variants:
            path = os.path.join(d, variant)
            if os.path.isfile(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
    return {}


def set_language(lang: str) -> None:
    global _translations, _fallback, _current_lang, _names_data
    _current_lang = lang
    _fallback = _load_lang_file("en")
    if lang == "en":
        _translations = _fallback
    else:
        _translations = _load_lang_file(lang)
    _names_data = _load_names_data(lang)


def get_language() -> str:
    return _current_lang


def tr(key: str, **kwargs) -> str:
    text = _translations.get(key) or _fallback.get(key) or key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text


def get_available_languages() -> list[tuple[str, str]]:
    locale_dir = _get_locale_dir()
    languages = []
    LANG_NAMES = {
        'en': 'English',
        'ja': 'Japanese',
        'ko': 'Korean',
        'zh': 'Chinese (Simplified)',
        'zh-tw': 'Chinese (Traditional)',
        'de': 'German',
        'fr': 'French',
        'es': 'Spanish',
        'pt': 'Portuguese',
        'ru': 'Russian',
        'th': 'Thai',
        'vi': 'Vietnamese',
        'id': 'Indonesian',
        'tr': 'Turkish',
        'ar': 'Arabic',
        'pl': 'Polish',
        'it': 'Italian',
    }
    try:
        for fname in sorted(os.listdir(locale_dir)):
            if fname.endswith('.json') and not fname.startswith('names_'):
                code = fname[:-5]
                try:
                    with open(os.path.join(locale_dir, fname), 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    name = data.get('_language_name', LANG_NAMES.get(code, code))
                except Exception:
                    name = LANG_NAMES.get(code, code)
                languages.append((code, name))
    except OSError:
        pass
    for folder in ['locale', 'language']:
        if getattr(sys, 'frozen', False):
            d = os.path.join(os.path.dirname(sys.executable), folder)
        else:
            d = os.path.join(_MY_DIR, folder)
        if not os.path.isdir(d):
            continue
        for fname in sorted(os.listdir(d)):
            if fname.startswith('names_') and fname.endswith('.json'):
                code = fname[6:-5]
                if not any(c == code for c, _ in languages):
                    try:
                        with open(os.path.join(d, fname), 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        name = data.get('_language_name', LANG_NAMES.get(code, code))
                    except Exception:
                        name = LANG_NAMES.get(code, code)
                    languages.append((code, f"{name} (names only)"))
    if not languages:
        languages.append(('en', 'English'))
    return languages


def export_template(output_path: str = None) -> str:
    if not _fallback:
        set_language("en")
    if output_path is None:
        output_path = os.path.join(_get_locale_dir(), "template.json")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(_fallback, f, indent=2, ensure_ascii=False)
    return output_path


def get_localized_name(db_type: str, key, fallback: str = "") -> str:
    if _current_lang == "en" or not _names_data:
        return fallback

    section = _names_data.get(db_type, {})
    translated = section.get(str(key), "")
    return translated if translated else fallback


def _load_names_data(lang: str) -> dict:
    if lang == "en":
        return {}
    filename = f"names_{lang}.json"
    search_dirs = []
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        search_dirs.append(os.path.join(exe_dir, 'locale'))
        search_dirs.append(os.path.join(exe_dir, 'language'))
    search_dirs.append(os.path.join(getattr(sys, '_MEIPASS', _MY_DIR), 'locale'))
    search_dirs.append(os.path.join(getattr(sys, '_MEIPASS', _MY_DIR), 'language'))
    search_dirs.append(os.path.join(_MY_DIR, 'locale'))
    search_dirs.append(os.path.join(_MY_DIR, 'language'))

    for d in search_dirs:
        path = os.path.join(d, filename)
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


set_language("en")
