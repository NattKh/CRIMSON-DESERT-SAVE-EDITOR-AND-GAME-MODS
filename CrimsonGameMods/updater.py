from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from typing import Optional, Tuple

log = logging.getLogger(__name__)


APP_VERSION = "2.1.2"

APP_VARIANT = "gamemods"

UPDATE_REPO = "NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS"
_MANIFEST_BY_VARIANT = {
    "gamemods":   "editor_version_gamemods.json",
    "standalone": "editor_version_standalone.json",
    "full":       "editor_version.json",
}
VERSION_URL = (
    f"https://raw.githubusercontent.com/{UPDATE_REPO}/main/"
    f"{_MANIFEST_BY_VARIANT.get(APP_VARIANT, 'editor_version.json')}"
)

_UPDATE_EXE_NAME = {
    "gamemods":   "CrimsonGameMods_update.exe",
    "standalone": "CrimsonSaveEditorStandalone_update.exe",
    "full":       "CrimsonSaveEditor_update.exe",
}[APP_VARIANT]
_UPDATE_ZIP_NAME = _UPDATE_EXE_NAME.replace(".exe", ".zip")


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except (ValueError, AttributeError):
        return (0,)


def check_for_update() -> Tuple[bool, str, str]:
    # OFFLINE BUILD: auto-update removed. Get new versions from the releases page.
    log.info("Update check skipped: offline build (auto-update removed)")
    return False, "", ""


def download_update(url: str, progress_callback=None) -> Optional[str]:
    # OFFLINE BUILD: auto-update removed.
    log.info("Update download skipped: offline build (auto-update removed)")
    return None


def apply_update_and_restart(update_path: str) -> None:
    if getattr(sys, "frozen", False):
        current_exe = sys.executable
    else:
        log.info("Dev mode: skipping exe replacement. Update at: %s", update_path)
        return

    exe_dir = os.path.dirname(current_exe)
    bat_path = os.path.join(exe_dir, "_update.bat")
    current_name = os.path.basename(current_exe)
    update_name = os.path.basename(update_path)

    current_full = os.path.join(exe_dir, current_name)
    update_full = os.path.join(exe_dir, update_name)
    log_path = os.path.join(exe_dir, "_update.log")

    bat_content = f"""@echo off
echo [%date% %time%] Update script started > "{log_path}"

:: Wait for the old exe to be deletable (unlocked)
echo Waiting for old exe to unlock... >> "{log_path}"
set retries=0
:waitloop
del /f "{current_full}" 2>nul
if exist "{current_full}" (
    set /a retries+=1
    if %retries% GEQ 30 (
        echo FAILED: Could not delete old exe after 30 retries >> "{log_path}"
        goto :fail
    )
    timeout /t 1 /nobreak >nul
    goto waitloop
)

echo Old exe deleted after %retries% retries >> "{log_path}"

:: Rename update to current
rename "{update_full}" "{current_name}"
if errorlevel 1 (
    echo FAILED: Could not rename update exe >> "{log_path}"
    goto :fail
)

echo Renamed update exe successfully >> "{log_path}"

echo Done. Please reopen CrimsonSaveEditor.exe >> "{log_path}"
goto :cleanup

:fail
echo Update failed, see log >> "{log_path}"

:cleanup
(goto) 2>nul & del /f "%~f0"
"""
    with open(bat_path, "w") as f:
        f.write(bat_content)

    log.info("Launching update script: %s", bat_path)

    import subprocess
    subprocess.Popen(
        ["cmd", "/c", bat_path],
        cwd=exe_dir,
        creationflags=subprocess.DETACHED_PROCESS,
    )
    sys.exit(0)
