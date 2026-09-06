# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
Path("quest_packs").mkdir(exist_ok=True)

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[('dmm_parser/dmm_parser.pyd', 'dmm_parser'), ('dmm_parser/dmm_parser.abi3.so', 'dmm_parser')],
    datas=[('crimson_data.db.gz', '.'), ('data', 'data'), ('vfx_equip_attachments.json', '.'), ('parc_parser.dll', '.'), ('locale', 'locale'), ('knowledge_packs', 'knowledge_packs'), ('quest_packs', 'quest_packs'), ('dropset_packs', 'dropset_packs'), ('localizationstring_eng_items.tsv', '.'), ('pabgb_parser_local.py', '.'), ('crimson_rs', 'crimson_rs'), ('dmm_parser', 'dmm_parser'), ('game_baselines', 'game_baselines')],
    hiddenimports=['table_layout', 'client_names', 'lz4', 'lz4.block', 'iteminfo_parser', 'cryptography', 'cryptography.hazmat.primitives.ciphers', 'cryptography.hazmat.primitives.ciphers.algorithms', 'parc_inserter3', 'storeinfo_parser', 'gamedata_editor', 'pabgb_field_parsers', 'universal_pabgb_parser', 'factionnode_operator_parser', 'fieldinfo_parser', 'vehicleinfo_parser', 'regioninfo_parser', 'armor_catalog', 'character_mesh_swap', 'gimmickinfo_parser', 'pipeline_report', 'characterinfo_full_parser', 'data_db', 'gui.tabs.buffs_v319', 'gui.tabs.field_edit', 'gui.tabs.skill_tree', 'gui.tabs.patches', 'gui.tabs.pas_editor', 'gui.tabs.reserveslot', 'gui.tabs.iteminfo_inspector', 'gui.tabs.mod_loader', 'gui.tabs.dmm_webview', 'gui.item_creator_dialog', 'gui.add_to_save_dialog', 'gui.iteminfo_index', 'gui.dialogs', 'gui.theme', 'gui.utils', 'gui_i18n', 'i18n', 'lang_pack_downloader', 'gui.language_picker', 'item_creator', 'skilltreeinfo_parser', 'skillinfo_parser', 'mercenaryinfo_parser', 'dropset_editor', 'equipslotinfo_parser', 'gui.tabs.stacker', 'gui.tabs.mercpets', 'gui.tabs.world', 'gui.tabs.bagspace', 'gui.tabs.items', 'gui.tabs.load_manager', 'gui.tabs.quest_mods', 'icon_cache', 'item_db', 'item_packs', 'item_scanner', 'models', 'paz_patcher', 'localization', 'overlay_coordinator', 'shared_state', 'reserveslot_parser', 'store_editor', 'wantedinfo_parser', 'terrain_spawn_parser'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Read version from updater.py so splash always matches
import re as _re
with open('updater.py', 'r') as _f:
    _m = _re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)', _f.read())
_app_ver = _m.group(1) if _m else '?'

splash = Splash(
    'splash.png',
    binaries=a.binaries,
    datas=a.datas,
    text_pos=(24, 195),
    text_size=10,
    text_color='#F0F0F5',
    text_default=f'v{_app_ver} — Initializing...',
    always_on_top=True,
)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    splash,
    splash.binaries,
    [],
    name='CrimsonGameMods',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon='app_icon.ico',
    codesign_identity=None,
    entitlements_file=None,
)
