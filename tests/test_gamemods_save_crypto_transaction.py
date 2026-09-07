from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "CrimsonGameMods" / "save_crypto.py"


def _load_save_crypto():
    module_name = "_test_gamemods_save_crypto"
    previous_models = sys.modules.pop("models", None)
    sys.path.insert(0, str(MODULE_PATH.parent))
    try:
        spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)
        sys.modules.pop("models", None)
        if previous_models is not None:
            sys.modules["models"] = previous_models


def _header(module) -> bytes:
    raw = bytearray(module.HEADER_SIZE)
    raw[:4] = b"SAVE"
    struct.pack_into("<H", raw, module.VERSION_OFFSET, 2)
    return bytes(raw)


def test_fsync_failure_preserves_existing_destination(tmp_path, monkeypatch):
    module = _load_save_crypto()
    destination = tmp_path / "save.save"
    original = b"ORIGINAL SAVE BYTES"
    destination.write_bytes(original)

    def fail_fsync(_fd):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(module.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="simulated disk failure"):
        module.write_save_file(str(destination), b"edited blob", _header(module))

    assert destination.read_bytes() == original
    assert not list(tmp_path.glob(".save.save.*.tmp"))


def test_temp_validation_failure_preserves_existing_destination(tmp_path, monkeypatch):
    module = _load_save_crypto()
    destination = tmp_path / "save.save"
    original = b"ORIGINAL SAVE BYTES"
    destination.write_bytes(original)

    def reject_temp(_path):
        raise ValueError("temporary save validation failed")

    monkeypatch.setattr(module, "load_save_file", reject_temp)

    with pytest.raises(ValueError, match="temporary save validation failed"):
        module.write_save_file(str(destination), b"edited blob", _header(module))

    assert destination.read_bytes() == original
    assert not list(tmp_path.glob(".save.save.*.tmp"))
