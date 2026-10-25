/**
 * parc_xml.h — Lossless XML export/import for PARC save trees.
 *
 * Export writes a self-contained XML: every scalar as an editable value,
 * structure (masks, type names, sentinels, list headers) as attributes,
 * undecoded gap/trailing bytes as hex blobs with their source offsets.
 * Offsets, POs, trailing sizes, list counts and the TOC are NOT exported —
 * the serializer recomputes them all on import.
 *
 * Type and field references are resolved BY NAME against the importing
 * save's schema (per-save type indices differ between saves).
 */
#pragma once
#include "parc_engine.h"
#include <string>
#include <vector>

namespace ParcXml {

// Export the whole save (node_path empty) or a single subtree
// (node_path e.g. "InventorySaveData._inventorylist[1]" or
//  "EquipmentSaveData._list[0]._item._itemDyeDataList").
// Returns "" on success, error message on failure.
std::string ExportXml(const ParcEngine::SaveTree& tree,
                      const std::string& xml_path,
                      const std::string& node_path = "");

// Import a whole-save XML. Produces the serialized PARC blob and the
// 128-byte container header (empty if the XML came from a raw blob).
// Runs a parse + reserialize self-check on the result before returning.
std::string ImportXml(const std::string& xml_path,
                      std::vector<uint8_t>& out_blob,
                      std::vector<uint8_t>& out_header);

// Same as ImportXml but reads the XML from memory.
std::string ImportXmlBuffer(const void* data, size_t size,
                            std::vector<uint8_t>& out_blob,
                            std::vector<uint8_t>& out_header);

// Build a lobby.save blob (load-menu slot metadata) from an embedded
// known-good template. lobby.save does not need to match save.save, so a
// generated one makes any save.save a drag-and-drop slot. display_name is
// what the load menu shows; non-ASCII stripped, capped at 32 chars.
std::string BuildLobbySave(const std::string& display_name,
                           std::vector<uint8_t>& out_blob,
                           std::vector<uint8_t>& out_header,
                           int character_key = 1, int level = 1);

// Import a subtree XML and graft it into the loaded save at the path stored
// in the file (or override_path). Appending is allowed: an element path index
// equal to the list size appends. Reserializes + reparses the tree.
std::string ImportNodeXml(ParcEngine::SaveTree& tree,
                          const std::string& xml_path,
                          const std::string& override_path = "");

} // namespace ParcXml
