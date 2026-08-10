# Changelog

## Unreleased - Pack Storage and Insertion Fixes

### Improved

- Downloaded community pack JSON files are kept in the editor's local `packs` folder beside the executable, so the same editor installation can immediately list and inject them.
- The Add Pack confirmation now identifies the selected destination storage before any change is made.

### Fixed

- Fixed Add Pack failing after a successful insertion because its result message referenced an undefined value.
- Fixed batch pack insertion silently falling back to another inventory container when the selected storage could not be found.
- Pack insertion now requires and preserves the user's selected storage container.
- Fixed multi-item pack insertion corrupting an inventory-list wrapper when its internal list header is offset from the field start.

### Privacy

- No save files, local editor configuration, downloaded packs, test data, or build artifacts are included in Git commits.

## v1.0.9 - Companion, Inventory and Item Creation Update

### Added

- Dedicated Pets workspace with confirmed pet detection, editing for health, vigor, custom name, full available stats, and equipped pet items.
- Pet equipment controls for stack, upgrade, item/outfit editing, and direct access to companion sockets.
- Separate Mercenaries, Pets, and Mounts views with catalog-backed names and correct type classification.
- Create New Item action in Inventory. It creates a new record in the selected save storage instead of requiring a donor item.
- Dynamic inventory storage navigation: every detected InventoryKey is shown separately with its own item count.

### Improved

- Inventory now shows the real storage key for each item and no longer presents unrelated containers as one inventory.
- The Add New Item workflow requires a destination storage and validates that the created item is present in that exact storage before applying it.
- Companion editor now exposes all present, editable scalar stats through the More stats dialog.

### Fixed

- Fixed pets and mounts being mixed with NPCs; unrecognized characters are no longer presented as pets.
- Fixed companion catalog packaging in the standalone executable.
- Fixed pet-equipment scanning and item ownership detection.
- Fixed the hidden Add Item control and removed the unsafe tree-serializer insertion path from that workflow.
- Prevented new items from silently falling back to a different storage container.

### Privacy

- No save files, local editor configuration, test data, build folders, or loose workspace files are included in this release.

## v1.0.8 — Editor UX, Save Loading & Companion Update

### Added

- Dedicated **Mercenaries**, **Pets**, and **Mounts** workspaces in the sidebar.
- Automatic companion scan when opening any of those workspaces.
- Pet editing controls for custom name, health, and vigor.
- Mercenary controls for custom name, health, and vigor.
- Home-page save discovery to make selecting a local save easier.
- A compact one-line equipment action bar for upgrade, copy count, and duplication.

### Improved

- Rebuilt sidebar navigation around clear task groups: Start, Save Editing, Companions, Content, World, and Tools.
- Added sidebar scrolling for smaller displays and removed duplicated Settings navigation.
- Simplified Inventory, Equipment, Sockets, Repurchase, Quest Editor, Factions, and Mounts layouts to reduce visual clutter.
- Kept inventory filters in the sidebar instead of duplicating them as content tabs.
- Mounts, pets, and mercenaries are no longer presented as one mixed list.

### Fixed

- Fixed save loading errors caused by missing optional `mod_loader` imports and deleted Qt widgets.
- Fixed the companion reader being blocked by an unnecessary crypto dependency.
- Fixed companion and mount tables loading empty for valid saves.
- Fixed item scanning so PARC saves correctly expose all detected inventory records.
- Corrected socket field offsets and socket application behavior.
- Removed the startup splash path that could leave the editor stuck while building tabs.
- Improved standalone build packaging for PySide6.

### Privacy

- Local saves, test-save copies, build directories, and all `*.save` files are ignored by Git and are not part of this release.
