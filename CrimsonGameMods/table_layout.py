"""2.01.00 static-table paths. The game no longer reads .pabgb/.pabgh.

Every extract and overlay write goes through map_entry() so tabs cannot
keep packing gamedata/binary__/client/bin/*.pabgb after a game rename.
"""

LEGACY_DIR = "gamedata/binary__/client/bin"
STATICINFO_DIR = "gamedata/binarystaticinfo__/bin"
INTERNAL_DIR = STATICINFO_DIR

_BODY_OLD = ".pabgb"
_HEADER_OLD = ".pabgh"
BODY_EXT = ".staticinfobody"
HEADER_EXT = ".staticinfoheader"


def _norm_dir(dir_path: str) -> str:
    return (dir_path or "").replace("\\", "/").strip("/")


def archive_name(file_name: str) -> str:
    """Contract name → archive name the 2.01 engine looks up."""
    lower = (file_name or "").lower()
    if lower.endswith(_BODY_OLD):
        return file_name[: -len(_BODY_OLD)] + BODY_EXT
    if lower.endswith(_HEADER_OLD):
        return file_name[: -len(_HEADER_OLD)] + HEADER_EXT
    return file_name


def is_static_table_name(file_name: str) -> bool:
    lower = (file_name or "").lower()
    return lower.endswith((_BODY_OLD, _HEADER_OLD, BODY_EXT, HEADER_EXT))


def map_entry(dir_path: str, file_name: str) -> tuple[str, str]:
    """Map a pack/extract (dir, name) to the 2.01 archive location."""
    name = archive_name(file_name)
    directory = _norm_dir(dir_path)
    if is_static_table_name(file_name) or is_static_table_name(name):
        if directory.lower() in ("", LEGACY_DIR.lower(), STATICINFO_DIR.lower()):
            directory = STATICINFO_DIR
        elif directory.lower() == LEGACY_DIR.lower():
            directory = STATICINFO_DIR
    return directory, name


def table_file(stem: str, header: bool = False) -> str:
    stem = (stem or "").strip()
    for ext in (_BODY_OLD, _HEADER_OLD, BODY_EXT, HEADER_EXT):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    return stem + (HEADER_EXT if header else BODY_EXT)


def is_same_table_file(actual_name: str, wanted: str) -> bool:
    """PAMT / folder name equals wanted stem under either spelling."""
    if not actual_name or not wanted:
        return False
    return archive_name(actual_name).lower() == archive_name(wanted).lower()


def is_iteminfo_body(name: str) -> bool:
    return is_same_table_file(name, "iteminfo.staticinfobody")


def is_iteminfo_header(name: str) -> bool:
    return is_same_table_file(name, "iteminfo.staticinfoheader")
