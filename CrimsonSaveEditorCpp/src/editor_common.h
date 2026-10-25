#pragma once
/**
 * editor_common.h — Shared utilities for all editor tabs.
 *
 * Blob read/write helpers, ImGui table wrappers, data path resolution.
 * Include this instead of duplicating helpers in every tab file.
 */

#include "parc_engine.h"
#include "imgui.h"
#include <nlohmann/json.hpp>
#include <string>
#include <vector>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <algorithm>
#include <functional>
#include <cstdarg>
#include <cstdio>

namespace EditorCommon {

// ── Startup log (writes to startup_log.txt next to exe) ──

inline FILE* g_logFile = nullptr;

inline void LogInit(const std::string& dataDir) {
    std::string logPath = dataDir + "startup_log.txt";
    g_logFile = fopen(logPath.c_str(), "w");
    if (!g_logFile) g_logFile = fopen("startup_log.txt", "w");
}

inline void Log(const char* fmt, ...) {
    if (!g_logFile) return;
    va_list args;
    va_start(args, fmt);
    vfprintf(g_logFile, fmt, args);
    va_end(args);
    fprintf(g_logFile, "\n");
    fflush(g_logFile);
}

inline void LogClose() {
    if (g_logFile) { fclose(g_logFile); g_logFile = nullptr; }
}

// ── Blob read helpers ──

inline uint8_t  RU8 (const std::vector<uint8_t>& b, uint32_t o) { return (o<b.size()) ? b[o] : 0; }
inline uint16_t RU16(const std::vector<uint8_t>& b, uint32_t o) { if(o+2>b.size()) return 0; return b[o]|(b[o+1]<<8); }
inline uint32_t RU32(const std::vector<uint8_t>& b, uint32_t o) { if(o+4>b.size()) return 0; return b[o]|(b[o+1]<<8)|(b[o+2]<<16)|(b[o+3]<<24); }
inline uint64_t RU64(const std::vector<uint8_t>& b, uint32_t o) { if(o+8>b.size()) return 0; return (uint64_t)RU32(b,o)|((uint64_t)RU32(b,o+4)<<32); }
inline float    RF32(const std::vector<uint8_t>& b, uint32_t o) { uint32_t v=RU32(b,o); float f; memcpy(&f,&v,4); return f; }

// ── Blob write helpers ──

inline void WU8 (std::vector<uint8_t>& b, uint32_t o, uint8_t  v) { if(o<b.size()) b[o]=v; }
inline void WU16(std::vector<uint8_t>& b, uint32_t o, uint16_t v) { if(o+2>b.size()) return; b[o]=v&0xFF; b[o+1]=(v>>8)&0xFF; }
inline void WU32(std::vector<uint8_t>& b, uint32_t o, uint32_t v) { if(o+4>b.size()) return; b[o]=v&0xFF; b[o+1]=(v>>8)&0xFF; b[o+2]=(v>>16)&0xFF; b[o+3]=(v>>24)&0xFF; }
inline void WU64(std::vector<uint8_t>& b, uint32_t o, uint64_t v) { WU32(b,o,(uint32_t)v); WU32(b,o+4,(uint32_t)(v>>32)); }
inline void WF32(std::vector<uint8_t>& b, uint32_t o, float    v) { if(o+4>b.size()) return; memcpy(&b[o],&v,4); }

// ── ImGui table column setup (always uses correct flags) ──

inline void ColFixed(const char* label, float width) {
    ImGui::TableSetupColumn(label, ImGuiTableColumnFlags_WidthFixed, width);
}
inline void ColStretch(const char* label) {
    ImGui::TableSetupColumn(label, ImGuiTableColumnFlags_WidthStretch);
}

// ── Data path resolution ──
// Searches multiple candidate paths and returns the first that exists.

inline std::string FindDataFile(const std::string& dataDir, const std::string& filename) {
    std::vector<std::string> candidates = {
        dataDir + "data\\" + filename,
        std::string("data\\") + filename,
        std::string("..\\data\\") + filename,
        std::string("..\\..\\data\\") + filename,
        std::string("..\\..\\..\\data\\") + filename,
    };
    for (auto& c : candidates) {
        if (std::ifstream(c).good()) return c;
    }
    return "";
}

// ── Name lookups ──

std::string GetItemName(uint32_t key);

// Display names loaded from display_names.json (knowledge groups, quest groups, gauges, etc.)
inline std::unordered_map<std::string, std::unordered_map<std::string, std::string>> g_displayNames;

inline bool LoadDisplayNames(const std::string& dataDir) {
    std::string path = FindDataFile(dataDir, "display_names.json");
    if (path.empty()) return false;
    try {
        std::ifstream f(path);
        auto j = nlohmann::json::parse(f);
        for (auto& [category, entries] : j.items()) {
            for (auto& [key, name] : entries.items()) {
                g_displayNames[category][key] = name.get<std::string>();
            }
        }
        Log("  display_names: %d categories", (int)g_displayNames.size());
        return true;
    } catch (...) { return false; }
}

inline std::string LookupDisplayName(const std::string& category, uint32_t key) {
    auto cit = g_displayNames.find(category);
    if (cit == g_displayNames.end()) return "";
    auto nit = cit->second.find(std::to_string(key));
    if (nit == cit->second.end()) return "";
    return nit->second;
}

} // namespace EditorCommon

// Shorthand namespace alias for convenience
namespace EC = EditorCommon;
