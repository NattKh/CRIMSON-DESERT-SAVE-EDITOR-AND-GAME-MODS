# Build from Source

## Prerequisites

- **Python 3.12+** from [python.org](https://www.python.org/downloads/)
  - **Do NOT use the Microsoft Store version** — it restricts site-packages and breaks PyInstaller
  - Check "Add Python to PATH" during install
- **pip packages**: `PySide6 lz4 cryptography Pillow pyinstaller`

## Windows

```bat
pip install PySide6 lz4 cryptography Pillow pyinstaller

:: Build Game Mods
cd CrimsonGameMods
python -m PyInstaller CrimsonGameMods.spec --noconfirm
:: Output: CrimsonGameMods\dist\CrimsonGameMods.exe

:: Build Save Editor
cd ..\CrimsonSaveEditor
python -m PyInstaller CrimsonSaveEditor.spec --noconfirm
:: Output: CrimsonSaveEditor\dist\CrimsonSaveEditorStandalone.exe
```

Or use `build.bat` inside `CrimsonGameMods/` — it clears caches and runs PyInstaller for you.

## Linux / SteamOS

```bash
sudo apt install python3 python3-pip git   # Debian/Ubuntu/SteamOS
pip install PySide6 lz4 cryptography Pillow pyinstaller

git clone https://github.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS.git
cd CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS/CrimsonGameMods

python -m PyInstaller CrimsonGameMods.spec --noconfirm
```

## Note on native extensions

The tools use `dmm_parser` (Rust-based parser). Pre-built `.pyd` (Windows) and `.abi3.so` (Linux) binaries ship with the repo in `CrimsonGameMods/dmm_parser/`. You do not need Rust installed to build.

The Windows client-data parser is pinned to the upstream revision recorded in
`CrimsonGameMods/dmm_parser/UPSTREAM.json`, with support for game 2.04.00 archives
and ItemInfo records. Both Save Editor specs include the sibling Game Mods
package path and bundle this same native parser. Keep the two source folders
together when building the standalone editor; installing an older `crimson_rs`
package does not update the vendored dependency.

The existing Linux `.abi3.so` has not been updated or verified for game 2.04.
Before building a Linux/SteamOS release with current-client scanning, rebuild the
native extension from that pinned upstream revision and replace the bundled
Linux binary. The Windows dependency update does not establish Linux support.

## Troubleshooting

| Error | Fix |
|-------|-----|
| "Unable to locate PySide6/shiboken6 shared libraries" | You're using MS Store Python. Uninstall it and install from [python.org](https://www.python.org/downloads/) |
| `pip` not found | Re-run Python installer, check "Add Python to PATH" |
| `ModuleNotFoundError: No module named 'PySide6'` | Run `pip install PySide6` (make sure you're using the right pip — `python -m pip install PySide6`) |
