# Installed-client catalog regression tests

Run from the repository root:

```powershell
python -m pip install "pytest>=8"
python -m pytest tests -q
```

The 15 cases use synthetic archive records and native-parser results. They do
not require an installed game, proprietary data, GUI dependencies or a native
parser binary. They exercise the actual catalog readers and JSON/SQLite writes.

Coverage includes both apps' 2.04 and legacy item records, current-client names
and stack limits, persistence, mismatched parser/index keys, missing localization,
truncated records, cached-catalog failure handling, custom display names and
local-only entries.

## Current-client integration checks

Use a disposable catalog directory when testing against a local installation.
Extract the current ItemInfo index/body and run each app's
`ItemNameDB.sync_from_local_game`. Reopen the resulting JSON/SQLite catalog and
compare all indexed keys, internal names and stack limits with the extracted
client records. Confirm that failed scans do not report cached items as freshly
scanned data. Keep private saves and extracted client tables out of the test suite.

The shared reader in `CrimsonGameMods/crimson_rs/catalog.py` delegates reordered
2.04 ItemInfo decoding to the native parser and validates the returned count and
keys against the index before either catalog changes. Legacy metadata retains
bounded prefix reads. Current English labels come from
`gamedata/stringtable/binary__/eng/item.paloc`; missing labels retain the apps'
existing fallback behavior and localization extraction failures are logged.

Build both Windows applications with the specs described in
`BUILD_FROM_SOURCE.md`. Verify that each executable includes its own app's
models/item database, the shared catalog reader and the native parser pinned in
`CrimsonGameMods/dmm_parser/UPSTREAM.json`.

The bundled Windows dependency supports game 2.04.00. The existing Linux binary
is unchanged and must be rebuilt from the pinned revision before claiming
current-client support on Linux. Passing these catalog tests does not establish
mod deployment or in-game save editing compatibility.
