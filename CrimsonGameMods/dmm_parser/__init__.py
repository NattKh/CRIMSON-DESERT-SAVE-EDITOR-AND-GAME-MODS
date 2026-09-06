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
    from table_layout import map_entry as _map_static_entry
except ImportError:
    import os
    import sys
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from table_layout import map_entry as _map_static_entry

try:
    _native_extract_file = extract_file  # type: ignore[name-defined]
except NameError:
    _native_extract_file = None

try:
    _native_extract_file_from_paz = extract_file_from_paz  # type: ignore[name-defined]
except NameError:
    _native_extract_file_from_paz = None

try:
    _NativePackGroupBuilder = PackGroupBuilder  # type: ignore[name-defined]
except NameError:
    _NativePackGroupBuilder = None


if _native_extract_file is not None:
    def extract_file(game_dir, group_name, dir_path, file_name):  # type: ignore[no-redef]
        mapped_dir, mapped_name = _map_static_entry(dir_path, file_name)
        return _native_extract_file(game_dir, group_name, mapped_dir, mapped_name)

if _native_extract_file_from_paz is not None:
    def extract_file_from_paz(paz_path, internal_path):  # type: ignore[no-redef]
        path = str(internal_path).replace("\\", "/")
        if "/" in path:
            directory, name = path.rsplit("/", 1)
            directory, name = _map_static_entry(directory, name)
            path = f"{directory}/{name}"
        return _native_extract_file_from_paz(paz_path, path)


if _NativePackGroupBuilder is not None:
    class PackGroupBuilder:  # type: ignore[no-redef]
        """PackGroupBuilder that always writes 2.01 staticinfo archive names."""

        def __init__(self, *args, **kwargs):
            self._inner = _NativePackGroupBuilder(*args, **kwargs)

        def add_file(self, dir_path, file_name, data):
            mapped_dir, mapped_name = _map_static_entry(dir_path, file_name)
            return self._inner.add_file(mapped_dir, mapped_name, data)

        def add_file_from_path(self, dir_path, file_name, file_path):
            mapped_dir, mapped_name = _map_static_entry(dir_path, file_name)
            return self._inner.add_file_from_path(mapped_dir, mapped_name, file_path)

        def finish(self):
            return self._inner.finish()

        def __getattr__(self, name):
            return getattr(self._inner, name)
