#pragma once
#include "parc_engine.h"
#include <string>
#include <vector>
#include <cstdint>

namespace SaveRepair {

struct DiagnosticResult {
    bool decrypt_ok = false;
    bool decompress_ok = false;
    bool parc_header_ok = false;
    bool toc_ok = false;
    bool tree_parse_ok = false;
    int blocks_parsed = 0;
    int blocks_failed = 0;
    int total_fields = 0;
    int fields_present = 0;
    uint32_t blob_size = 0;
    std::string error_msg;
    std::string error_block;
    uint32_t error_offset = 0;
    std::vector<std::string> block_names;
    std::vector<std::string> warnings;
};

struct RepairAction {
    std::string description;
    enum Type { RESERIALIZE, RESTORE_BLOCK, ZERO_FIELD, TRANSPLANT } type;
    std::string block_name;
    int block_index = -1;
};

struct BlockHealth {
    std::string name;
    uint32_t entry_index = 0;
    enum Status { OK, WARN, BROKEN } status = OK;
    std::string issue;
    uint32_t block_size = 0;
    int elem_count = 0;
    int fields_present = 0;
    int fields_total = 0;
    int undecoded_ranges = 0;
    uint32_t undecoded_bytes = 0;
    bool fixable = false; // true if a reference save can fix it
};

// Run diagnostics on the current save
DiagnosticResult DiagnoseSave(ParcEngine::SaveTree& tree);

// Run diagnostics on a file that may fail to load normally
DiagnosticResult DiagnoseFile(const std::string& path);

// Repair: re-serialize the tree (fixes all PO/trailing_size corruption)
bool RepairReserialize(ParcEngine::SaveTree& tree);

// Repair: transplant a block from a reference save
bool TransplantBlock(ParcEngine::SaveTree& tree, const std::string& refPath,
                     const std::string& blockName);

// Repair: zero out a specific block's data (keeps structure valid)
bool ZeroBlock(ParcEngine::SaveTree& tree, const std::string& blockName);

// Verify save: parse → serialize → compare
bool VerifyRoundtrip(ParcEngine::SaveTree& tree, std::string& diff_report);

// Render the repair tab
void RenderRepairTab(ParcEngine::SaveTree& tree, bool& dirty, const std::string& savePath);

// Diagnostic: dump full save tree state to startup_log
void DumpFullSaveState(const char* label, const ParcEngine::SaveTree& tree);

// Diagnostic: compare two save states block-by-block
void CompareStates(const char* label,
    const ParcEngine::SaveTree& before, const std::vector<uint8_t>& before_blob,
    const ParcEngine::SaveTree& after, const std::vector<std::string>& transplanted_blocks);

} // namespace SaveRepair
