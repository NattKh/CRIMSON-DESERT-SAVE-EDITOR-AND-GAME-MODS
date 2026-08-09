#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import os
import shutil
import site
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECTS = {
    "gamemods": {
        "root": ROOT / "CrimsonGameMods",
        "targets": {
            "cli": {"entry": "cli.py", "name": "CrimsonCLI", "out": "build-nuitka-cli", "spec": "CrimsonCLI.spec"},
            "full": {"entry": "main.py", "name": "CrimsonGameMods", "out": "build-nuitka-full", "spec": "CrimsonGameMods.spec"},
            "lite": {"entry": "simple_launcher.py", "name": "CrimsonGameModsSimple", "out": "build-nuitka-lite", "spec": "CrimsonGameModsSimple.spec"},
        },
    },
    "saveeditor": {
        "root": ROOT / "CrimsonSaveEditor",
        "targets": {
            "full": {"entry": "main.py", "name": "CrimsonSaveEditor", "out": "build-output", "spec": "CrimsonSaveEditor.spec"},
        },
    },
}


def find_qt_libs() -> tuple[str, str]:
    """Find PySide6 libraries for nuitka compilation."""
    paths = []
    try:
        paths.extend(site.getsitepackages())
    except AttributeError:
        pass
    try:
        paths.append(site.getusersitepackages())
    except AttributeError:
        pass

    # Platform-specific extension
    if sys.platform == "win32":
        ext_pattern = "*.pyd"
    else:
        ext_pattern = "libpyside6*.so*"

    for base in paths:
        if not base:
            continue
        pyside = sorted(glob.glob(os.path.join(base, "PySide6", ext_pattern)))
        if pyside:
            return pyside[0], ""
    raise SystemExit("Unable to locate PySide6 libraries")


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", choices=PROJECTS.keys(), required=True)
    parser.add_argument("--target", default="full")
    parser.add_argument("--backend", choices=["nuitka", "pyinstaller"], default="pyinstaller")
    args = parser.parse_args()

    project = PROJECTS[args.project]
    if args.target not in project["targets"]:
        raise SystemExit(f"Invalid target {args.target!r} for project {args.project}")

    target = project["targets"][args.target]
    root = project["root"]
    pyside_lib, shiboken_lib = find_qt_libs()

    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)

    if args.backend == "pyinstaller":
        spec = root / target["spec"]
        dist = root / "dist"
        shutil.rmtree(dist, ignore_errors=True)
        run([sys.executable, "-m", "PyInstaller", str(spec), "--noconfirm"], cwd=root)
        return 0

    build_dir = root / target["out"]
    shutil.rmtree(build_dir, ignore_errors=True)

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        "--enable-plugin=pyside6",
        "--output-dir",
        str(build_dir),
        "--output-filename",
        target["name"],
    ]

    if args.project == "gamemods":
        cmd += ["--include-package=crimson_rs"]

    common_datas = [
        ("data", "data"),
        ("locale", "locale"),
        ("knowledge_packs", "knowledge_packs"),
        ("dropset_packs", "dropset_packs"),
    ]
    for src, dst in common_datas:
        candidate = root / src
        if candidate.exists():
            cmd += ["--include-data-dir", f"{candidate}={dst}"]

    if args.project == "gamemods":
        quest_packs = ROOT / "quest_packs"
        if quest_packs.exists():
            cmd += ["--include-data-dir", f"{quest_packs}=quest_packs"]
        for file_name in ["crimson_data.db.gz", "vfx_equip_attachments.json", "localizationstring_eng_items.tsv"]:
            candidate = root / file_name
            if candidate.exists():
                cmd += ["--include-data-file", f"{candidate}={file_name}"]
        backend_files = {
            "Linux": [root / "dmm_parser" / "dmm_parser.abi3.so"],
            "Windows": [root / "dmm_parser" / "dmm_parser.pyd"],
        }
        platform_files = backend_files.get(os.name == "nt" and "Windows" or "Linux", [])
        for candidate in platform_files:
            if candidate.exists():
                cmd += ["--include-data-file", f"{candidate}=dmm_parser/{candidate.name}"]
    else:
        for file_name in [
            "parc_parser.dll",
            "item_names.json",
            "store_names.json",
            "item_templates.json",
            "master_templates.json",
            "item_limits.json",
            "item_category_map.json",
            "max_enchant_map.json",
            "waypoint_templates_community.json",
            "abyss_gimmick_templates.json",
            "knowledge_keys_all.json",
            "community_knowledge_keys.json",
            "quest_names.json",
            "quest_database.json",
            "mission_names.json",
            "quest_stage_map.json",
            "stage_names.json",
            "gimmick_respawn_timers.json",
            "quest_chains.json",
            "dye_slot_counts.json",
            "buff_skill_descriptions.json",
            "game_map.json",
            "localizationstring_eng_items.tsv",
            "editor_version_standalone.json",
        ]:
            candidate = root / file_name
            if candidate.exists():
                cmd += ["--include-data-file", f"{candidate}={file_name}"]

    cmd += ["--include-data-file", f"{pyside_lib}=PySide6/{Path(pyside_lib).name}"]
    cmd += ["--include-data-file", f"{shiboken_lib}=shiboken6/{Path(shiboken_lib).name}"]

    entry = root / target["entry"]
    if not entry.exists():
        raise SystemExit(f"Missing entry point: {entry}")
    cmd.append(str(entry))

    run(cmd, cwd=root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
