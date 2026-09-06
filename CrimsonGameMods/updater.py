from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from typing import Optional, Tuple
from urllib.request import urlopen, Request
from urllib.error import URLError

log = logging.getLogger(__name__)


APP_VERSION = "2.1.7"

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
    try:
        req = Request(VERSION_URL, headers={"User-Agent": "CrimsonSaveEditor"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        remote_version = data.get("version", "0")
        download_url = data.get("url", "")

        if not download_url:
            return False, remote_version, ""

        local = _version_tuple(APP_VERSION)
        remote = _version_tuple(remote_version)

        if remote > local:
            return True, remote_version, download_url
        else:
            return False, remote_version, download_url

    except (URLError, json.JSONDecodeError, OSError, KeyError) as e:
        log.warning("Update check failed: %s", e)
        return False, "", ""


def download_update(url: str, progress_callback=None) -> Optional[str]:
    try:
        if getattr(sys, "frozen", False):
            current_exe = sys.executable
        else:
            current_exe = os.path.abspath(sys.argv[0])

        exe_dir = os.path.dirname(current_exe)
        is_zip = url.lower().endswith(".zip")
        download_path = os.path.join(
            exe_dir,
            _UPDATE_ZIP_NAME if is_zip else _UPDATE_EXE_NAME,
        )
        update_path = os.path.join(exe_dir, _UPDATE_EXE_NAME)

        req = Request(url, headers={
            "User-Agent": "CrimsonSaveEditor/3.0",
            "Accept": "application/octet-stream",
        })
        with urlopen(req, timeout=600) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 256 * 1024

            with open(download_path, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total)

        with open(download_path, "rb") as f:
            magic = f.read(2)

        if magic == b"PK":
            import zipfile
            with zipfile.ZipFile(download_path, "r") as zf:
                exe_names = [n for n in zf.namelist() if n.lower().endswith(".exe")]
                if not exe_names:
                    os.remove(download_path)
                    log.error("Zip contains no .exe files: %s", zf.namelist())
                    return None
                target = next(
                    (n for n in exe_names if "crimsonsaveeditor" in n.lower()),
                    exe_names[0],
                )
                with zf.open(target) as src, open(update_path, "wb") as dst:
                    import shutil
                    shutil.copyfileobj(src, dst)
            os.remove(download_path)
            log.info("Extracted %s from zip", target)
        elif magic == b"MZ":
            if download_path != update_path:
                if os.path.exists(update_path):
                    os.remove(update_path)
                os.rename(download_path, update_path)
        else:
            os.remove(download_path)
            log.error("Downloaded file is not a valid exe or zip (magic=%s)", magic.hex())
            return None

        with open(update_path, "rb") as f:
            if f.read(2) != b"MZ":
                os.remove(update_path)
                log.error("Extracted file is not a valid executable")
                return None

        log.info("Downloaded update to %s (%d bytes)", update_path, downloaded)
        return update_path

    except Exception as e:
        log.error("Download failed: %s", e)
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
