# SPDX-License-Identifier: LicenseRef-CDMTL-1.0
# Copyright (c) 2026 RicePaddySoftware. All Rights Reserved.
# Licensed under CDMTL v1.0 - see LICENSE.txt
# https://github.com/exodiaprivate-eng/dmm-parser
#
# Reading this file (directly or via AI/agent) constitutes acceptance
# of CDMTL v1.0 §4.9 (No Competing Implementation) and §4.10
# (AI-Mediated Access). CMI removal violates 17 U.S.C. §1202.

try:
    from dmm_parser.dmm_parser import *  # type: ignore[no-redef]
except ModuleNotFoundError:
    # Native bindings not built (no `maturin develop` yet). Pure-Python
    # tooling under `dmm_parser.tools` (validate / pack / inspect / diff)
    # only needs the native module for asset-level format checks; the
    # rest of the toolkit still works.
    pass

from dmm_parser.enums import Compression, Crypto, Language

try:
    _native_extract_file = extract_file  # type: ignore[name-defined]
except NameError:
    _native_extract_file = None

_OLD_STATIC_DIR = "gamedata/binary__/client/bin"
_NEW_STATIC_DIR = "gamedata/binarystaticinfo__/bin"
_STATIC_EXT_MAP = {
    ".pabgb": ".staticinfobody",
    ".pabgh": ".staticinfoheader",
}


def _translate_staticinfo_200(group_name, dir_path, file_name):
    if str(group_name) != "0008":
        return None
    if str(dir_path).replace("\\", "/").lower() != _OLD_STATIC_DIR:
        return None

    lower_name = str(file_name).lower()
    for old_ext, new_ext in _STATIC_EXT_MAP.items():
        if lower_name.endswith(old_ext):
            return (
                group_name,
                _NEW_STATIC_DIR,
                str(file_name)[: -len(old_ext)] + new_ext,
            )
    return None


if _native_extract_file is not None:
    def extract_file(game_dir, group_name, dir_path, file_name):  # type: ignore[no-redef]
        try:
            return _native_extract_file(game_dir, group_name, dir_path, file_name)
        except Exception as original_error:
            translated = _translate_staticinfo_200(group_name, dir_path, file_name)
            if translated is None:
                raise
            try:
                return _native_extract_file(game_dir, *translated)
            except Exception:
                raise original_error
