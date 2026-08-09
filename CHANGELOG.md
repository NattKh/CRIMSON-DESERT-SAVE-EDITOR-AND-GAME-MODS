# Changelog

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
