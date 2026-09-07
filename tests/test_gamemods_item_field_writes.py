from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "CrimsonGameMods" / "item_scanner.py"


def _load_item_scanner():
    module_name = "_test_gamemods_item_scanner"
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


@pytest.mark.parametrize(
    "field_name,function_name,new_value,attribute,target",
    (
        ("_endurance", "apply_endurance_edit", 0x1234, "endurance", 70),
        ("_sharpness", "apply_sharpness_edit", 0x5678, "sharpness", 80),
    ),
)
def test_parsed_field_offset_is_the_only_bytes_modified(
    field_name, function_name, new_value, attribute, target
):
    module = _load_item_scanner()
    data = bytearray(range(100))
    original = bytes(data)
    item = module.SaveItem(
        offset=10,
        field_offsets={field_name: target},
        parc_parsed=True,
    )

    old = getattr(module, function_name)(data, item, new_value)

    assert old == original[target:target + 2]
    expected = bytearray(original)
    struct.pack_into("<H", expected, target, new_value)
    assert data == expected
    assert getattr(item, attribute) == new_value
