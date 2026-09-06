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

## Note on native extensions

The tools use `dmm_parser` (Rust-based parser). Pre-built `.pyd` (Windows) and `.abi3.so` (Linux) binaries ship with the repo in `CrimsonGameMods/dmm_parser/`. You do not need Rust installed to build.

## Notes

- The save editor shares parser/backend components with `CrimsonGameMods`.
- The Linux build expects the native backend artifacts to be present in the sibling `CrimsonGameMods` tree.
