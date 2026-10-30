"""Use the vendored client archive dependency in source and frozen builds."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path


def load_backend():
    source = str(Path(__file__).resolve().parents[1] / "CrimsonGameMods")
    added = not getattr(sys, "frozen", False) and source not in sys.path
    if added:
        sys.path.insert(1, source)
    try:
        return importlib.import_module("crimson_rs")
    finally:
        if added:
            sys.path.remove(source)
