# Crimson Save Editor

A PySide6 desktop tool for editing **Crimson Desert** save files. It handles inventory, equipment, quests, knowledge, abyss gates, dyes, and related save data.

## Install

1. Download the latest release build for your platform.
2. Place the app in a folder where you want it to keep config and backups.
3. Run it and let it auto-detect your save location, or point it at your save manually.

## Build from source

## Windows

```bat
pip install PySide6 lz4 cryptography Pillow pyinstaller

:: Build Save Editor
cd ..\CrimsonSaveEditor
python -m PyInstaller CrimsonSaveEditor.spec --noconfirm
:: Output: CrimsonSaveEditor\dist\CrimsonSaveEditor.exe
```

## Linux / SteamOS

```bash
sudo apt install python3 python3-pip git   # Debian/Ubuntu/SteamOS
pip install PySide6 lz4 cryptography Pillow pyinstaller

git clone https://github.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS.git
cd CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS/CrimsonSaveEditor

python -m PyInstaller CrimsonSaveEditor.spec --noconfirm
```

## Native C++ backend

This Python GUI uses `parc_parser.dll` (see `native_backend.py`) for save validation and validated writes. The DLL source is in `../CrimsonSaveEditorCpp` (`parc_dll.cpp`, `parc_engine.cpp`, `parc_engine_cli.cpp`).

Ship `parc_parser.dll` next to the executable. Saving is disabled if the DLL is missing.

## Notes

- Complex mutations (template swap, insertion, repurchase) may still run in Python; the C++ backend validates and writes the SAVE container.
- `CHANGELOG.md` describes the standalone 2.1.0 hybrid testing build.
