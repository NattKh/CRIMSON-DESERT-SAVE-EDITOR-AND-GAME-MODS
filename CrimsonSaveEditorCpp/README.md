# Crimson Desert Save Editor (C++)

Source for the C++ rewrite shipped as `cpp-v1.0.0` (ImGui / DirectX 11).

This folder is **source only**: editor classes, the PARC/save engine, and the vendored headers those files include. No local save paths, no machine config, no item/icon data dumps.

## Layout

- `src/` — editor and engine sources
- `third_party/lz4` — compression used by save write-back
- `third_party/imgui` — Win32 + DX11 backends only
- `third_party/nlohmann` — JSON

## Build (Windows, MSVC)

```bat
cmake -S . -B build -G "Visual Studio 17 2022" -A x64
cmake --build build --config Release
```

Useful targets:

- `inventory_editor` — desktop ImGui editor
- `parc_parser` — native DLL used by the Python standalone
- `parc_engine_cli` / `parc_dump` — command-line tools

Optional Qt shell (`main.cpp` / `MainWindow.*`) is off unless you pass `-DBUILD_GUI=ON` and have Qt 6.5+.

The editor looks for a `data/` folder next to the exe at runtime (item names, quest maps, icons). Those files are not part of this source drop; copy them from a `cpp-v1.0.0` release zip if you want a runnable UI.

## License notes

ImGui, nlohmann/json, LZ4, and pugixml keep their upstream licenses. The save container key in `save_parser_cpp.cpp` is the same derived key already published in the Python editor.
