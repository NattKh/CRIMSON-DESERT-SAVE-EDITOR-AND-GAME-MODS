/**
 * inventory_editor.cpp — ImGui-based save editor.
 * Win32 + DirectX11. Save browser + inventory editing.
 */
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <d3d11.h>
#include <shlobj.h>
#include <urlmon.h>
#pragma comment(lib, "urlmon.lib")

#include "imgui.h"
#include "imgui_impl_win32.h"
#include "imgui_impl_dx11.h"

#include "parc_engine.h"
#include "parc_serializer.h"
#include "parc_xml.h"
#include "item_factory.h"
#include "icon_cache.h"
#include "quest_editor.h"
#include "knowledge_editor.h"
#include "dye_editor.h"
#include "world_editor.h"
#include "appearance_editor.h"
#include "editor_common.h"
#include "save_repair.h"
#include "transplant_editor.h"

#include <nlohmann/json.hpp>
#include <filesystem>
#include <lz4.h>
using json = nlohmann::json;

#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <unordered_map>
#include <cstring>
#include <algorithm>
#include <cstdio>
#include <filesystem>
#include <thread>
#include <atomic>
#include <mutex>
#include <functional>
#include <unordered_set>
#include <ctime>
namespace fs = std::filesystem;

// ── D3D11 globals ──
static ID3D11Device*            g_pd3dDevice = nullptr;
static ID3D11DeviceContext*     g_pd3dDeviceContext = nullptr;
static IDXGISwapChain*          g_pSwapChain = nullptr;
static ID3D11RenderTargetView*  g_mainRenderTargetView = nullptr;
static UINT                     g_ResizeWidth = 0, g_ResizeHeight = 0;

static bool CreateDeviceD3D(HWND hWnd);
static void CleanupDeviceD3D();
static void CreateRenderTarget();
static void CleanupRenderTarget();
static LRESULT WINAPI WndProc(HWND hWnd, UINT msg, WPARAM wParam, LPARAM lParam);
extern IMGUI_IMPL_API LRESULT ImGui_ImplWin32_WndProcHandler(HWND hWnd, UINT msg, WPARAM wParam, LPARAM lParam);

// ── Async task system (reusable for any blocking operation) ──
struct AsyncTask {
    std::thread thread;
    std::atomic<bool> running{false};
    std::atomic<bool> done{false};
    std::mutex mtx;
    std::string statusText;
    std::function<void()> onComplete;

    void Run(const std::string& label, std::function<void()> work, std::function<void()> complete = {}) {
        if (running) return;
        {
            std::lock_guard<std::mutex> lk(mtx);
            statusText = label;
        }
        running = true;
        done = false;
        onComplete = complete;
        if (thread.joinable()) thread.join();
        thread = std::thread([this, work]() {
            try { work(); } catch (...) {}
            done = true;
            running = false;
        });
    }

    void Update() {
        if (done && onComplete) {
            if (thread.joinable()) thread.join();
            onComplete();
            onComplete = nullptr;
            done = false;
        }
    }

    bool IsRunning() const { return running; }

    std::string GetStatus() {
        std::lock_guard<std::mutex> lk(mtx);
        return statusText;
    }

    void SetStatus(const std::string& s) {
        std::lock_guard<std::mutex> lk(mtx);
        statusText = s;
    }

    ~AsyncTask() { if (thread.joinable()) thread.join(); }
};

static AsyncTask g_asyncTask;

static void RenderLoadingOverlay() {
    if (!g_asyncTask.IsRunning()) return;

    ImGuiIO& io = ImGui::GetIO();
    ImGui::SetNextWindowPos(ImVec2(0, 0));
    ImGui::SetNextWindowSize(io.DisplaySize);
    ImGui::SetNextWindowBgAlpha(0.7f);
    ImGui::Begin("##Loading", nullptr,
        ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
        ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoInputs |
        ImGuiWindowFlags_NoNav);

    float cx = io.DisplaySize.x * 0.5f;
    float cy = io.DisplaySize.y * 0.5f;

    // Spinner
    float t = (float)ImGui::GetTime();
    float radius = 20.0f;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    int segments = 12;
    for (int i = 0; i < segments; i++) {
        float angle = t * 3.0f + (float)i * (6.2832f / segments);
        float alpha = 0.2f + 0.8f * ((float)((i + (int)(t * 8.0f)) % segments) / segments);
        float x = cx + cosf(angle) * radius;
        float y = cy + sinf(angle) * radius;
        dl->AddCircleFilled(ImVec2(x, y), 4.0f, ImGui::GetColorU32(ImVec4(1, 0.65f, 0.3f, alpha)));
    }

    // Status text
    std::string status = g_asyncTask.GetStatus();
    ImVec2 textSize = ImGui::CalcTextSize(status.c_str());
    ImGui::SetCursorPos(ImVec2(cx - textSize.x * 0.5f, cy + 35));
    ImGui::TextColored(ImVec4(1, 1, 1, 0.9f), "%s", status.c_str());

    ImGui::End();
}

// ── Item name database ──
struct ItemDef {
    int key = 0;
    std::string name;
    std::string category;
    int64_t maxStack = 1;
};
static std::unordered_map<int, ItemDef> g_itemDB;
static std::string g_dataDir;

// ── Tab visibility config (loaded from tabs.json next to exe) ──
static std::unordered_map<std::string, bool> g_tabEnabled;
static bool TabOn(const char* name) {
    auto it = g_tabEnabled.find(name);
    return it == g_tabEnabled.end() || it->second; // default: visible
}
static void LoadTabConfig(const std::string& dir) {
    for (auto& path : { dir + "tabs.json", std::string("tabs.json") }) {
        std::ifstream f(path);
        if (!f.good()) continue;
        try {
            auto j = nlohmann::json::parse(f);
            for (auto& [key, val] : j.items()) {
                if (val.is_boolean()) g_tabEnabled[key] = val.get<bool>();
            }
            EC::Log("Loaded tabs.json: %zu entries from %s", g_tabEnabled.size(), path.c_str());
        } catch (...) { EC::Log("Failed to parse tabs.json"); }
        return;
    }
    EC::Log("No tabs.json found — all tabs enabled");
}

std::string GetItemDisplayName(uint32_t key) {
    auto it = g_itemDB.find((int)key);
    if (it != g_itemDB.end()) return it->second.name;
    return "";
}

static void LoadItemDB(const std::string& path) {
    std::ifstream f(path);
    if (!f) return;
    try {
        json j = json::parse(f);
        for (auto& item : j["items"]) {
            ItemDef def;
            def.key = item.value("itemKey", 0);
            def.name = item.value("name", "");
            def.category = item.value("category", "");
            def.maxStack = item.value("maxStack", (int64_t)1);
            g_itemDB[def.key] = def;
        }
    } catch (...) {}
}

// ── Globals (declared early so all functions can use them) ──
static ParcEngine::SaveTree g_tree;
static bool g_saveLoaded = false;
static std::string g_savePath;
static std::string g_statusMsg;
static bool g_dirty = false;
static bool g_backupCreated = false;

// ── Local game-data sync (port of the Python ItemDatabasePage pipeline) ──
// Reads iteminfo straight from the game's PAZ archives (PAMT index + LZ4),
// resolves English names from the bundled localization map (with an optional
// fresh .paloc overlay), and rewrites data/item_names.json. No crypto needed.
namespace LocalSync {

struct PazEntry {
    std::string path;
    std::string pazFile;
    uint32_t offset = 0;
    uint32_t compSize = 0;
    uint32_t origSize = 0;
    uint32_t flags = 0;
    bool compressed() const { return compSize != origSize; }
};

inline uint32_t rdU32(const std::vector<uint8_t>& d, size_t off) {
    uint32_t v = 0;
    if (off + 4 <= d.size()) memcpy(&v, d.data() + off, 4);
    return v;
}

// Parse a .pamt index into its PAZ entries. Mirrors tools/paz_parse.py:parse_pamt.
// pamtPath like ".../0008/0.pamt"; paz files resolve to <pazDir>/<stem+index>.paz.
inline std::vector<PazEntry> ParsePamt(const std::string& pamtPath, const std::string& pazDir) {
    std::vector<PazEntry> out;
    std::ifstream f(pamtPath, std::ios::binary);
    if (!f) return out;
    std::vector<uint8_t> d((std::istreambuf_iterator<char>(f)), {});
    if (d.size() < 16) return out;

    int pamtStem = 0;
    try { pamtStem = std::stoi(fs::path(pamtPath).stem().string()); } catch (...) { pamtStem = 0; }

    size_t off = 0;
    off += 4;                                   // magic
    uint32_t pazCount = rdU32(d, off); off += 4;
    off += 8;
    for (uint32_t i = 0; i < pazCount; i++) {
        off += 4; off += 4;
        if (i < pazCount - 1) off += 4;
    }

    // Folder table (we only need the root prefix)
    uint32_t folderSize = rdU32(d, off); off += 4;
    size_t folderEnd = off + folderSize;
    std::string folderPrefix;
    while (off < folderEnd && off + 5 <= d.size()) {
        uint32_t parent = rdU32(d, off);
        uint8_t slen = d[off + 4];
        std::string name((const char*)d.data() + off + 5, std::min<size_t>(slen, d.size() - (off + 5)));
        if (parent == 0xFFFFFFFFu) folderPrefix = name;
        off += 5 + slen;
    }

    // Node table (path segments, parent-linked)
    uint32_t nodeSize = rdU32(d, off); off += 4;
    size_t nodeStart = off;
    std::unordered_map<uint32_t, std::pair<uint32_t, std::string>> nodes;
    while (off < nodeStart + nodeSize && off + 5 <= d.size()) {
        uint32_t rel = (uint32_t)(off - nodeStart);
        uint32_t parent = rdU32(d, off);
        uint8_t slen = d[off + 4];
        std::string name((const char*)d.data() + off + 5, std::min<size_t>(slen, d.size() - (off + 5)));
        nodes[rel] = { parent, name };
        off += 5 + slen;
    }
    auto buildPath = [&](uint32_t ref) {
        std::vector<std::string> parts;
        uint32_t cur = ref;
        while (cur != 0xFFFFFFFFu && parts.size() < 64) {
            auto it = nodes.find(cur);
            if (it == nodes.end()) break;
            parts.push_back(it->second.second);
            cur = it->second.first;
        }
        std::string s;
        for (auto it = parts.rbegin(); it != parts.rend(); ++it) s += *it;
        return s;
    };

    uint32_t folderCount = rdU32(d, off); off += 4;
    off += 4;
    off += (size_t)folderCount * 16;

    while (off + 20 <= d.size()) {
        PazEntry e;
        uint32_t nodeRef = rdU32(d, off);
        e.offset   = rdU32(d, off + 4);
        e.compSize = rdU32(d, off + 8);
        e.origSize = rdU32(d, off + 12);
        e.flags    = rdU32(d, off + 16);
        off += 20;
        uint32_t pazIndex = e.flags & 0xFF;
        std::string nodePath = buildPath(nodeRef);
        e.path = folderPrefix.empty() ? nodePath : (folderPrefix + "/" + nodePath);
        char pazName[64];
        snprintf(pazName, sizeof(pazName), "%d.paz", pamtStem + (int)pazIndex);
        e.pazFile = pazDir + "\\" + pazName;
        out.push_back(std::move(e));
    }
    return out;
}

// Read an entry's bytes from its PAZ, LZ4-decompressing if stored compressed.
inline bool ExtractEntry(const PazEntry& e, std::vector<uint8_t>& out) {
    std::ifstream f(e.pazFile, std::ios::binary);
    if (!f) return false;
    f.seekg(e.offset, std::ios::beg);
    std::vector<uint8_t> raw(e.compSize);
    f.read((char*)raw.data(), e.compSize);
    if ((uint32_t)f.gcount() != e.compSize) return false;
    if (!e.compressed()) { out = std::move(raw); return true; }
    out.resize(e.origSize);
    int n = LZ4_decompress_safe((const char*)raw.data(), (char*)out.data(),
                                (int)raw.size(), (int)out.size());
    if (n < 0) return false;
    out.resize(n);
    return true;
}

// Parse a decrypted/plain .paloc into loc_index -> string. Mirrors _parse_paloc.
inline void ParsePaloc(const std::vector<uint8_t>& d, std::unordered_map<uint64_t, std::string>& map) {
    size_t pos = 0;
    while (pos + 12 < d.size()) {
        pos += 8;
        if (pos + 4 > d.size()) break;
        uint32_t kl = rdU32(d, pos); pos += 4;
        if (kl == 0 || kl > 100 || pos + kl > d.size()) break;
        std::string ks((const char*)d.data() + pos, kl); pos += kl;
        if (pos + 4 > d.size()) break;
        uint32_t vl = rdU32(d, pos); pos += 4;
        if (vl > 50000 || pos + vl > d.size()) break;
        std::string vs((const char*)d.data() + pos, vl); pos += vl;
        bool digits = !ks.empty();
        for (char c : ks) if (c < '0' || c > '9') { digits = false; break; }
        if (digits) { try { map[std::stoull(ks)] = vs; } catch (...) {} }
    }
}

inline std::string GuessCategory(const std::string& internal) {
    std::string n = internal;
    for (auto& c : n) c = (char)tolower((unsigned char)c);
    auto starts = [&](const char* p) { return n.rfind(p, 0) == 0; };
    auto has = [&](const char* p) { return n.find(p) != std::string::npos; };
    if (starts("money") || starts("currency")) return "Currency";
    for (const char* p : {"weapon_", "onehand", "twohand", "bow_", "crossbow"}) if (starts(p)) return "Equipment";
    for (const char* p : {"armor_", "helmet_", "glove_", "shoe_", "shield_"}) if (starts(p)) return "Equipment";
    for (const char* p : {"ring_", "necklace_", "earring_", "belt_", "accessory_"}) if (starts(p)) return "Equipment";
    for (const char* p : {"_ore", "_ingot", "_hide", "_leather", "_timber", "_plank",
                          "_herb", "_reagent", "_fabric", "_thread", "_stone", "material"}) if (has(p)) return "Material";
    for (const char* p : {"potion", "food_", "elixir", "meal_", "drink_", "consumable"}) if (has(p)) return "Consumable";
    for (const char* p : {"arrow", "bolt_", "ammo", "quiver", "pyeonjeon"}) if (has(p)) return "Ammo";
    if (has("quest")) return "Quest";
    return "Misc";
}

} // namespace LocalSync

// ── Settings ──
struct AppSettings {
    std::string customSaveDir;
    std::string customLocalAppData;
    std::string customPackDir;
    std::string gamePath;
    std::string lastSavePath;
    bool autoBackup = true;

    void Load() {
        try {
            std::ifstream f("settings.json");
            if (!f) return;
            json j = json::parse(f);
            customSaveDir = j.value("savePath", "");
            customLocalAppData = j.value("customLocalAppData", "");
            customPackDir = j.value("packPath", "");
            gamePath = j.value("gamePath", "");
            lastSavePath = j.value("lastSavePath", "");
            autoBackup = j.value("autoBackup", true);
        } catch (...) {}
    }

    void Save() {
        try {
            json j;
            j["savePath"] = customSaveDir;
            j["customLocalAppData"] = customLocalAppData;
            j["packPath"] = customPackDir;
            j["gamePath"] = gamePath;
            j["lastSavePath"] = lastSavePath;
            j["autoBackup"] = autoBackup;
            std::ofstream f("settings.json");
            f << j.dump(2);
        } catch (...) {}
    }

    static std::string AutoDetectGamePath() {
        std::vector<std::string> candidates;
        // Scan all drive letters
        for (char letter = 'A'; letter <= 'Z'; letter++) {
            std::string drive(1, letter);
            candidates.push_back(drive + ":\\SteamLibrary\\steamapps\\common\\Crimson Desert");
        }
        candidates.push_back("C:\\Program Files (x86)\\Steam\\steamapps\\common\\Crimson Desert");
        candidates.push_back("C:\\Program Files\\Steam\\steamapps\\common\\Crimson Desert");
        candidates.push_back("C:\\Program Files\\Epic Games\\CrimsonDesert");
        candidates.push_back("D:\\Program Files\\Epic Games\\CrimsonDesert");
        // Check for 0008/0.paz as validation
        for (auto& path : candidates) {
            std::string paz = path + "\\0008\\0.paz";
            if (std::filesystem::exists(paz)) return path;
        }
        return "";
    }

    // Self-update item_names.json straight from the local game install.
    // Returns (ok, human-readable status). Writes to <outPath> on success.
    std::pair<bool, std::string> SyncItemsFromLocal(const std::string& dataDir,
                                                    const std::string& outPath) {
        using namespace LocalSync;
        if (gamePath.empty() || !fs::is_directory(gamePath))
            return { false, "Game path not set. Open Settings and set your Crimson Desert folder." };

        std::string pamtPath = gamePath + "\\0008\\0.pamt";
        std::string pazDir   = gamePath + "\\0008";
        if (!fs::exists(pamtPath))
            return { false, "Could not find 0008\\0.pamt in the game folder.\nIs the game path correct?" };

        auto entries = ParsePamt(pamtPath, pazDir);
        const PazEntry *hdrE = nullptr, *bodyE = nullptr;
        for (auto& e : entries) {
            std::string lp = e.path;
            for (auto& c : lp) c = (char)tolower((unsigned char)c);
            if (lp.find("iteminfo.pabgh") != std::string::npos) hdrE = &e;
            else if (lp.find("iteminfo.pabgb") != std::string::npos) bodyE = &e;
        }
        if (!hdrE || !bodyE)
            return { false, "iteminfo not found in PAZ 0008 (PAMT had " +
                            std::to_string(entries.size()) + " entries)." };

        std::vector<uint8_t> hdr, body;
        if (!ExtractEntry(*hdrE, hdr) || !ExtractEntry(*bodyE, body))
            return { false, "Failed to extract/decompress iteminfo from the PAZ." };
        if (hdr.size() < 2)
            return { false, "iteminfo.pabgh too small." };

        // Header: u16 count, then count × (u32 key, u32 record-offset)
        uint16_t count = 0; memcpy(&count, hdr.data(), 2);
        std::vector<std::pair<uint32_t, uint32_t>> hdrEntries; // (key, off)
        hdrEntries.reserve(count);
        for (int i = 0; i < count; i++) {
            size_t base = 2 + (size_t)i * 8;
            if (base + 8 > hdr.size()) break;
            uint32_t key = 0, off = 0;
            memcpy(&key, hdr.data() + base, 4);
            memcpy(&off, hdr.data() + base + 4, 4);
            hdrEntries.push_back({ key, off });
        }

        // Parse item records (key, internalName, maxStack, locIndex)
        struct Rec { uint32_t key; std::string internal; uint64_t maxStack; uint64_t locIndex; };
        std::vector<Rec> recs;
        recs.reserve(hdrEntries.size());
        for (size_t idx = 0; idx < hdrEntries.size(); idx++) {
            uint32_t recOff = hdrEntries[idx].second;
            uint32_t recEnd = (idx + 1 < hdrEntries.size()) ? hdrEntries[idx + 1].second
                                                            : (uint32_t)body.size();
            if ((size_t)recOff + 20 > body.size()) continue;
            size_t pos = recOff;
            uint32_t itemKey = 0; memcpy(&itemKey, body.data() + pos, 4); pos += 4;
            uint32_t strLen = 0;  memcpy(&strLen, body.data() + pos, 4); pos += 4;
            if (strLen > 200 || pos + strLen > recEnd) continue;
            std::string internal((char*)body.data() + pos, strLen); pos += strLen;
            pos += 1;
            if (pos + 8 > recEnd) continue;
            uint64_t maxStack = 0; memcpy(&maxStack, body.data() + pos, 8); pos += 8;
            if (pos + 9 > recEnd) continue;
            pos += 1;
            uint64_t locIndex = 0; memcpy(&locIndex, body.data() + pos, 8); pos += 8;
            recs.push_back({ itemKey, internal, maxStack, locIndex });
        }
        if (recs.empty())
            return { false, "No item records parsed from iteminfo.pabgb." };

        // Build loc map: bundled localization_eng_map.json, then overlay a fresh
        // .paloc if the user has one (fresh strings win).
        std::unordered_map<uint64_t, std::string> locMap;
        std::string locSource = "none";
        {
            std::string lp = EC::FindDataFile(dataDir, "localization_eng_map.json");
            if (!lp.empty()) {
                try {
                    std::ifstream lf(lp);
                    json lj = json::parse(lf);
                    for (auto it = lj.begin(); it != lj.end(); ++it) {
                        try { locMap[std::stoull(it.key())] = it.value().get<std::string>(); }
                        catch (...) {}
                    }
                    locSource = "bundled map";
                } catch (...) {}
            }
        }
        // Optional fresh paloc overlay (unpacked by the user, no crypto here).
        for (const std::string& cand : {
                 dataDir + "localizationstring_eng.paloc",
                 dataDir + "data\\localizationstring_eng.paloc",
                 std::string("localizationstring_eng.paloc") }) {
            if (fs::exists(cand)) {
                std::ifstream pf(cand, std::ios::binary);
                std::vector<uint8_t> pd((std::istreambuf_iterator<char>(pf)), {});
                size_t before = locMap.size();
                ParsePaloc(pd, locMap);
                locSource = (locSource == "none" ? "fresh paloc"
                                                 : "bundled map + fresh paloc");
                (void)before;
                break;
            }
        }

        // Emit item_names.json (same schema the editor loads)
        std::sort(recs.begin(), recs.end(),
                  [](const Rec& a, const Rec& b) { return a.key < b.key; });
        json items = json::array();
        int matched = 0;
        for (auto& r : recs) {
            if (r.key == 0) continue;
            std::string name;
            auto it = locMap.find(r.locIndex);
            if (it != locMap.end() && !it->second.empty()) { name = it->second; matched++; }
            else { name = r.internal; for (auto& c : name) if (c == '_') c = ' '; }
            json item;
            item["itemKey"] = r.key;
            item["name"] = name;
            item["category"] = GuessCategory(r.internal);
            item["internalName"] = r.internal;
            item["maxStack"] = r.maxStack;
            items.push_back(item);
        }
        json jout;
        jout["items"] = items;

        std::error_code ec;
        fs::create_directories(fs::path(outPath).parent_path(), ec);
        std::ofstream of(outPath, std::ios::binary);
        if (!of) return { false, "Could not write " + outPath };
        of << jout.dump(2);
        of.close();

        char msg[512];
        snprintf(msg, sizeof(msg),
                 "Synced %zu items from the game client.\n"
                 "Names matched: %d (source: %s)\n"
                 "Unmatched (internal name): %zu\n"
                 "Saved to %s",
                 (size_t)items.size(), matched, locSource.c_str(),
                 (size_t)items.size() - (size_t)matched, outPath.c_str());
        return { true, msg };
    }
};
static AppSettings g_settings;
static int g_selectedSlot = -1;
static char g_treeSearch[128] = {};
static char g_activeSearch[128] = {};
static uint32_t g_navTargetOffset = 0;
static bool g_navPending = false;
static int g_skipTreeFrames = 0;  // skip tree rendering for N frames after splice
static std::unordered_set<const void*> g_searchHits;
static std::unordered_map<const void*, size_t> g_listPage;
static bool g_pendingDuplicate = false;
static std::string g_pendingDupBlock;
static std::string g_pendingDupField;
static std::vector<uint8_t> g_pendingDupBytes;
static uint32_t g_pendingDupSrcOffset = 0;
static uint32_t g_pendingReplaceEnd = 0; // pagination state for tree lists
static bool g_pendingCreateItem = false;
static std::string g_pendingCreatePath;
static bool g_pendingXmlNodeImport = false;
static std::string g_pendingXmlNodeFile;
static std::string g_pendingXmlNodePath;
static bool g_pendingXmlSaveImport = false;
static std::string g_pendingXmlSaveFile;
static std::string g_pendingXmlSaveOut;
static bool g_xmlImportWriteLobby = true;
static std::vector<uint8_t> g_pendingCreateBytes;
static void LogMsg(const char* fmt, ...);
static void ExtractItems();
static const char* LookupFieldName(const std::string& fieldName, int64_t value);

// ── Mount database ──
struct MountDef {
    int characterKey;
    std::string name;
};
static std::vector<MountDef> g_mountList;
static std::unordered_set<int> g_mountKeySet;

static void LoadMountKeys(const std::string& path) {
    std::ifstream f(path);
    if (!f) return;
    std::string line;
    while (std::getline(f, line)) {
        if (line.empty() || line[0] == '#') continue;
        // Format: "1234 - Name (internal)"
        auto dash = line.find(" - ");
        if (dash == std::string::npos) continue;
        int key = atoi(line.substr(0, dash).c_str());
        if (key <= 0) continue;
        std::string name = line.substr(dash + 3);
        // Strip internal name in parentheses
        auto paren = name.find(" (");
        if (paren != std::string::npos) name = name.substr(0, paren);
        g_mountList.push_back({key, name});
        g_mountKeySet.insert(key);
    }
}

// ── Pet catalog (data/pet_catalog.json, extracted from characterinfo via dmm parser) ──
struct PetDef {
    int characterKey = 0;
    std::string name;     // display name (e.g. "Beagle")
    std::string species;  // Dog/Cat/Horse/Livestock/...
    std::string role;     // Friendly / Domestic / Wild / Battle / ...
    int vehicleInfo = 0;  // !=0 => mountable, behaves as a vehicle
};
static std::vector<PetDef> g_petList;
static std::unordered_set<int> g_petKeySet;

static void LoadPetCatalog(const std::string& path) {
    std::ifstream f(path);
    if (!f) return;
    json j; f >> j;
    if (!j.contains("entries")) return;
    for (auto& e : j["entries"]) {
        PetDef p;
        p.characterKey = e.value("key", 0);
        p.name = e.value("display", std::string());
        if (p.name.empty()) p.name = e.value("internal", std::string("ID:") + std::to_string(p.characterKey));
        p.species = e.value("species", std::string("?"));
        p.role = e.value("role", std::string("?"));
        p.vehicleInfo = e.value("vehicle_info", 0);
        if (p.characterKey <= 0) continue;
        g_petList.push_back(p);
        g_petKeySet.insert(p.characterKey);
    }
}

static bool AddMountToSave(int targetCharKey) {
    if (!g_saveLoaded) return false;

    // Find a donor mount in _mercenaryDataList
    const SaveParserCpp::GenericFieldValue* donorElem = nullptr;
    const SaveParserCpp::GenericFieldValue* donorList = nullptr;
    std::string blockClass;

    for (auto& obj : g_tree.parsed.objects) {
        if (obj.class_name.find("MercenaryClan") == std::string::npos) continue;
        blockClass = obj.class_name;
        for (auto& fld : obj.fields) {
            if (fld.name != "_mercenaryDataList") continue;
            donorList = &fld;
            for (auto& el : fld.list_elements) {
                for (auto& cf : el.child_fields) {
                    if (cf.name == "_characterKey" && cf.present &&
                        cf.start_offset > 0 && cf.end_offset > cf.start_offset) {
                        uint32_t sz = cf.end_offset - cf.start_offset;
                        if (sz <= 8 && cf.start_offset + sz <= g_tree.blob.size()) {
                            int64_t ck = 0;
                            memcpy(&ck, g_tree.blob.data() + cf.start_offset, sz);
                            if (g_mountKeySet.count((int)ck)) {
                                donorElem = &el;
                                break;
                            }
                        }
                    }
                }
                if (donorElem) break;
            }
            break;
        }
        break;
    }

    if (!donorElem || !donorList) {
        g_statusMsg = "No existing mount found to duplicate from!";
        return false;
    }

    // Check target not already present
    for (auto& el : donorList->list_elements) {
        for (auto& cf : el.child_fields) {
            if (cf.name == "_characterKey" && cf.present &&
                cf.start_offset > 0 && cf.end_offset > cf.start_offset) {
                uint32_t sz = cf.end_offset - cf.start_offset;
                if (sz <= 8 && cf.start_offset + sz <= g_tree.blob.size()) {
                    int64_t ck = 0;
                    memcpy(&ck, g_tree.blob.data() + cf.start_offset, sz);
                    if ((int)ck == targetCharKey) {
                        g_statusMsg = "This mount is already in your save!";
                        return false;
                    }
                }
            }
        }
    }

    // Copy donor bytes
    uint32_t elemSize = donorElem->end_offset - donorElem->start_offset;
    std::vector<uint8_t> elemBytes(
        g_tree.blob.begin() + donorElem->start_offset,
        g_tree.blob.begin() + donorElem->end_offset);

    // Find max mercenaryNo
    uint32_t maxMercNo = 0;
    for (auto& el : donorList->list_elements) {
        for (auto& cf : el.child_fields) {
            if (cf.name == "_mercenaryNo" && cf.present &&
                cf.start_offset > 0 && cf.end_offset > cf.start_offset) {
                uint32_t sz = cf.end_offset - cf.start_offset;
                if (sz <= 8 && cf.start_offset + sz <= g_tree.blob.size()) {
                    uint64_t mno = 0;
                    memcpy(&mno, g_tree.blob.data() + cf.start_offset, sz);
                    if ((uint32_t)mno > maxMercNo) maxMercNo = (uint32_t)mno;
                }
            }
        }
    }
    uint32_t newMercNo = maxMercNo + 1;

    // Patch _mercenaryNo in the copy
    for (auto& cf : donorElem->child_fields) {
        if (cf.name == "_mercenaryNo" && cf.present &&
            cf.start_offset >= donorElem->start_offset) {
            uint32_t relOff = cf.start_offset - donorElem->start_offset;
            uint32_t sz = cf.end_offset - cf.start_offset;
            if (relOff + sz <= elemBytes.size() && sz <= 8) {
                uint64_t val = (uint64_t)newMercNo;
                memcpy(elemBytes.data() + relOff, &val, sz);
            }
            break;
        }
    }

    // Patch _characterKey in the copy
    for (auto& cf : donorElem->child_fields) {
        if (cf.name == "_characterKey" && cf.present &&
            cf.start_offset >= donorElem->start_offset) {
            uint32_t relOff = cf.start_offset - donorElem->start_offset;
            uint32_t sz = cf.end_offset - cf.start_offset;
            if (relOff + sz <= elemBytes.size() && sz <= 8) {
                int64_t val = (int64_t)targetCharKey;
                memcpy(elemBytes.data() + relOff, &val, sz);
            }
            break;
        }
    }

    // Splice
    LogMsg("ADD_MOUNT: target=%d donor=[0x%X..0x%X] mercNo=%u->%u",
        targetCharKey, donorElem->start_offset, donorElem->end_offset, maxMercNo, newMercNo);

    auto result = ParcEngine::SpliceIntoList(g_tree, blockClass, "_mercenaryDataList", elemBytes);

    if (result.ok) {
        g_searchHits.clear();
        g_activeSearch[0] = 0;
        g_treeSearch[0] = 0;
        g_navPending = false;
        ExtractItems(); // clears g_items internally
        g_dirty = true;
        LogMsg("ADD_MOUNT: OK, po_fixed=%d", result.po_fixed);
        return true;
    } else {
        g_statusMsg = "Add mount failed: " + result.error;
        LogMsg("ADD_MOUNT: FAILED: %s", result.error.c_str());
        return false;
    }
}

// ── Store names database ──
static std::unordered_map<int, std::string> g_storeNames;

static void LoadStoreNames(const std::string& path) {
    std::ifstream f(path);
    if (!f) return;
    try {
        json j = json::parse(f);
        for (auto& [k, v] : j.items()) {
            try { g_storeNames[std::stoi(k)] = v.get<std::string>(); } catch (...) {}
        }
    } catch (...) {}
}

// ── Repurchase (vendor buyback) items ──
struct VendorItem {
    int itemNo = 0;
    int itemKey = 0;
    int64_t stackCount = 0;
    int slotNo = 0;
    int enchantLevel = 0;
    int endurance = 0;
    int sharpness = 0;
    std::string name;
    std::string category;
    std::string vendorName;
    uint32_t blobOffset = 0;      // absolute offset of saveVer field in blob
    uint32_t itemKeyOffset = 0;   // absolute offset of itemKey in blob
    uint32_t itemKeySize = 0;
    uint32_t xferKeyOffset = 0;   // _transferredItemKey offset
    uint32_t xferKeySize = 0;
    uint32_t stackOffset = 0;
    uint32_t stackSize = 0;
};
static std::vector<VendorItem> g_vendorItems;

static void ScanVendorItems() {
    g_vendorItems.clear();
    if (!g_saveLoaded) return;
    auto& blob = g_tree.blob;

    // Find StoreSaveData block in TOC
    uint32_t storeOff = 0, storeSize = 0;
    for (auto& obj : g_tree.parsed.objects) {
        if (obj.class_name.find("StoreSaveData") != std::string::npos &&
            obj.class_name.find("Stock") == std::string::npos) {
            storeOff = obj.data_offset;
            storeSize = obj.data_size;
            break;
        }
    }
    if (storeOff == 0 || storeSize == 0) return;

    uint32_t storeEnd = storeOff + storeSize;
    if (storeEnd > blob.size()) storeEnd = (uint32_t)blob.size();

    static const uint8_t SENT[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};

    // Scan for items inside the store block (same pattern as inventory scanner)
    for (uint32_t off = storeOff + 20; off + 40 < storeEnd; off++) {
        // saveVersion must be 1
        uint32_t saveVer = 0;
        memcpy(&saveVer, blob.data() + off, 4);
        if (saveVer != 1) continue;

        // itemNo (i64)
        int64_t itemNo = 0;
        memcpy(&itemNo, blob.data() + off + 4, 8);
        if (itemNo < 1 || itemNo > 9999999) continue;

        // itemKey (u32)
        uint32_t itemKey = 0;
        memcpy(&itemKey, blob.data() + off + 12, 4);
        if (itemKey < 1 || itemKey > 0x7FFFFFFF) continue;

        // slotNo (u16)
        uint16_t slotNo = 0;
        memcpy(&slotNo, blob.data() + off + 16, 2);

        // stackCount (i64)
        int64_t stack = 0;
        memcpy(&stack, blob.data() + off + 18, 8);
        if (stack < 1 || stack > 9999999) continue;

        // Must have sentinel 16 bytes before
        if (off < 16) continue;
        if (memcmp(blob.data() + off - 16, SENT, 8) != 0) continue;

        // enchant, endurance, sharpness
        uint16_t enchant = 0, endur = 0, sharp = 0;
        if (off + 34 <= storeEnd) {
            memcpy(&enchant, blob.data() + off + 26, 2);
            memcpy(&endur, blob.data() + off + 30, 2);
            memcpy(&sharp, blob.data() + off + 32, 2);
        }

        // Find which vendor this belongs to by checking store key from parse tree
        std::string vendorName = "Unknown Vendor";
        for (auto& obj : g_tree.parsed.objects) {
            if (obj.class_name.find("StoreSaveData") == std::string::npos) continue;
            if (obj.class_name.find("Stock") != std::string::npos) continue;
            for (auto& fld : obj.fields) {
                if (fld.meta_kind != 6 && fld.meta_kind != 7) continue;
                for (auto& el : fld.list_elements) {
                    if (off >= el.start_offset && off < el.end_offset) {
                        // This item is inside this store element
                        for (auto& cf : el.child_fields) {
                            if (cf.name == "_storeKey" && cf.present &&
                                cf.start_offset > 0 && cf.end_offset > cf.start_offset) {
                                uint32_t sz = cf.end_offset - cf.start_offset;
                                if (sz <= 4 && cf.start_offset + sz <= blob.size()) {
                                    uint32_t sk = 0;
                                    memcpy(&sk, blob.data() + cf.start_offset, sz);
                                    auto it = g_storeNames.find(sk);
                                    if (it != g_storeNames.end())
                                        vendorName = it->second;
                                    else
                                        vendorName = "Store " + std::to_string(sk);
                                }
                            }
                        }
                        goto vendor_found;
                    }
                }
            }
            break;
        }
        vendor_found:

        VendorItem vi;
        vi.itemNo = (int)itemNo;
        vi.itemKey = (int)itemKey;
        vi.stackCount = stack;
        vi.slotNo = slotNo;
        vi.enchantLevel = (enchant != 0xFFFF) ? enchant : 0;
        vi.endurance = endur;
        vi.sharpness = sharp;
        vi.blobOffset = off;
        vi.itemKeyOffset = off + 12;
        vi.itemKeySize = 4;
        vi.stackOffset = off + 18;
        vi.stackSize = 8;
        // _transferredItemKey is after sockets: off + 52 + maxSocketCount * 26
        // But we need to find it reliably. maxSocketCount is at off+50 (1 byte)
        if (off + 52 <= storeEnd) {
            uint8_t maxSock = blob[off + 50];
            if (maxSock <= 8) {
                uint32_t xferOff = off + 52 + maxSock * 26;
                if (xferOff + 4 <= storeEnd) {
                    vi.xferKeyOffset = xferOff;
                    vi.xferKeySize = 4;
                }
            }
        }
        vi.vendorName = vendorName;

        auto it = g_itemDB.find(itemKey);
        if (it != g_itemDB.end()) {
            vi.name = it->second.name;
            vi.category = it->second.category;
        } else {
            vi.name = "Item " + std::to_string(itemKey);
            vi.category = "?";
        }

        g_vendorItems.push_back(vi);
    }
}

// ── Save browser ──
struct SaveSlot {
    std::string path;
    std::string userId;
    std::string slotId;
    std::string displayName;
    std::string platform;
    std::string dateStr;
    uint64_t size = 0;
    time_t mtime = 0;
};
static std::vector<SaveSlot> g_saveSlots;

static std::string SlotDisplayName(const std::string& slotId) {
    if (slotId.size() > 4 && slotId.substr(0, 4) == "slot") {
        const char* numStr = slotId.c_str() + 4;
        bool allDigits = true;
        for (const char* p = numStr; *p; p++) { if (!isdigit(*p)) { allDigits = false; break; } }
        if (allDigits && *numStr) {
            int n = atoi(numStr);
            if (n < 100) return "Auto Save " + std::to_string(n + 1);
            return "Save Slot " + std::to_string(n - 99);
        }
    }
    return slotId;
}

static int SlotSortKey(const std::string& slotId) {
    if (slotId.size() > 4 && slotId.substr(0, 4) == "slot") {
        const char* numStr = slotId.c_str() + 4;
        bool allDigits = true;
        for (const char* p = numStr; *p; p++) { if (!isdigit(*p)) { allDigits = false; break; } }
        if (allDigits && *numStr) return atoi(numStr);
    }
    return 999999;
}

static bool IsAutoSaveSlot(const std::string& slotId) {
    if (slotId.size() > 4 && slotId.substr(0, 4) == "slot") {
        const char* numStr = slotId.c_str() + 4;
        bool allDigits = true;
        for (const char* p = numStr; *p; p++) { if (!isdigit(*p)) { allDigits = false; break; } }
        if (allDigits && *numStr) return atoi(numStr) < 100;
    }
    return false;
}

static std::string FormatFileTime(const fs::path& filePath) {
    auto ftime = fs::last_write_time(filePath);
    auto sctp = std::chrono::time_point_cast<std::chrono::system_clock::duration>(
        ftime - fs::file_time_type::clock::now() + std::chrono::system_clock::now());
    auto tt = std::chrono::system_clock::to_time_t(sctp);
    char buf[64];
    struct tm tm_local;
    localtime_s(&tm_local, &tt);
    strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M", &tm_local);
    return buf;
}

static bool HasSaveMagic(const fs::path& filePath) {
    try {
        std::ifstream f(filePath, std::ios::binary);
        if (!f) return false;
        char magic[4] = {};
        f.read(magic, 4);
        return f.gcount() == 4 && memcmp(magic, "SAVE", 4) == 0;
    } catch (...) { return false; }
}

static void ScanStandardSaveDir(const std::string& baseDir, const std::string& platform) {
    if (!fs::is_directory(baseDir)) return;
    try {
        for (auto& userEntry : fs::directory_iterator(baseDir)) {
            if (!userEntry.is_directory()) continue;
            std::string userId = userEntry.path().filename().string();

            // Collect and sort slot directories numerically
            std::vector<fs::path> slotDirs;
            for (auto& slotEntry : fs::directory_iterator(userEntry.path())) {
                if (slotEntry.is_directory()) slotDirs.push_back(slotEntry.path());
            }
            std::sort(slotDirs.begin(), slotDirs.end(), [](const fs::path& a, const fs::path& b) {
                return SlotSortKey(a.filename().string()) < SlotSortKey(b.filename().string());
            });

            for (auto& slotPath : slotDirs) {
                std::string slotId = slotPath.filename().string();

                // Check save.save first, then lobby.save as fallback
                fs::path savePath = slotPath / "save.save";
                if (!fs::is_regular_file(savePath)) {
                    savePath = slotPath / "lobby.save";
                    if (!fs::is_regular_file(savePath)) continue;
                }

                SaveSlot slot;
                slot.path = savePath.string();
                slot.userId = userId;
                slot.slotId = slotId;
                slot.platform = platform;
                slot.size = fs::file_size(savePath);
                slot.mtime = fs::last_write_time(savePath).time_since_epoch().count();
                slot.dateStr = FormatFileTime(savePath);
                slot.displayName = SlotDisplayName(slotId);
                g_saveSlots.push_back(slot);
            }
        }
    } catch (...) {}
}

static void ScanGamePassWgs(const std::string& localAppData) {
    std::string packagesDir = localAppData + "\\Packages";
    if (!fs::is_directory(packagesDir)) return;

    try {
        for (auto& pkgEntry : fs::directory_iterator(packagesDir)) {
            if (!pkgEntry.is_directory()) continue;
            std::string pkgName = pkgEntry.path().filename().string();
            if (pkgName.find("PearlAbyss.CrimsonDesert") == std::string::npos) continue;

            fs::path wgsBase = pkgEntry.path() / "SystemAppData" / "wgs";
            if (!fs::is_directory(wgsBase)) continue;

            for (auto& userEntry : fs::directory_iterator(wgsBase)) {
                if (!userEntry.is_directory()) continue;
                std::string userDir = userEntry.path().filename().string();
                if (userDir == "t") continue; // WGS system folder

                int slotIdx = 0;
                std::vector<fs::path> guidDirs;
                for (auto& guidEntry : fs::directory_iterator(userEntry.path())) {
                    if (guidEntry.is_directory())
                        guidDirs.push_back(guidEntry.path());
                }
                std::sort(guidDirs.begin(), guidDirs.end());

                for (auto& guidPath : guidDirs) {
                    fs::path bestFile;
                    uintmax_t bestSize = 0;

                    for (auto& fileEntry : fs::directory_iterator(guidPath)) {
                        if (!fileEntry.is_regular_file()) continue;
                        std::string fname = fileEntry.path().filename().string();
                        if (fname.find("container") == 0) continue;

                        uintmax_t sz = fileEntry.file_size();
                        if (sz > 1000 && sz > bestSize && HasSaveMagic(fileEntry.path())) {
                            bestSize = sz;
                            bestFile = fileEntry.path();
                        }
                    }

                    if (!bestFile.empty()) {
                        SaveSlot slot;
                        slot.path = bestFile.string();
                        slot.userId = userDir;
                        slot.slotId = "Save " + std::to_string(slotIdx);
                        slot.platform = "GamePass";
                        slot.size = bestSize;
                        slot.mtime = fs::last_write_time(bestFile).time_since_epoch().count();
                        slot.dateStr = FormatFileTime(bestFile);
                        slot.displayName = "Save " + std::to_string(slotIdx);
                        g_saveSlots.push_back(slot);
                        slotIdx++;
                    }
                }
            }
        }
    } catch (...) {}
}

static void ScanCustomDir(const std::string& customDir) {
    if (!fs::is_directory(customDir)) return;

    // Strategy 1: check for save.save directly in root (single save pointed at)
    {
        fs::path direct = fs::path(customDir) / "save.save";
        if (fs::is_regular_file(direct)) {
            SaveSlot slot;
            slot.path = direct.string();
            slot.userId = "";
            slot.slotId = "save";
            slot.platform = "Custom";
            slot.size = fs::file_size(direct);
            slot.mtime = fs::last_write_time(direct).time_since_epoch().count();
            slot.dateStr = FormatFileTime(direct);
            slot.displayName = "Custom Save";
            g_saveSlots.push_back(slot);
            return;
        }
    }

    // Strategy 2: standard userId/slotId/save.save hierarchy
    bool foundStandard = false;
    try {
        for (auto& userEntry : fs::directory_iterator(customDir)) {
            if (!userEntry.is_directory()) continue;
            for (auto& slotEntry : fs::directory_iterator(userEntry.path())) {
                if (!slotEntry.is_directory()) continue;
                fs::path savePath = slotEntry.path() / "save.save";
                if (!fs::is_regular_file(savePath)) {
                    // Also check for SAVE-magic files (WGS-style GUID folders)
                    for (auto& f : fs::directory_iterator(slotEntry.path())) {
                        if (!f.is_regular_file()) continue;
                        if (f.path().filename().string().find("container") == 0) continue;
                        if (f.file_size() > 1000 && HasSaveMagic(f.path())) {
                            savePath = f.path();
                            break;
                        }
                    }
                    if (!fs::is_regular_file(savePath)) continue;
                }
                foundStandard = true;
                SaveSlot slot;
                slot.path = savePath.string();
                slot.userId = userEntry.path().filename().string();
                slot.slotId = slotEntry.path().filename().string();
                slot.platform = "Custom";
                slot.size = fs::file_size(savePath);
                slot.mtime = fs::last_write_time(savePath).time_since_epoch().count();
                slot.dateStr = FormatFileTime(savePath);
                slot.displayName = "Custom " + slotEntry.path().filename().string();
                g_saveSlots.push_back(slot);
            }
        }
    } catch (...) {}

    if (foundStandard) return;

    // Strategy 3: flat folder with .save files or SAVE-magic files
    try {
        int idx = 0;
        for (auto& entry : fs::directory_iterator(customDir)) {
            if (!entry.is_regular_file()) continue;
            if (entry.file_size() < 1000) continue;
            std::string ext = entry.path().extension().string();
            bool isSaveExt = (ext == ".save" || ext == ".bak");
            if (!isSaveExt && !HasSaveMagic(entry.path())) continue;

            SaveSlot slot;
            slot.path = entry.path().string();
            slot.userId = "";
            slot.slotId = entry.path().filename().string();
            slot.platform = "Custom";
            slot.size = entry.file_size();
            slot.mtime = fs::last_write_time(entry.path()).time_since_epoch().count();
            slot.dateStr = FormatFileTime(entry.path());
            slot.displayName = "Custom " + entry.path().stem().string();
            g_saveSlots.push_back(slot);
            idx++;
        }
    } catch (...) {}
}

static void ScanSaveFiles() {
    g_saveSlots.clear();

    // Scan custom save directory first (works on Linux/Wine or any manual path)
    if (!g_settings.customSaveDir.empty()) {
        ScanCustomDir(g_settings.customSaveDir);
    }

    // Build list of LOCALAPPDATA paths to check
    std::vector<std::string> localAppDatas;

    // Custom LOCALAPPDATA override (for non-C: drive installs, Wine prefixes, etc.)
    if (!g_settings.customLocalAppData.empty() && fs::is_directory(g_settings.customLocalAppData)) {
        localAppDatas.push_back(g_settings.customLocalAppData);
    }

    // Standard LOCALAPPDATA env var
    const char* env = getenv("LOCALAPPDATA");
    if (env && *env) {
        std::string envStr(env);
        if (std::find(localAppDatas.begin(), localAppDatas.end(), envStr) == localAppDatas.end())
            localAppDatas.push_back(envStr);
    }

    // Linux Steam Proton path (for native Linux or Wine with Proton)
    const char* home = getenv("HOME");
    if (!home) home = getenv("USERPROFILE");
    if (home && *home) {
        std::string protonLocal = std::string(home) +
            "/.local/share/Steam/steamapps/compatdata/3321460/pfx/drive_c/users/steamuser/AppData/Local";
        if (fs::is_directory(protonLocal) &&
            std::find(localAppDatas.begin(), localAppDatas.end(), protonLocal) == localAppDatas.end()) {
            localAppDatas.push_back(protonLocal);
        }
    }

    for (auto& local : localAppDatas) {
        // Steam + Epic + GamePass legacy
        ScanStandardSaveDir(local + "\\Pearl Abyss\\CD\\save", "Steam");
        ScanStandardSaveDir(local + "\\Pearl Abyss\\CD_Epic\\save", "Epic");
        ScanStandardSaveDir(local + "\\Pearl Abyss\\CD_GamePass\\save", "GamePass");

        // Also try forward-slash paths (Linux/Proton)
        ScanStandardSaveDir(local + "/Pearl Abyss/CD/save", "Steam");
        ScanStandardSaveDir(local + "/Pearl Abyss/CD_Epic/save", "Epic");

        // Game Pass WGS (Xbox/Microsoft Store)
        ScanGamePassWgs(local);
    }

    // Deduplicate by path (custom + auto-detect might find the same files)
    std::unordered_set<std::string> seen;
    auto it = std::remove_if(g_saveSlots.begin(), g_saveSlots.end(), [&](const SaveSlot& s) {
        return !seen.insert(s.path).second;
    });
    g_saveSlots.erase(it, g_saveSlots.end());

    std::sort(g_saveSlots.begin(), g_saveSlots.end(), [](const SaveSlot& a, const SaveSlot& b) {
        return a.mtime > b.mtime;
    });
}

// ── Inventory item ──
struct DisplayItem {
    int itemNo = 0;
    int itemKey = 0;
    int64_t stackCount = 0;
    int slotNo = 0;
    int enchantLevel = 0;
    int endurance = 0;
    int sharpness = 0;
    int maxSockets = 0;
    int validSockets = 0;
    std::string name;
    std::string category;
    std::string source;
    uint32_t stackCountOffset = 0;
    uint32_t stackCountSize = 0;
    uint32_t itemKeyOffset = 0;
    uint32_t itemKeySize = 0;
    uint32_t enchantOffset = 0;
    uint32_t enchantSize = 0;
    uint32_t enduranceOffset = 0;
    uint32_t enduranceSize = 0;
    uint32_t sharpnessOffset = 0;
    uint32_t sharpnessSize = 0;
    uint32_t maxSocketOffset = 0;
    uint32_t validSocketOffset = 0;
    uint32_t xferKeyOffset = 0;
    uint32_t xferKeySize = 0;
    uint32_t elemStart = 0;
    uint32_t elemEnd = 0;
    bool modified = false;
};

// ── App state ──
static std::vector<DisplayItem> g_items;

static uint64_t ReadFieldVal(const std::vector<uint8_t>& blob, uint32_t off, uint32_t end) {
    uint32_t sz = end - off;
    if (off == 0 || end == 0 || off >= blob.size() || sz > 8) return 0;
    uint64_t v = 0;
    memcpy(&v, blob.data() + off, sz);
    return v;
}

// Extract items from InventorySaveData._inventorylist[N]._itemList[M]
static void ExtractItems() {
    g_items.clear();
    auto& blob = g_tree.blob;

    for (auto& obj : g_tree.parsed.objects) {
        bool isInventory = obj.class_name.find("InventorySaveData") != std::string::npos
            && obj.class_name.find("Contents") == std::string::npos;
        bool isEquipment = obj.class_name == "EquipmentSaveData";
        bool isMerc = obj.class_name.find("MercenaryClanSaveData") != std::string::npos;

        if (!isInventory && !isEquipment) continue;

        std::string blockSource = isEquipment ? "Equipment" : "Inventory";

        // Walk all list fields to find item lists
        std::function<void(const std::vector<SaveParserCpp::GenericFieldValue>&, const std::string&)> walkFields;
        walkFields = [&](const std::vector<SaveParserCpp::GenericFieldValue>& fields, const std::string& src) {
            for (auto& field : fields) {
                if (!field.present) continue;

                // If this is a list containing ItemSaveData elements
                if ((field.meta_kind == 6 || field.meta_kind == 7) && !field.list_elements.empty()) {
                    for (auto& elem : field.list_elements) {
                        if (elem.child_type_name.find("ItemSaveData") != std::string::npos
                            && elem.child_type_name.find("CharacterConversion") == std::string::npos) {

                            DisplayItem di = {};
                            di.elemStart = elem.start_offset;
                            di.elemEnd = elem.end_offset;
                            for (auto& nf : elem.child_fields) {
                                if (!nf.present) continue;
                                if (nf.name == "_itemNo")
                                    di.itemNo = (int)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                else if (nf.name == "_itemKey") {
                                    di.itemKey = (int)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                    di.itemKeyOffset = nf.start_offset;
                                    di.itemKeySize = nf.end_offset - nf.start_offset;
                                }
                                else if (nf.name == "_stackCount") {
                                    di.stackCount = (int64_t)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                    di.stackCountOffset = nf.start_offset;
                                    di.stackCountSize = nf.end_offset - nf.start_offset;
                                }
                                else if (nf.name == "_slotNo")
                                    di.slotNo = (int)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                else if (nf.name == "_enchantLevel") {
                                    di.enchantLevel = (int)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                    di.enchantOffset = nf.start_offset;
                                    di.enchantSize = nf.end_offset - nf.start_offset;
                                }
                                else if (nf.name == "_endurance") {
                                    di.endurance = (int)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                    di.enduranceOffset = nf.start_offset;
                                    di.enduranceSize = nf.end_offset - nf.start_offset;
                                }
                                else if (nf.name == "_sharpness") {
                                    di.sharpness = (int)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                    di.sharpnessOffset = nf.start_offset;
                                    di.sharpnessSize = nf.end_offset - nf.start_offset;
                                }
                                else if (nf.name == "_maxSocketCount") {
                                    di.maxSockets = (int)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                    di.maxSocketOffset = nf.start_offset;
                                }
                                else if (nf.name == "_validSocketCount") {
                                    di.validSockets = (int)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                    di.validSocketOffset = nf.start_offset;
                                }
                                else if (nf.name == "_transferredItemKey") {
                                    di.xferKeyOffset = nf.start_offset;
                                    di.xferKeySize = nf.end_offset - nf.start_offset;
                                }
                            }
                            if (di.itemNo == 0 && di.itemKey == 0) continue;

                            di.source = src;
                            int lookupKey = di.itemKey ? di.itemKey : di.itemNo;
                            auto it = g_itemDB.find(lookupKey);
                            if (it != g_itemDB.end()) {
                                di.name = it->second.name;
                                di.category = it->second.category;
                            } else {
                                di.name = "Item " + std::to_string(lookupKey);
                                di.category = "?";
                            }
                            g_items.push_back(di);
                        }
                    }

                    // Recurse into list elements' child fields
                    for (auto& elem : field.list_elements) {
                        if (!elem.child_fields.empty()) {
                            walkFields(elem.child_fields, src);
                        }
                    }
                }

                // Recurse into inline objects
                if ((field.meta_kind == 4 || field.meta_kind == 5) && !field.child_fields.empty()) {
                    walkFields(field.child_fields, src);
                }
            }
        };

        walkFields(obj.fields, blockSource);
    }

    std::sort(g_items.begin(), g_items.end(), [](const DisplayItem& a, const DisplayItem& b) {
        if (a.source != b.source) return a.source < b.source;
        return a.slotNo < b.slotNo;
    });

    int equipCount = 0, socketCount = 0;
    for (auto& di : g_items) {
        if (di.source == "Equipment") equipCount++;
        if (di.maxSocketOffset > 0) socketCount++;
    }
    EC::Log("ExtractItems: %zu total, %d equipment, %d with sockets", g_items.size(), equipCount, socketCount);
}

static ParcEngine::SaveTree g_pendingTree;
static std::string g_pendingPath;
static std::string g_pendingError;

static void DoLoadSave(const std::string& path) {
    if (g_asyncTask.IsRunning()) return;

    g_pendingPath = path;
    g_pendingError.clear();

    g_asyncTask.Run("Loading save...", [&]() {
        try {
            g_asyncTask.SetStatus("Decrypting & parsing...");
            g_pendingTree = ParcEngine::LoadSave(g_pendingPath);
            g_asyncTask.SetStatus("Extracting items...");
        } catch (const std::exception& e) {
            g_pendingError = e.what();
        }
    }, [&]() {
        if (!g_pendingError.empty()) {
            g_statusMsg = "Error: " + g_pendingError;
            g_saveLoaded = false;
            return;
        }
        g_savePath = g_pendingPath;
        g_tree = std::move(g_pendingTree);
        g_saveLoaded = true;
        g_backupCreated = false;
        ExtractItems();
        ScanVendorItems();
        if (TabOn("Quests")) QuestEditor::ScanSaveState(g_tree);
        if (TabOn("Knowledge")) KnowledgeEditor::ScanSaveState(g_tree);
        if (TabOn("Dye")) DyeEditor::ScanDyeData(g_tree);
        if (TabOn("World") || TabOn("Stores") || TabOn("Waypoints") || TabOn("Misc")) WorldEditor::ScanAll(g_tree);
        if (TabOn("Appearance")) AppearanceEditor::ScanAppearance(g_tree);
        g_dirty = false;

        // Persist last loaded save path
        g_settings.lastSavePath = g_savePath;
        g_settings.Save();

        std::string fname = fs::path(g_savePath).parent_path().filename().string();
        g_statusMsg = SlotDisplayName(fname) + " \xe2\x80\x94 " + std::to_string(g_items.size()) + " items, "
            + std::to_string(g_tree.blob.size() / 1024) + " KB";
    });
}

// ── Auto-backup system ──
// Creates .bak before first write, .bak2 before second, keeps both.

static void EnsureBackup() {
    if (g_savePath.empty()) return;
    std::string bak1 = g_savePath + ".bak";
    std::string bak2 = g_savePath + ".bak2";
    try {
        if (!g_backupCreated) {
            // First write this session: create .bak from original
            if (std::filesystem::exists(g_savePath)) {
                // If .bak already exists, rotate to .bak2
                if (std::filesystem::exists(bak1)) {
                    std::filesystem::copy_file(bak1, bak2,
                        std::filesystem::copy_options::overwrite_existing);
                }
                std::filesystem::copy_file(g_savePath, bak1,
                    std::filesystem::copy_options::overwrite_existing);
            }
            g_backupCreated = true;
        }
    } catch (...) {}
}

static void RestoreBackup() {
    if (g_savePath.empty()) return;
    std::string bak = g_savePath + ".bak";
    if (!std::filesystem::exists(bak)) {
        g_statusMsg = "No backup found!";
        return;
    }
    try {
        std::filesystem::copy_file(bak, g_savePath,
            std::filesystem::copy_options::overwrite_existing);
        g_tree = ParcEngine::LoadSave(g_savePath);
        g_saveLoaded = true;
        g_backupCreated = false;
        ExtractItems();
        ScanVendorItems();
        if (TabOn("Quests")) QuestEditor::ScanSaveState(g_tree);
        if (TabOn("Knowledge")) KnowledgeEditor::ScanSaveState(g_tree);
        if (TabOn("Dye")) DyeEditor::ScanDyeData(g_tree);
        if (TabOn("World") || TabOn("Stores") || TabOn("Waypoints") || TabOn("Misc")) WorldEditor::ScanAll(g_tree);
        if (TabOn("Appearance")) AppearanceEditor::ScanAppearance(g_tree);
        g_dirty = false;
        g_statusMsg = "Restored from backup!";
    } catch (const std::exception& e) {
        g_statusMsg = "Restore error: " + std::string(e.what());
    }
}

static void DoSave() {
    if (!g_saveLoaded || !g_dirty) return;
    try {
        EnsureBackup();
        for (auto& di : g_items) {
            if (!di.modified || di.stackCountOffset == 0) continue;
            if (di.stackCountSize <= 4) {
                uint32_t val = (uint32_t)di.stackCount;
                memcpy(g_tree.blob.data() + di.stackCountOffset, &val, di.stackCountSize);
            } else {
                uint64_t val = (uint64_t)di.stackCount;
                memcpy(g_tree.blob.data() + di.stackCountOffset, &val, di.stackCountSize);
            }
            di.modified = false;
        }
        ParcEngine::WriteSave(g_tree, g_savePath);
        g_dirty = false;
        g_statusMsg = "Saved! (backup at .bak)";
    } catch (const std::exception& e) {
        g_statusMsg = "Save error: " + std::string(e.what());
    }
}

static void SaveAsDialog() {
    OPENFILENAMEA ofn = {};
    char path[MAX_PATH] = {};
    strncpy(path, g_savePath.c_str(), MAX_PATH - 1);
    ofn.lStructSize = sizeof(ofn);
    ofn.lpstrFilter = "Save files (*.save)\0*.save\0All files\0*.*\0";
    ofn.lpstrFile = path;
    ofn.nMaxFile = MAX_PATH;
    ofn.Flags = OFN_OVERWRITEPROMPT;
    ofn.lpstrTitle = "Save As";
    if (GetSaveFileNameA(&ofn)) {
        g_savePath = path;
        g_dirty = true;
        DoSave();
    }
}

// ── UI Rendering ──

static void RenderSaveBrowser(float width) {
    ImGui::BeginChild("SaveBrowser", ImVec2(width, 0), true);
    ImGui::TextColored(ImVec4(1.0f, 0.65f, 0.3f, 1.0f), "Save Files");
    ImGui::Separator();

    if (ImGui::Button("Refresh", ImVec2(-1, 0))) {
        ScanSaveFiles();
    }

    ImGui::BeginChild("SlotList", ImVec2(0, 0), false);
    if (g_saveSlots.empty()) {
        ImGui::TextWrapped("No saves found.");
        ImGui::TextWrapped("Set a custom path in Settings menu, or edit settings.json:");
        ImGui::TextColored(ImVec4(0.8f, 0.8f, 0.4f, 1.0f), "{\"savePath\": \"...\"}");
    }

    // Find newest save for highlight
    int newestIdx = -1;
    if (!g_saveSlots.empty()) newestIdx = 0; // already sorted by mtime desc

    for (int i = 0; i < (int)g_saveSlots.size(); i++) {
        auto& slot = g_saveSlots[i];
        bool isCurrent = (g_saveLoaded && slot.path == g_savePath);
        bool isLastLoaded = (!g_saveLoaded && slot.path == g_settings.lastSavePath);
        bool isNewest = (i == newestIdx && !isCurrent && !isLastLoaded);
        bool isAutoSave = IsAutoSaveSlot(slot.slotId);

        // Color priority: current (green) > last loaded (accent) > newest (cyan) > auto-save (orange-dim)
        if (isCurrent)
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.3f, 1.0f, 0.3f, 1.0f));
        else if (isLastLoaded)
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(1.0f, 0.27f, 0.4f, 1.0f));
        else if (isNewest)
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.4f, 0.9f, 1.0f, 1.0f));
        else if (isAutoSave)
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(1.0f, 0.75f, 0.3f, 0.8f));

        char label[256];
        snprintf(label, sizeof(label), "%s %s\n  %s  %.1f KB",
            slot.platform.c_str(), slot.displayName.c_str(),
            slot.dateStr.c_str(), slot.size / 1024.0f);

        if (ImGui::Selectable(label, g_selectedSlot == i, 0, ImVec2(0, 36))) {
            g_selectedSlot = i;
            DoLoadSave(slot.path);
        }

        // Right-click context menu
        if (ImGui::BeginPopupContextItem()) {
            ImGui::TextColored(ImVec4(0.5f, 0.8f, 1.0f, 1.0f), "%s", slot.displayName.c_str());
            ImGui::TextColored(ImVec4(0.6f, 0.6f, 0.6f, 1.0f), "%s", slot.path.c_str());
            ImGui::Separator();
            if (ImGui::MenuItem("Open save folder")) {
                std::string folder = slot.path.substr(0, slot.path.find_last_of("\\/"));
                ShellExecuteA(nullptr, "explore", folder.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
            }
            if (ImGui::MenuItem("Copy path to clipboard")) {
                ImGui::SetClipboardText(slot.path.c_str());
            }
            ImGui::EndPopup();
        }

        if (isCurrent || isLastLoaded || isNewest || isAutoSave)
            ImGui::PopStyleColor();
    }
    ImGui::EndChild();
    ImGui::EndChild();
}

// ── Item JSON dump/load ──

static void DumpFieldToJson(json& out, const SaveParserCpp::GenericFieldValue& f,
                             const std::vector<uint8_t>& blob) {
    json fj;
    fj["name"] = f.name;
    fj["type"] = f.type_name;
    fj["kind"] = f.meta_kind;
    fj["present"] = f.present;
    fj["offset"] = f.start_offset;
    fj["end"] = f.end_offset;

    if (f.present && f.meta_kind <= 3 && f.start_offset > 0 && f.end_offset > f.start_offset) {
        uint32_t sz = f.end_offset - f.start_offset;
        if (sz <= 8 && f.start_offset + sz <= blob.size()) {
            int64_t val = 0;
            memcpy(&val, blob.data() + f.start_offset, sz);
            fj["value"] = val;
            fj["size"] = sz;
            const char* dn = LookupFieldName(f.name, val);
            if (dn) fj["display"] = dn;
        }
    }

    if (!f.child_fields.empty()) {
        json children = json::array();
        for (auto& cf : f.child_fields)
            DumpFieldToJson(children.emplace_back(), cf, blob);
        fj["fields"] = children;
    }

    if (!f.list_elements.empty()) {
        json elems = json::array();
        for (auto& el : f.list_elements) {
            json ej;
            ej["type"] = el.child_type_name;
            ej["offset"] = el.start_offset;
            ej["end"] = el.end_offset;
            if (!el.child_fields.empty()) {
                json cfs = json::array();
                for (auto& cf : el.child_fields)
                    DumpFieldToJson(cfs.emplace_back(), cf, blob);
                ej["fields"] = cfs;
            }
            elems.push_back(ej);
        }
        fj["elements"] = elems;
    }

    out = fj;
}

static void DumpItemJson(int itemIdx) {
    if (itemIdx < 0 || itemIdx >= (int)g_items.size()) return;
    auto& di = g_items[itemIdx];
    if (di.elemStart == 0 || di.elemEnd == 0) return;

    // Find the element in the parse tree using iterative DFS
    const SaveParserCpp::GenericFieldValue* foundElem = nullptr;
    {
        struct SI { const SaveParserCpp::GenericFieldValue* f; };
        std::vector<SI> ss;
        ss.reserve(512);
        for (auto& obj : g_tree.parsed.objects) {
            for (auto& f : obj.fields) ss.push_back({&f});
        }
        while (!ss.empty()) {
            const auto* fp = ss.back().f;
            ss.pop_back();
            // Check this field
            if (fp->start_offset == di.elemStart && fp->end_offset == di.elemEnd) {
                foundElem = fp; break;
            }
            // Check list elements
            for (auto& el : fp->list_elements) {
                if (el.start_offset == di.elemStart && el.end_offset == di.elemEnd) {
                    foundElem = &el; break;
                }
                for (auto& cf : el.child_fields) ss.push_back({&cf});
            }
            if (foundElem) break;
            for (auto& cf : fp->child_fields) ss.push_back({&cf});
        }
    }

    if (!foundElem) {
        g_statusMsg = "Could not find item element in parse tree";
        return;
    }

    json root;
    root["itemKey"] = di.itemKey;
    root["itemNo"] = di.itemNo;
    root["name"] = di.name;
    root["category"] = di.category;
    root["source"] = di.source;
    root["elemOffset"] = di.elemStart;
    root["elemEnd"] = di.elemEnd;
    root["elemSize"] = di.elemEnd - di.elemStart;

    json fields = json::array();
    for (auto& cf : foundElem->child_fields)
        DumpFieldToJson(fields.emplace_back(), cf, g_tree.blob);
    root["fields"] = fields;

    // Also dump raw hex
    std::string hex;
    for (uint32_t i = di.elemStart; i < di.elemEnd && i < g_tree.blob.size(); i++) {
        char buf[4]; snprintf(buf, sizeof(buf), "%02X", g_tree.blob[i]);
        hex += buf;
    }
    root["rawHex"] = hex;

    // Write to file
    char path[MAX_PATH] = {};
    snprintf(path, sizeof(path), "item_%d_%d.json", di.itemKey, di.itemNo);
    OPENFILENAMEA ofn = {};
    ofn.lStructSize = sizeof(ofn);
    ofn.lpstrFilter = "JSON files (*.json)\0*.json\0All files\0*.*\0";
    ofn.lpstrFile = path;
    ofn.nMaxFile = MAX_PATH;
    ofn.Flags = OFN_OVERWRITEPROMPT;
    ofn.lpstrTitle = "Dump Item JSON";
    if (GetSaveFileNameA(&ofn)) {
        std::ofstream f(path);
        f << root.dump(2);
        g_statusMsg = "Dumped to " + std::string(path);
    }
}

static void LoadItemJson(int itemIdx) {
    if (itemIdx < 0 || itemIdx >= (int)g_items.size()) return;
    auto& di = g_items[itemIdx];
    if (di.elemStart == 0) return;

    char path[MAX_PATH] = {};
    OPENFILENAMEA ofn = {};
    ofn.lStructSize = sizeof(ofn);
    ofn.lpstrFilter = "JSON files (*.json)\0*.json\0All files\0*.*\0";
    ofn.lpstrFile = path;
    ofn.nMaxFile = MAX_PATH;
    ofn.Flags = OFN_FILEMUSTEXIST;
    ofn.lpstrTitle = "Load Item JSON";
    if (!GetOpenFileNameA(&ofn)) return;

    std::ifstream f(path);
    if (!f) { g_statusMsg = "Cannot open file"; return; }

    json root;
    try { root = json::parse(f); } catch (...) { g_statusMsg = "Invalid JSON"; return; }

    // Find the TARGET element in the parse tree to get its field offsets
    const SaveParserCpp::GenericFieldValue* targetElem = nullptr;
    {
        struct SI { const SaveParserCpp::GenericFieldValue* f; };
        std::vector<SI> ss;
        ss.reserve(512);
        for (auto& obj : g_tree.parsed.objects)
            for (auto& ff : obj.fields) ss.push_back({&ff});
        while (!ss.empty()) {
            const auto* fp = ss.back().f; ss.pop_back();
            if (fp->start_offset == di.elemStart && fp->end_offset == di.elemEnd) {
                targetElem = fp; break;
            }
            for (auto& el : fp->list_elements) {
                if (el.start_offset == di.elemStart && el.end_offset == di.elemEnd) {
                    targetElem = &el; break;
                }
                for (auto& cf : el.child_fields) ss.push_back({&cf});
            }
            if (targetElem) break;
            for (auto& cf : fp->child_fields) ss.push_back({&cf});
        }
    }

    if (!targetElem) {
        g_statusMsg = "Could not find target item in tree";
        return;
    }

    // Build a map of target field name → (offset, size) from the TARGET element
    std::unordered_map<std::string, std::pair<uint32_t, uint32_t>> targetFields;
    for (auto& cf : targetElem->child_fields) {
        if (cf.present && cf.start_offset > 0 && cf.end_offset > cf.start_offset) {
            uint32_t sz = cf.end_offset - cf.start_offset;
            if (sz <= 8) targetFields[cf.name] = {cf.start_offset, sz};
        }
    }

    // Apply values from JSON by matching field NAMES to target offsets
    int applied = 0;
    if (root.contains("fields")) {
        for (auto& fj : root["fields"]) {
            if (!fj.contains("name") || !fj.contains("value") || !fj.contains("present")) continue;
            if (!fj["present"].get<bool>()) continue;
            std::string fname = fj["name"].get<std::string>();
            int64_t val = fj["value"].get<int64_t>();

            auto it = targetFields.find(fname);
            if (it != targetFields.end()) {
                uint32_t off = it->second.first;
                uint32_t sz = it->second.second;
                if (off + sz <= g_tree.blob.size()) {
                    memcpy(g_tree.blob.data() + off, &val, sz);
                    applied++;
                }
            }
        }
    }

    if (applied > 0) {
        g_dirty = true;
        ExtractItems();
        g_statusMsg = "Applied " + std::to_string(applied) + " fields from " + std::string(path);
    } else if (root.contains("rawHex") && root["rawHex"].is_string()) {
        // No matching fields — try full element replacement from rawHex
        std::string hexStr = root["rawHex"].get<std::string>();
        std::vector<uint8_t> newBytes;
        newBytes.reserve(hexStr.size() / 2);
        for (size_t hi = 0; hi + 1 < hexStr.size(); hi += 2) {
            char buf[3] = {hexStr[hi], hexStr[hi+1], 0};
            newBytes.push_back((uint8_t)strtoul(buf, nullptr, 16));
        }

        if (!newBytes.empty()) {
            // Patch itemNo and slotNo in the new bytes to avoid conflicts
            // Find field positions from the JSON
            int64_t maxItemNo = 0;
            int maxSlot = -1;
            for (auto& it2 : g_items) {
                if (it2.itemNo > maxItemNo) maxItemNo = it2.itemNo;
                if (it2.source == "Inventory" && it2.slotNo > maxSlot) maxSlot = it2.slotNo;
            }

            if (root.contains("fields")) {
                for (auto& fj : root["fields"]) {
                    if (!fj.contains("name") || !fj.contains("present") || !fj["present"].get<bool>()) continue;
                    if (!fj.contains("size")) continue;
                    std::string fn = fj["name"].get<std::string>();
                    // Compute relative offset in rawHex: field_offset - elemOffset + header_size
                    // Actually the rawHex starts at element start including header
                    // So field's relative position = field_offset - elemOffset
                    if (!fj.contains("offset") || !root.contains("elemOffset")) continue;
                    uint32_t absOff = fj["offset"].get<uint32_t>();
                    uint32_t elemOff = root["elemOffset"].get<uint32_t>();
                    uint32_t relOff = absOff - elemOff;
                    uint32_t sz = fj["size"].get<uint32_t>();
                    if (relOff + sz > newBytes.size()) continue;

                    if (fn == "_itemNo") {
                        int64_t v = maxItemNo + 1;
                        memcpy(newBytes.data() + relOff, &v, sz);
                    } else if (fn == "_slotNo") {
                        int64_t v = maxSlot + 1;
                        memcpy(newBytes.data() + relOff, &v, sz);
                    }
                }
            }

            // Remap type indices from source to current save
            ParcEngine::AdaptTypeIndicesFromReference(newBytes, g_tree.blob, g_tree,
                "InventorySaveData", "_itemList");

            // Defer the replace
            g_pendingDuplicate = true;
            g_pendingDupBlock = "InventorySaveData";
            g_pendingDupField = "_REPLACE_";
            g_pendingDupBytes = newBytes;
            g_pendingDupSrcOffset = di.elemStart;
            g_pendingReplaceEnd = di.elemEnd;

            g_statusMsg = "Replacing element with JSON data...";
        } else {
            g_statusMsg = "No matching fields and rawHex is empty";
        }
    } else {
        g_statusMsg = "No matching fields found";
    }
}

// Swap popup state
static int g_swapSelectedItem = -1;  // index into g_items
static bool g_swapPopupOpen = false;

// (g_pendingDuplicate, g_pendingDupBlock, etc. declared at top)
static char g_swapSearch[128] = {};
static std::vector<std::pair<int, std::string>> g_swapResults; // itemKey, name

static void DoSwapSearch() {
    g_swapResults.clear();
    if (g_swapSearch[0] == 0) return;
    std::string search = g_swapSearch;
    std::string searchLower = search;
    std::transform(searchLower.begin(), searchLower.end(), searchLower.begin(), ::tolower);

    // Check if numeric
    bool isNum = true;
    for (char c : search) if (!isdigit(c)) { isNum = false; break; }

    for (auto& [key, def] : g_itemDB) {
        if (isNum) {
            if (std::to_string(key).find(search) != std::string::npos) {
                g_swapResults.push_back({key, def.name + " (" + def.category + ")"});
            }
        } else {
            std::string nameLower = def.name;
            std::transform(nameLower.begin(), nameLower.end(), nameLower.begin(), ::tolower);
            std::string catLower = def.category;
            std::transform(catLower.begin(), catLower.end(), catLower.begin(), ::tolower);
            if (nameLower.find(searchLower) != std::string::npos ||
                catLower.find(searchLower) != std::string::npos) {
                g_swapResults.push_back({key, def.name + " (" + def.category + ")"});
            }
        }
        if (g_swapResults.size() >= 100) break; // cap results
    }
    // Sort by name
    std::sort(g_swapResults.begin(), g_swapResults.end(),
        [](auto& a, auto& b) { return a.second < b.second; });
}

static void RenderSwapPopup() {
    if (!g_swapPopupOpen || g_swapSelectedItem < 0 || g_swapSelectedItem >= (int)g_items.size()) return;

    ImGui::SetNextWindowSize(ImVec2(500, 400), ImGuiCond_FirstUseEver);
    if (ImGui::Begin("Swap Item", &g_swapPopupOpen, ImGuiWindowFlags_NoCollapse)) {
        auto& item = g_items[g_swapSelectedItem];
        ImGui::Text("Swapping: %s (key=%d, slot=%d)", item.name.c_str(), item.itemKey, item.slotNo);
        ImGui::Separator();

        ImGui::SetNextItemWidth(350);
        if (ImGui::InputText("Search (Enter)", g_swapSearch, sizeof(g_swapSearch),
                              ImGuiInputTextFlags_EnterReturnsTrue)) {
            DoSwapSearch();
        }
        ImGui::SameLine();
        ImGui::Text("%zu results", g_swapResults.size());

        ImGui::Separator();

        ImGui::BeginChild("SwapResults", ImVec2(0, -30), true);
        for (int ri = 0; ri < (int)g_swapResults.size(); ri++) {
            auto& [key, name] = g_swapResults[ri];
            char label[256];
            snprintf(label, sizeof(label), "%d - %s", key, name.c_str());
            if (ImGui::Selectable(label)) {
                // Do the swap
                if (item.itemKeyOffset > 0 && item.itemKeySize > 0 &&
                    item.itemKeyOffset + item.itemKeySize <= g_tree.blob.size()) {
                    uint32_t newKey = (uint32_t)key;
                    memcpy(g_tree.blob.data() + item.itemKeyOffset, &newKey, item.itemKeySize);
                    // _transferredItemKey = ((itemKey & 0xFFFF) << 16) | 0x0101
                    if (item.xferKeyOffset > 0 && item.xferKeySize > 0 &&
                        item.xferKeyOffset + item.xferKeySize <= g_tree.blob.size()) {
                        uint32_t tik = ((newKey & 0xFFFF) << 16) | 0x0101;
                        memcpy(g_tree.blob.data() + item.xferKeyOffset, &tik, 4);
                    }
                    // Reset enchant/sharpness to 0 (fresh item)
                    if (item.enchantOffset > 0 && item.enchantSize > 0) {
                        uint16_t zero16 = 0;
                        memcpy(g_tree.blob.data() + item.enchantOffset, &zero16, 2);
                        item.enchantLevel = 0;
                    }
                    if (item.sharpnessOffset > 0 && item.sharpnessSize > 0) {
                        uint16_t zero16 = 0;
                        memcpy(g_tree.blob.data() + item.sharpnessOffset, &zero16, 2);
                        item.sharpness = 0;
                    }
                    item.itemKey = key;
                    auto it = g_itemDB.find(key);
                    if (it != g_itemDB.end()) {
                        item.name = it->second.name;
                        item.category = it->second.category;
                    } else {
                        item.name = "Item " + std::to_string(key);
                    }
                    item.modified = true;
                    g_dirty = true;
                    g_statusMsg = "Swapped to " + item.name + "! Save with Ctrl+S.";
                    g_swapPopupOpen = false;
                }
            }
        }
        ImGui::EndChild();

        if (ImGui::Button("Cancel", ImVec2(-1, 0))) {
            g_swapPopupOpen = false;
        }
    }
    ImGui::End();
}

static void RenderInventory() {
    ImGui::BeginChild("MainArea", ImVec2(0, 0), false);

    if (!g_saveLoaded) {
        ImGui::Text("Select a save file from the browser.");
        ImGui::EndChild();
        return;
    }

    // Status bar
    if (!g_statusMsg.empty()) {
        ImGui::TextColored(ImVec4(0.4f, 0.8f, 0.4f, 1.0f), "%s", g_statusMsg.c_str());
    }
    if (g_dirty) {
        ImGui::SameLine();
        ImGui::TextColored(ImVec4(1.0f, 0.4f, 0.3f, 1.0f), " [MODIFIED]");
        ImGui::SameLine();
        if (ImGui::SmallButton("Save (Ctrl+S)")) DoSave();
        ImGui::SameLine();
        if (ImGui::SmallButton("Save As...")) SaveAsDialog();
    }

    ImGui::Separator();

    // Filter
    static char filterBuf[128] = {};
    ImGui::SetNextItemWidth(300);
    ImGui::InputText("Filter", filterBuf, sizeof(filterBuf));

    ImGui::Separator();

    /* Import from another save — REMOVED, use Dump/Replace JSON instead
    if (false) {
        OPENFILENAMEA ofn = {};
        char path[MAX_PATH] = {};
        ofn.lStructSize = sizeof(ofn);
        ofn.lpstrFilter = "Save files (*.save)\0*.save\0All files\0*.*\0";
        ofn.lpstrFile = path;
        ofn.nMaxFile = MAX_PATH;
        ofn.Flags = OFN_FILEMUSTEXIST;
        ofn.lpstrTitle = "Open Source Save File";
        if (GetOpenFileNameA(&ofn)) {
            try {
                g_importTree = ParcEngine::LoadSave(path);
                g_importLoaded = true;
                g_importItems.clear();
                // Extract items from import save using same logic
                auto& iblob = g_importTree.blob;
                for (auto& obj : g_importTree.parsed.objects) {
                    bool isInv = obj.class_name.find("InventorySaveData") != std::string::npos
                        && obj.class_name.find("Contents") == std::string::npos;
                    bool isEquip = obj.class_name == "EquipmentSaveData";
                    if (!isInv && !isEquip) continue;
                    std::string blockSrc = isEquip ? "Equipment" : "Inventory";
                    std::function<void(const std::vector<SaveParserCpp::GenericFieldValue>&, const std::string&)> walkF;
                    walkF = [&](const std::vector<SaveParserCpp::GenericFieldValue>& fields, const std::string& src) {
                        for (auto& field : fields) {
                            if (!field.present) continue;
                            if ((field.meta_kind == 6 || field.meta_kind == 7) && !field.list_elements.empty()) {
                                for (auto& elem : field.list_elements) {
                                    if (elem.child_type_name.find("ItemSaveData") != std::string::npos
                                        && elem.child_type_name.find("CharacterConversion") == std::string::npos) {
                                        DisplayItem di = {};
                                        di.elemStart = elem.start_offset;
                                        di.elemEnd = elem.end_offset;
                                        for (auto& nf : elem.child_fields) {
                                            if (!nf.present) continue;
                                            if (nf.name == "_itemNo") di.itemNo = (int)ReadFieldVal(iblob, nf.start_offset, nf.end_offset);
                                            else if (nf.name == "_itemKey") { di.itemKey = (int)ReadFieldVal(iblob, nf.start_offset, nf.end_offset); di.itemKeyOffset = nf.start_offset; di.itemKeySize = nf.end_offset - nf.start_offset; }
                                            else if (nf.name == "_stackCount") { di.stackCount = (int64_t)ReadFieldVal(iblob, nf.start_offset, nf.end_offset); di.stackCountOffset = nf.start_offset; di.stackCountSize = nf.end_offset - nf.start_offset; }
                                            else if (nf.name == "_slotNo") di.slotNo = (int)ReadFieldVal(iblob, nf.start_offset, nf.end_offset);
                                            else if (nf.name == "_enchantLevel") di.enchantLevel = (int)ReadFieldVal(iblob, nf.start_offset, nf.end_offset);
                                            else if (nf.name == "_endurance") di.endurance = (int)ReadFieldVal(iblob, nf.start_offset, nf.end_offset);
                                            else if (nf.name == "_sharpness") di.sharpness = (int)ReadFieldVal(iblob, nf.start_offset, nf.end_offset);
                                        }
                                        if (di.itemNo == 0 && di.itemKey == 0) continue;
                                        di.source = src;
                                        int lk = di.itemKey ? di.itemKey : di.itemNo;
                                        auto it = g_itemDB.find(lk);
                                        if (it != g_itemDB.end()) { di.name = it->second.name; di.category = it->second.category; }
                                        else { di.name = "Item " + std::to_string(lk); di.category = "?"; }
                                        g_importItems.push_back(di);
                                    }
                                }
                                for (auto& elem : field.list_elements)
                                    if (!elem.child_fields.empty()) walkF(elem.child_fields, src);
                            }
                            if ((field.meta_kind == 4 || field.meta_kind == 5) && !field.child_fields.empty())
                                walkF(field.child_fields, src);
                        }
                    };
                    walkF(obj.fields, blockSrc);
                }
                g_importFilter[0] = 0;
                g_importPopupOpen = true;
                g_statusMsg = "Loaded " + std::to_string(g_importItems.size()) + " items from source save";
            } catch (const std::exception& e) {
                g_statusMsg = "Import error: " + std::string(e.what());
            }
        }
    }

    if (g_importPopupOpen && g_importLoaded) {
        ImGui::SetNextWindowSize(ImVec2(700, 500), ImGuiCond_FirstUseEver);
        if (ImGui::Begin("Import Item from Save", &g_importPopupOpen, ImGuiWindowFlags_NoCollapse)) {
            ImGui::Text("%zu items in source save", g_importItems.size());
            ImGui::SetNextItemWidth(300);
            ImGui::InputText("Filter", g_importFilter, sizeof(g_importFilter));
            ImGui::Separator();

            std::string fL = g_importFilter;
            std::transform(fL.begin(), fL.end(), fL.begin(), ::tolower);

            if (ImGui::BeginTable("ImportItems", 8,
                ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg | ImGuiTableFlags_ScrollY |
                ImGuiTableFlags_Resizable, ImVec2(0, -30))) {

                ImGui::TableSetupScrollFreeze(0, 1);
                ImGui::TableSetupColumn("Source", ImGuiTableColumnFlags_WidthFixed, 70);
                ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 70);
                ImGui::TableSetupColumn("Name", ImGuiTableColumnFlags_WidthStretch);
                ImGui::TableSetupColumn("Cat", ImGuiTableColumnFlags_WidthFixed, 80);
                ImGui::TableSetupColumn("Stack", ImGuiTableColumnFlags_WidthFixed, 50);
                ImGui::TableSetupColumn("+Enc", ImGuiTableColumnFlags_WidthFixed, 40);
                ImGui::TableSetupColumn("Size", ImGuiTableColumnFlags_WidthFixed, 50);
                ImGui::TableSetupColumn("Import", ImGuiTableColumnFlags_WidthFixed, 55);
                ImGui::TableHeadersRow();

                for (int ii = 0; ii < (int)g_importItems.size(); ii++) {
                    auto& im = g_importItems[ii];
                    if (g_importFilter[0] != 0) {
                        std::string nL = im.name; std::transform(nL.begin(), nL.end(), nL.begin(), ::tolower);
                        std::string cL = im.category; std::transform(cL.begin(), cL.end(), cL.begin(), ::tolower);
                        if (nL.find(fL) == std::string::npos && cL.find(fL) == std::string::npos &&
                            std::to_string(im.itemKey).find(fL) == std::string::npos) continue;
                    }

                    ImGui::TableNextRow();
                    ImGui::PushID(ii + 100000);

                    ImGui::TableSetColumnIndex(0); ImGui::Text("%s", im.source.c_str());
                    ImGui::TableSetColumnIndex(1); ImGui::Text("%d", im.itemKey);
                    ImGui::TableSetColumnIndex(2); ImGui::Text("%s", im.name.c_str());
                    ImGui::TableSetColumnIndex(3); ImGui::Text("%s", im.category.c_str());
                    ImGui::TableSetColumnIndex(4); ImGui::Text("%lld", (long long)im.stackCount);
                    ImGui::TableSetColumnIndex(5); ImGui::Text("%d", im.enchantLevel);
                    ImGui::TableSetColumnIndex(6); ImGui::Text("%u", im.elemEnd - im.elemStart);
                    ImGui::TableSetColumnIndex(7);
                    if (im.elemStart > 0 && ImGui::SmallButton("Add##imp")) {
                        // Copy raw bytes from import save's blob
                        std::vector<uint8_t> elemBytes(
                            g_importTree.blob.begin() + im.elemStart,
                            g_importTree.blob.begin() + im.elemEnd);

                        // Remap type indices from import save to current save
                        ParcEngine::AdaptTypeIndicesFromReference(elemBytes, g_tree.blob, g_tree,
                            "InventorySaveData", "_itemList");

                        // Find max itemNo + slotNo in current save
                        int64_t maxItemNo = 0;
                        int maxSlot = -1;
                        for (auto& it2 : g_items) {
                            if (it2.itemNo > maxItemNo) maxItemNo = it2.itemNo;
                            if (it2.source == "Inventory" && it2.slotNo > maxSlot) maxSlot = it2.slotNo;
                        }

                        // Patch itemNo and slotNo in the copy using import tree's field offsets
                        for (auto& obj2 : g_importTree.parsed.objects) {
                            for (auto& fld2 : obj2.fields) {
                                std::function<void(const std::vector<SaveParserCpp::GenericFieldValue>&)> findElem;
                                findElem = [&](const std::vector<SaveParserCpp::GenericFieldValue>& flds) {
                                    for (auto& f2 : flds) {
                                        if ((f2.meta_kind == 6 || f2.meta_kind == 7)) {
                                            for (auto& el2 : f2.list_elements) {
                                                if (el2.start_offset == im.elemStart) {
                                                    for (auto& cf : el2.child_fields) {
                                                        if (!cf.present || cf.start_offset < im.elemStart) continue;
                                                        uint32_t relOff = cf.start_offset - im.elemStart;
                                                        uint32_t sz = cf.end_offset - cf.start_offset;
                                                        if (relOff + sz > elemBytes.size()) continue;
                                                        if (cf.name == "_itemNo" && sz <= 8) {
                                                            int64_t v = maxItemNo + 1;
                                                            memcpy(elemBytes.data() + relOff, &v, sz);
                                                        } else if (cf.name == "_slotNo" && sz <= 8) {
                                                            int64_t v = maxSlot + 1;
                                                            memcpy(elemBytes.data() + relOff, &v, sz);
                                                        }
                                                    }
                                                    return;
                                                }
                                                if (!el2.child_fields.empty()) findElem(el2.child_fields);
                                            }
                                        }
                                        if (!f2.child_fields.empty()) findElem(f2.child_fields);
                                    }
                                };
                                findElem({fld2});
                            }
                        }

                        // Defer the splice — find _itemList inside _inventoryKey==2 (main bag)
                        g_pendingDuplicate = true;
                        g_pendingDupBlock = "InventorySaveData";
                        g_pendingDupField = "_itemList";
                        g_pendingDupBytes = elemBytes;
                        g_pendingDupSrcOffset = 0;
                        {
                            for (auto& obj3 : g_tree.parsed.objects) {
                                if (obj3.class_name.find("InventorySaveData") == std::string::npos) continue;
                                for (auto& f3 : obj3.fields) {
                                    if (f3.name != "_inventorylist") continue;
                                    for (auto& el3 : f3.list_elements) {
                                        // Check _inventoryKey == 2 (main bag)
                                        int invKey = 0;
                                        for (auto& cf3 : el3.child_fields) {
                                            if (cf3.name == "_inventoryKey" && cf3.present &&
                                                cf3.start_offset > 0 && cf3.end_offset > cf3.start_offset) {
                                                uint32_t sz3 = cf3.end_offset - cf3.start_offset;
                                                if (sz3 <= 4 && cf3.start_offset + sz3 <= g_tree.blob.size())
                                                    memcpy(&invKey, g_tree.blob.data() + cf3.start_offset, sz3);
                                            }
                                        }
                                        if (invKey == 2) {
                                            // Found main bag — get its _itemList
                                            for (auto& cf3 : el3.child_fields) {
                                                if (cf3.name == "_itemList" && !cf3.list_elements.empty()) {
                                                    g_pendingDupSrcOffset = cf3.list_elements[0].start_offset;
                                                    LogMsg("IMPORT: found main bag (_inventoryKey=2), _itemList has %zu items, firstElem=0x%X",
                                                        cf3.list_elements.size(), g_pendingDupSrcOffset);
                                                }
                                            }
                                        }
                                    }
                                }
                                break;
                            }
                            if (g_pendingDupSrcOffset == 0)
                                LogMsg("IMPORT: WARNING - could not find main bag (_inventoryKey=2)!");
                        }

                        g_importPopupOpen = false;
                        g_statusMsg = "Importing " + im.name + "...";
                        LogMsg("IMPORT: key=%d from source save, elemSize=%u", im.itemKey, im.elemEnd - im.elemStart);
                    }

                    ImGui::PopID();
                }
                ImGui::EndTable();
            }
            if (ImGui::Button("Cancel", ImVec2(-1, 0))) g_importPopupOpen = false;
        }
        ImGui::End();
    }

    */ // end of removed import block

    // CustomAddItem — duplicate selected item, swap key, auto-save
    static int g_addItemSource = -1;
    static bool g_addItemPopup = false;
    static char g_addItemSearch[128] = {};
    static std::vector<std::pair<int, std::string>> g_addItemResults;

    if (g_addItemPopup && g_addItemSource >= 0 && g_addItemSource < (int)g_items.size()) {
        ImGui::SetNextWindowSize(ImVec2(550, 450), ImGuiCond_FirstUseEver);
        if (ImGui::Begin("Add Item (Clone + Swap)", &g_addItemPopup, ImGuiWindowFlags_NoCollapse)) {
            auto& src = g_items[g_addItemSource];
            ImGui::Text("Cloning from: %s (key=%d, slot=%d)", src.name.c_str(), src.itemKey, src.slotNo);
            ImGui::TextColored(ImVec4(1.0f, 0.9f, 0.3f, 1.0f),
                "Best results: pick a target item of the SAME TYPE as the source.");
            ImGui::Separator();

            ImGui::SetNextItemWidth(350);
            if (ImGui::InputText("Search item (Enter)", g_addItemSearch, sizeof(g_addItemSearch),
                                  ImGuiInputTextFlags_EnterReturnsTrue)) {
                g_addItemResults.clear();
                std::string s = g_addItemSearch;
                std::string sL = s;
                std::transform(sL.begin(), sL.end(), sL.begin(), ::tolower);
                bool isNum = true;
                for (char c : s) if (!isdigit(c)) { isNum = false; break; }
                for (auto& [k, def] : g_itemDB) {
                    if (isNum) {
                        if (std::to_string(k).find(s) != std::string::npos)
                            g_addItemResults.push_back({k, def.name + " (" + def.category + ")"});
                    } else {
                        std::string nL = def.name;
                        std::transform(nL.begin(), nL.end(), nL.begin(), ::tolower);
                        std::string cL = def.category;
                        std::transform(cL.begin(), cL.end(), cL.begin(), ::tolower);
                        if (nL.find(sL) != std::string::npos || cL.find(sL) != std::string::npos)
                            g_addItemResults.push_back({k, def.name + " (" + def.category + ")"});
                    }
                    if (g_addItemResults.size() >= 100) break;
                }
                std::sort(g_addItemResults.begin(), g_addItemResults.end(),
                    [](auto& a, auto& b) { return a.second < b.second; });
            }
            ImGui::SameLine();
            ImGui::Text("%zu results", g_addItemResults.size());

            // Stack count
            static int g_addItemStack = 1;
            ImGui::SetNextItemWidth(100);
            ImGui::InputInt("Stack count", &g_addItemStack, 1, 10);
            if (g_addItemStack < 1) g_addItemStack = 1;

            ImGui::Separator();
            ImGui::BeginChild("AddItemResults", ImVec2(0, -30), true);
            for (auto& [k, nm] : g_addItemResults) {
                char lbl[256];
                snprintf(lbl, sizeof(lbl), "%d - %s", k, nm.c_str());
                if (ImGui::Selectable(lbl)) {
                    // Build the duplicate: copy source element, patch key+stack+slot+itemNo
                    if (src.elemStart > 0 && src.elemEnd > src.elemStart &&
                        src.elemEnd <= g_tree.blob.size()) {

                        std::vector<uint8_t> elemBytes(
                            g_tree.blob.begin() + src.elemStart,
                            g_tree.blob.begin() + src.elemEnd);

                        // Find max itemNo across all items for unique ID
                        int64_t maxItemNo = 0;
                        for (auto& it2 : g_items)
                            if (it2.itemNo > maxItemNo) maxItemNo = it2.itemNo;
                        int64_t newItemNo = maxItemNo + 1;

                        // Find max slotNo in same source category
                        int maxSlot = -1;
                        for (auto& it2 : g_items)
                            if (it2.source == src.source && it2.slotNo > maxSlot) maxSlot = it2.slotNo;
                        int newSlot = maxSlot + 1;

                        // Patch fields in the copy using child_field offsets from parse tree
                        // Find the element in the tree to get field offsets
                        const SaveParserCpp::GenericFieldValue* srcElem = nullptr;
                        {
                            struct SI { const SaveParserCpp::GenericFieldValue* f; };
                            std::vector<SI> ss;
                            ss.reserve(512);
                            for (auto& obj : g_tree.parsed.objects)
                                for (auto& ff : obj.fields) ss.push_back({&ff});
                            while (!ss.empty()) {
                                const auto* fp = ss.back().f; ss.pop_back();
                                if (fp->start_offset == src.elemStart && fp->end_offset == src.elemEnd) {
                                    srcElem = fp; break;
                                }
                                for (auto& el : fp->list_elements) {
                                    if (el.start_offset == src.elemStart && el.end_offset == src.elemEnd) {
                                        srcElem = &el; break;
                                    }
                                    for (auto& cf : el.child_fields) ss.push_back({&cf});
                                }
                                if (srcElem) break;
                                for (auto& cf : fp->child_fields) ss.push_back({&cf});
                            }
                        }

                        if (srcElem) {
                            for (auto& cf : srcElem->child_fields) {
                                if (!cf.present || cf.start_offset < src.elemStart) continue;
                                uint32_t relOff = cf.start_offset - src.elemStart;
                                uint32_t sz = cf.end_offset - cf.start_offset;
                                if (relOff + sz > elemBytes.size()) continue;

                                if (cf.name == "_itemKey" && sz <= 8) {
                                    int64_t v = (int64_t)k;
                                    memcpy(elemBytes.data() + relOff, &v, sz);
                                } else if (cf.name == "_itemNo" && sz <= 8) {
                                    memcpy(elemBytes.data() + relOff, &newItemNo, sz);
                                } else if (cf.name == "_slotNo" && sz <= 8) {
                                    int64_t v = (int64_t)newSlot;
                                    memcpy(elemBytes.data() + relOff, &v, sz);
                                } else if (cf.name == "_stackCount" && sz <= 8) {
                                    int64_t v = (int64_t)g_addItemStack;
                                    memcpy(elemBytes.data() + relOff, &v, sz);
                                } else if (cf.name == "_transferredItemKey" && sz <= 4) {
                                    uint32_t tik = (((uint32_t)k & 0xFFFF) << 16) | 0x0101;
                                    memcpy(elemBytes.data() + relOff, &tik, sz);
                                } else if (cf.name == "_enchantLevel" && sz <= 2) {
                                    uint16_t zero = 0;
                                    memcpy(elemBytes.data() + relOff, &zero, sz);
                                } else if (cf.name == "_sharpness" && sz <= 2) {
                                    uint16_t zero = 0;
                                    memcpy(elemBytes.data() + relOff, &zero, sz);
                                }
                            }

                            // Find parent list for splice
                            std::string dupBlock, dupField;
                            for (auto& obj : g_tree.parsed.objects) {
                                bool found = false;
                                for (auto& fld : obj.fields) {
                                    // Search nested
                                    struct SF2 { const SaveParserCpp::GenericFieldValue* f; };
                                    std::vector<SF2> ss2;
                                    ss2.push_back({&fld});
                                    while (!ss2.empty()) {
                                        const auto* fp2 = ss2.back().f; ss2.pop_back();
                                        if ((fp2->meta_kind == 6 || fp2->meta_kind == 7)) {
                                            for (auto& el : fp2->list_elements) {
                                                if (el.start_offset == src.elemStart) {
                                                    dupBlock = obj.class_name;
                                                    dupField = fp2->name;
                                                    found = true; break;
                                                }
                                                for (auto& cf2 : el.child_fields) ss2.push_back({&cf2});
                                            }
                                        }
                                        if (found) break;
                                        for (auto& cf2 : fp2->child_fields) ss2.push_back({&cf2});
                                    }
                                    if (found) break;
                                }
                                if (found) break;
                            }

                            if (!dupBlock.empty()) {
                                g_pendingDuplicate = true;
                                g_pendingDupBlock = dupBlock;
                                g_pendingDupField = dupField;
                                g_pendingDupBytes = elemBytes;
                                g_pendingDupSrcOffset = src.elemStart;
                                g_addItemPopup = false;
                                auto it3 = g_itemDB.find(k);
                                std::string tname = it3 != g_itemDB.end() ? it3->second.name : std::to_string(k);
                                g_statusMsg = "Adding " + tname + " (x" + std::to_string(g_addItemStack) + ")...";
                                LogMsg("ADDITEM: key=%d stack=%d from=%d slot=%d itemNo=%lld",
                                    k, g_addItemStack, src.itemKey, newSlot, (long long)newItemNo);
                            } else {
                                g_statusMsg = "Could not find parent list";
                            }
                        } else {
                            g_statusMsg = "Could not find source element in tree";
                        }
                    }
                }
            }
            ImGui::EndChild();
            if (ImGui::Button("Cancel", ImVec2(-1, 0))) g_addItemPopup = false;
        }
        ImGui::End();
    }

    ImGui::Separator();

    // Render swap popup if open
    RenderSwapPopup();

    // ── CREATE NEW ITEM dialogs ──
    static bool g_createItemOpen = false;
    static bool g_customItemOpen = false;
    static char g_createSearch[128] = {};
    static std::vector<std::pair<int, std::string>> g_createResults;
    static int g_createSelectedKey = 0;
    static char g_createSelectedName[128] = {};
    static int g_createEnchant = 0;
    static int g_createEndurance = 65535;
    static int g_createSharpness = 0;
    static int g_createSockets = 0;
    static int g_createStack = 1;
    static int g_createCategory = 1;
    static bool g_createNewMark = true;
    static int g_createItemType = 1; // 0=Equip+Sockets, 1=Equip, 2=Consumable, 3=Quest

    // Add Pack — bulk clone+swap from a pack file
    static bool g_packPopupOpen = false;
    static std::vector<std::pair<std::string, std::string>> g_packList; // filename, display name
    static std::string g_packStatus;

    // Add Pack state
    static std::vector<int> g_packQueue;
    static std::string g_packQueueName;
    static int g_packQueueTotal = 0;
    static int g_packQueueAdded = 0;

    if (ImGui::Button("Add Pack")) {
        g_packPopupOpen = true;
        g_packList.clear();
        // Scan data/packs/ for JSON files
        for (auto& candidate : {
            g_dataDir + "data\\packs",
            std::string("data\\packs"),
            std::string("packs")
        }) {
            if (std::filesystem::is_directory(candidate)) {
                for (auto& entry : std::filesystem::directory_iterator(candidate)) {
                    if (entry.path().extension() == ".json" && entry.path().filename() != "index.json") {
                        try {
                            std::ifstream pf(entry.path());
                            json pj = json::parse(pf);
                            std::string display = pj.value("name", entry.path().stem().string())
                                + " (" + std::to_string(pj["items"].size()) + " items)";
                            g_packList.push_back({entry.path().string(), display});
                        } catch (...) {}
                    }
                }
                break;
            }
        }
        g_packStatus = std::to_string(g_packList.size()) + " packs found";
    }
    ImGui::SameLine();

    // Process pack queue: one item per frame through the SAME deferred handler
    // that Add New Item uses. Save+reload each item. Slow but proven reliable.
    if (!g_packQueue.empty() && g_saveLoaded && !g_pendingDuplicate) {
        if (g_packQueueAdded == 0) EnsureBackup();

        int nextKey = g_packQueue.front();
        g_packQueue.erase(g_packQueue.begin());

        // Find a donor — use g_items (freshly extracted after each save+reload)
        int donor = -1;
        for (int i = 0; i < (int)g_items.size(); i++) {
            if (g_items[i].elemStart > 0 && g_items[i].category != "Currency") { donor = i; break; }
        }
        if (donor >= 0) {
            auto& src = g_items[donor];
            // Use the EXACT same clone+swap path as Add New Item
            SaveParserCpp::GenericFieldValue* srcElem = nullptr;
            std::string dupBlock, dupField;
            for (auto& obj : g_tree.parsed.objects) {
                for (auto& fld : obj.fields) {
                    struct SS { const SaveParserCpp::GenericFieldValue* f; };
                    std::vector<SS> ss; ss.push_back({&fld});
                    while (!ss.empty()) {
                        const auto* fp = ss.back().f; ss.pop_back();
                        if (fp->meta_kind == 6 || fp->meta_kind == 7) {
                            for (auto& el : fp->list_elements) {
                                if (el.start_offset == src.elemStart) {
                                    srcElem = const_cast<SaveParserCpp::GenericFieldValue*>(&el);
                                    dupBlock = obj.class_name;
                                    dupField = fp->name;
                                    goto pack_one_found;
                                }
                                for (auto& cf : el.child_fields) ss.push_back({&cf});
                            }
                        }
                        for (auto& cf : fp->child_fields) ss.push_back({&cf});
                    }
                }
            }
            pack_one_found:;
            if (srcElem && !srcElem->raw_value.empty() && !dupBlock.empty()) {
                auto elemBytes = srcElem->raw_value;
                int64_t maxNo = 0; int maxSl = 0;
                for (auto& it2 : g_items) {
                    if (it2.itemNo > maxNo) maxNo = it2.itemNo;
                    if (it2.slotNo > maxSl) maxSl = it2.slotNo;
                }
                for (auto& cf : srcElem->child_fields) {
                    if (!cf.present || cf.start_offset < src.elemStart) continue;
                    uint32_t relOff = cf.start_offset - src.elemStart;
                    uint32_t sz = cf.end_offset - cf.start_offset;
                    if (relOff + sz > elemBytes.size()) continue;
                    if (cf.name == "_itemKey" && sz <= 4) { uint32_t v = (uint32_t)nextKey; memcpy(elemBytes.data()+relOff, &v, sz); }
                    else if (cf.name == "_itemNo" && sz <= 8) { int64_t v = maxNo+1; memcpy(elemBytes.data()+relOff, &v, sz); }
                    else if (cf.name == "_slotNo" && sz <= 2) { uint16_t v = (uint16_t)(maxSl+1); memcpy(elemBytes.data()+relOff, &v, sz); }
                    else if (cf.name == "_transferredItemKey" && sz <= 4) { uint32_t tik = (((uint32_t)nextKey&0xFFFF)<<16)|0x0101; memcpy(elemBytes.data()+relOff, &tik, sz); }
                    else if (cf.name == "_enchantLevel" && sz <= 2) { uint16_t v=0; memcpy(elemBytes.data()+relOff, &v, sz); }
                    else if (cf.name == "_sharpness" && sz <= 2) { uint16_t v=0; memcpy(elemBytes.data()+relOff, &v, sz); }
                    else if (cf.name == "_stackCount" && sz <= 8) { int64_t v=1; memcpy(elemBytes.data()+relOff, &v, sz); }
                }
                // Queue through deferred handler (same as Add New Item)
                g_pendingDuplicate = true;
                g_pendingDupBlock = dupBlock;
                g_pendingDupField = dupField;
                g_pendingDupBytes = elemBytes;
                g_pendingDupSrcOffset = src.elemStart;
                g_packQueueAdded++;
                auto it3 = g_itemDB.find(nextKey);
                g_statusMsg = "Pack: " + std::to_string(g_packQueueAdded) + "/" +
                    std::to_string(g_packQueueTotal) + " - " +
                    (it3 != g_itemDB.end() ? it3->second.name : std::to_string(nextKey));
            }
        }
        if (g_packQueue.empty() && !g_pendingDuplicate) {
            g_statusMsg = "Pack done: " + std::to_string(g_packQueueAdded) + " items added!";
            LogMsg("PACK: complete, %d items", g_packQueueAdded);
        }
    }

    if (g_packPopupOpen) {
        ImGui::SetNextWindowSize(ImVec2(500, 400), ImGuiCond_FirstUseEver);
        if (ImGui::Begin("Add Item Pack", &g_packPopupOpen, ImGuiWindowFlags_NoCollapse)) {
            ImGui::Text("%s", g_packStatus.c_str());
            if (!g_packQueue.empty()) {
                ImGui::SameLine();
                ImGui::TextColored(ImVec4(1,0.8f,0.2f,1), "Adding... %zu remaining", g_packQueue.size());
            }
            ImGui::Separator();
            ImGui::BeginChild("##packlist", ImVec2(0, -40), true);
            for (auto& [path, display] : g_packList) {
                if (ImGui::Selectable(display.c_str())) {
                    // Load pack and queue ALL item keys
                    try {
                        std::ifstream pf(path);
                        json pj = json::parse(pf);
                        g_packQueue.clear();
                        g_packQueueAdded = 0;
                        for (auto& packItem : pj["items"]) {
                            int itemKey = packItem.value("item_key", packItem.value("itemKey", 0));
                            if (itemKey > 0) g_packQueue.push_back(itemKey);
                        }
                        g_packQueueTotal = (int)g_packQueue.size();
                        g_packQueueName = pj.value("name", "Pack");
                        g_packStatus = "Adding " + std::to_string(g_packQueueTotal) + " items from " + g_packQueueName + "...";
                    } catch (const std::exception& e) {
                        g_packStatus = std::string("Pack error: ") + e.what();
                    }
                }
            }
            ImGui::EndChild();
            if (ImGui::Button("Cancel", ImVec2(-1, 0))) { g_packPopupOpen = false; g_packQueue.clear(); }
        }
        ImGui::End();
    }

    // Add New Item — clone+swap approach (proven working in-game)
    if (ImGui::Button("Add New Item")) {
        // Find a non-currency donor item to clone
        int donor = -1;
        for (int i = 0; i < (int)g_items.size(); i++) {
            if (g_items[i].elemStart > 0 && g_items[i].category != "Currency") {
                donor = i; break;
            }
        }
        if (donor >= 0) {
            g_addItemSource = donor;
            g_addItemPopup = true;
            g_addItemSearch[0] = 0;
            g_addItemResults.clear();
        } else {
            g_statusMsg = "No donor item found — need at least one non-currency item in inventory";
        }
    }
    ImGui::SameLine();
    if (false && ImGui::Button("Add from JSON")) {
        OPENFILENAMEA ofn = {};
        char path[MAX_PATH] = {};
        ofn.lStructSize = sizeof(ofn);
        ofn.lpstrFilter = "Item JSON (*.json)\0*.json\0All files\0*.*\0";
        ofn.lpstrFile = path;
        ofn.nMaxFile = MAX_PATH;
        ofn.Flags = OFN_FILEMUSTEXIST;
        ofn.lpstrTitle = "Load Item Dump JSON";
        if (GetOpenFileNameA(&ofn) && g_saveLoaded) {
            std::ifstream jf(path);
            if (jf) {
                try {
                    json jroot = json::parse(jf);
                    if (jroot.contains("rawHex")) {
                        std::string hexStr = jroot["rawHex"].get<std::string>();
                        std::vector<uint8_t> itemBytes;
                        for (size_t hi = 0; hi + 1 < hexStr.size(); hi += 2) {
                            char hbuf[3] = {hexStr[hi], hexStr[hi+1], 0};
                            itemBytes.push_back((uint8_t)strtoul(hbuf, nullptr, 16));
                        }
                        // Remap type indices by name (donor→target schema).
                        // AdaptTypeIndicesFromReference doesn't work here because
                        // _itemList is nested inside _inventorylist, not top-level.
                        // Instead: scan for sentinels, read type_index 3 bytes before each,
                        // look up type NAME in the JSON's source schema info,
                        // and replace with target schema's index for that name.
                        {
                            // Build target name→idx map
                            std::unordered_map<std::string, uint16_t> tgt_ti;
                            for (size_t ti = 0; ti < g_tree.parsed.schema.types.size(); ti++)
                                tgt_ti[g_tree.parsed.schema.types[ti].name] = (uint16_t)ti;
                            // For items from same game version: type names are the same,
                            // just indices differ. Try to identify types by position.
                            // Main type at offset 2+MBC:
                            uint16_t ibMbc = 0; memcpy(&ibMbc, itemBytes.data(), 2);
                            // The main type should be "ItemSaveData"
                            auto iIt = tgt_ti.find("ItemSaveData");
                            if (iIt != tgt_ti.end()) {
                                uint16_t newTi = iIt->second;
                                memcpy(itemBytes.data() + 2 + ibMbc, &newTi, 2);
                            }
                            // Nested type remap using _is_real_po validation.
                            // The JSON has elemOffset — the original absolute position.
                            // A real sentinel at relative pos r has PO at r+8 = elemOffset+r+12.
                            static const uint8_t S8[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
                            uint32_t hdrEnd = 2 + ibMbc + 2 + 1 + 8 + 4;
                            uint32_t elemOff = jroot.value("elemOffset", (uint32_t)0);
                            auto sIt = tgt_ti.find("ItemSocketSaveData");
                            uint16_t sockTi = sIt != tgt_ti.end() ? sIt->second : 0;

                            for (size_t p = hdrEnd; p+12 <= itemBytes.size(); p++) {
                                if (memcmp(itemBytes.data()+p, S8, 8) != 0) continue;
                                if (p < 3) continue;
                                // Verify real PO: value at p+8 must == elemOff + p + 12
                                uint32_t poVal = 0;
                                memcpy(&poVal, itemBytes.data()+p+8, 4);
                                if (elemOff > 0 && poVal != elemOff + (uint32_t)p + 12) continue;
                                // Real sentinel — remap type_index
                                if (sockTi > 0) {
                                    memcpy(itemBytes.data()+p-3, &sockTi, 2);
                                }
                            }
                        }
                        // Patch itemNo + slotNo to be unique
                        uint64_t maxNo = 1000; int maxSl = 0;
                        for (auto& it2 : g_items) {
                            if ((uint64_t)it2.itemNo > maxNo) maxNo = it2.itemNo;
                            if (it2.slotNo > maxSl) maxSl = it2.slotNo;
                        }
                        // Find _itemNo and _slotNo positions in the raw bytes
                        // _itemNo is at header+4 (after reserved_u32), size 8
                        // _slotNo is at header+4+8+4 = header+16, size 2
                        uint16_t mbc = 0; memcpy(&mbc, itemBytes.data(), 2);
                        uint32_t hdr = 2 + mbc + 2 + 1 + 8 + 4 + 4; // to reserved_u32 end
                        if (hdr + 14 <= itemBytes.size()) {
                            // _saveVersion(4) + _itemNo(8) at hdr+4
                            uint64_t newNo = maxNo + 1;
                            memcpy(itemBytes.data() + hdr + 4, &newNo, 8);
                            // _slotNo at hdr+4+8+4 = hdr+16
                            uint16_t newSl = (uint16_t)(maxSl + 1);
                            memcpy(itemBytes.data() + hdr + 16, &newSl, 2);
                        }
                        // Insert via tree
                        int cat = 1; // default to equipment category
                        if (jroot.contains("category")) {
                            std::string c = jroot["category"].get<std::string>();
                            if (c == "Currency") cat = 0;
                            else if (c == "Consumable") cat = 4;
                        }
                        char ipath[64];
                        snprintf(ipath, sizeof(ipath), "_inventorylist[%d]._itemList", cat);
                        g_pendingCreatePath = ipath;
                        g_pendingCreateBytes = std::move(itemBytes);
                        g_pendingCreateItem = true;
                        g_statusMsg = "Adding item from JSON...";
                    }
                } catch (const std::exception& e) {
                    g_statusMsg = std::string("JSON error: ") + e.what();
                }
            }
        }
    }
    ImGui::SameLine();
    ImGui::TextDisabled("(%zu items)", g_items.size());

    // Helper lambda: search items and build results
    auto DoItemSearch = [](const char* search_buf, std::vector<std::pair<int,std::string>>& results) {
        results.clear();
        if (strlen(search_buf) < 2) return;
        // Try as numeric key first
        int numKey = atoi(search_buf);
        if (numKey > 0) {
            auto it = g_itemDB.find(numKey);
            if (it != g_itemDB.end()) {
                results.push_back({numKey, it->second.name + " [" + it->second.category + "]"});
                return;
            }
        }
        // Text search by name
        std::string needle(search_buf);
        for (auto& c : needle) c = (char)tolower(c);
        for (auto& [key, def] : g_itemDB) {
            std::string lower_name = def.name;
            for (auto& c : lower_name) c = (char)tolower(c);
            if (lower_name.find(needle) != std::string::npos) {
                results.push_back({key, def.name + " [" + def.category + "]"});
                if (results.size() >= 100) break;
            }
        }
    };

    // Helper lambda: determine inventory category from item category string
    auto GetInvCategory = [](const std::string& cat) -> int {
        if (cat == "Currency") return 0;
        if (cat == "Material" || cat == "Equipment") return 1;
        if (cat == "Consumable" || cat == "Potion") return 4;
        if (cat == "Misc") return 1;
        return 1; // default to equipment slot
    };

    // Helper lambda: detect if item is equipment from category
    auto IsEquipment = [](const std::string& cat) -> bool {
        return cat == "Material" || cat == "Equipment";
    };

    // Helper lambda: queue item creation
    auto QueueCreateItem = [&]() {
        std::unordered_map<std::string, uint16_t> ti_map;
        for (size_t i = 0; i < g_tree.parsed.schema.types.size(); i++)
            ti_map[g_tree.parsed.schema.types[i].name] = (uint16_t)i;
        uint64_t maxNo = 1000;
        for (auto& item : g_items) { if (item.itemNo > maxNo) maxNo = item.itemNo; }

        ItemFactory::ItemSpec spec;
        spec.itemKey = (uint32_t)g_createSelectedKey;
        spec.itemNo = maxNo + 1;
        spec.slotNo = 999;
        spec.stackCount = (uint64_t)g_createStack;
        spec.enchantLevel = (uint16_t)g_createEnchant;
        spec.endurance = (uint16_t)g_createEndurance;
        spec.sharpness = (uint16_t)g_createSharpness;
        spec.maxSocketCount = (uint8_t)g_createSockets;
        spec.isNewMark = g_createNewMark;
        // Map dropdown index to ItemMask enum
        static const ItemFactory::ItemMask maskMap[] = {
            ItemFactory::ItemMask::Equipment,
            ItemFactory::ItemMask::EquipmentNoSocket,
            ItemFactory::ItemMask::SimpleConsumable,
            ItemFactory::ItemMask::QuestItem,
        };
        spec.maskType = maskMap[g_createItemType < 4 ? g_createItemType : 1];
        for (int s = 0; s < g_createSockets; s++)
            spec.sockets.push_back({0, 65535});

        auto bytes = ItemFactory::BuildItem(spec, ti_map);
        if (!bytes.empty()) {
            char path[64];
            snprintf(path, sizeof(path), "_inventorylist[%d]._itemList", g_createCategory);
            g_pendingCreatePath = path;
            g_pendingCreateBytes = std::move(bytes);
            g_pendingCreateItem = true;
            g_createItemOpen = false;
            g_customItemOpen = false;
        } else {
            g_statusMsg = "Failed to build item (missing schema types?)";
        }
    };

    // ── ADD ITEM (Quick — auto-populated from game data) ──
    if (g_createItemOpen) {
        ImGui::SetNextWindowSize(ImVec2(550, 420), ImGuiCond_FirstUseEver);
        if (ImGui::Begin("Add Item", &g_createItemOpen, ImGuiWindowFlags_NoCollapse)) {
            ImGui::Text("Search by name or item key:");
            if (ImGui::InputText("##addsearch", g_createSearch, sizeof(g_createSearch))) {
                DoItemSearch(g_createSearch, g_createResults);
            }

            if (!g_createResults.empty()) {
                ImGui::BeginChild("##addresults", ImVec2(0, 180), true);
                for (auto& [key, display] : g_createResults) {
                    char label[256];
                    snprintf(label, sizeof(label), "[%d] %s", key, display.c_str());
                    if (ImGui::Selectable(label, g_createSelectedKey == key)) {
                        g_createSelectedKey = key;
                        snprintf(g_createSelectedName, sizeof(g_createSelectedName), "%s", display.c_str());
                        // Auto-populate from DB
                        auto it = g_itemDB.find(key);
                        if (it != g_itemDB.end()) {
                            g_createStack = (int)std::min(it->second.maxStack, (int64_t)1);
                            g_createCategory = GetInvCategory(it->second.category);
                            if (IsEquipment(it->second.category)) {
                                g_createEnchant = 0;
                                g_createEndurance = 65535;
                                g_createSharpness = 0;
                                g_createSockets = 0;
                            } else {
                                g_createEnchant = 0;
                                g_createEndurance = 65535;
                                g_createSharpness = 0;
                                g_createSockets = 0;
                            }
                        }
                    }
                }
                ImGui::EndChild();
            }

            ImGui::Separator();
            if (g_createSelectedKey > 0) {
                auto it = g_itemDB.find(g_createSelectedKey);
                ImGui::TextColored(ImVec4(1, 0.8f, 0.2f, 1), "Item: [%d] %s", g_createSelectedKey, g_createSelectedName);
                if (it != g_itemDB.end()) {
                    ImGui::Text("Category: %s | Max Stack: %lld | Inv Slot: %d",
                        it->second.category.c_str(), (long long)it->second.maxStack, g_createCategory);
                }
                ImGui::Separator();
                ImGui::SetNextItemWidth(200);
                ImGui::Combo("Item Type", &g_createItemType, ItemFactory::ItemMaskNames, 4);
                ImGui::SetNextItemWidth(150);
                ImGui::InputInt("Quantity", &g_createStack);
                if (g_createStack < 1) g_createStack = 1;

                ImGui::Separator();
                bool canAdd = g_saveLoaded;
                if (!canAdd) ImGui::BeginDisabled();
                if (ImGui::Button("Add to Inventory", ImVec2(200, 35))) {
                    g_createNewMark = true;
                    QueueCreateItem();
                }
                if (!canAdd) ImGui::EndDisabled();
            } else {
                ImGui::TextDisabled("Type a name or key number to search...");
            }
            ImGui::SameLine();
            if (ImGui::Button("Cancel", ImVec2(100, 35))) g_createItemOpen = false;
        }
        ImGui::End();
    }

    // ── CUSTOM ITEM (Full manual control) ──
    if (g_customItemOpen) {
        ImGui::SetNextWindowSize(ImVec2(600, 600), ImGuiCond_FirstUseEver);
        if (ImGui::Begin("Custom Item Builder", &g_customItemOpen, ImGuiWindowFlags_NoCollapse)) {
            ImGui::Text("Search by name or item key:");
            if (ImGui::InputText("##customsearch", g_createSearch, sizeof(g_createSearch))) {
                DoItemSearch(g_createSearch, g_createResults);
            }

            if (!g_createResults.empty()) {
                ImGui::BeginChild("##customresults", ImVec2(0, 130), true);
                for (auto& [key, display] : g_createResults) {
                    char label[256];
                    snprintf(label, sizeof(label), "[%d] %s", key, display.c_str());
                    if (ImGui::Selectable(label, g_createSelectedKey == key)) {
                        g_createSelectedKey = key;
                        snprintf(g_createSelectedName, sizeof(g_createSelectedName), "%s", display.c_str());
                    }
                }
                ImGui::EndChild();
            }

            ImGui::Separator();
            if (g_createSelectedKey > 0) {
                ImGui::TextColored(ImVec4(1, 0.8f, 0.2f, 1), "Selected: [%d] %s", g_createSelectedKey, g_createSelectedName);
            }

            ImGui::Separator();
            ImGui::Text("All Properties (manual):");

            ImGui::SetNextItemWidth(200); ImGui::Combo("Item Type##custom", &g_createItemType, ItemFactory::ItemMaskNames, 4);
            ImGui::SetNextItemWidth(150); ImGui::InputInt("Stack Count", &g_createStack);
            if (g_createStack < 1) g_createStack = 1;
            ImGui::SetNextItemWidth(150); ImGui::SliderInt("Enchant Level", &g_createEnchant, 0, 20);
            ImGui::SetNextItemWidth(150); ImGui::InputInt("Endurance", &g_createEndurance);
            if (g_createEndurance < 0) g_createEndurance = 0;
            if (g_createEndurance > 65535) g_createEndurance = 65535;
            ImGui::SetNextItemWidth(150); ImGui::SliderInt("Sharpness", &g_createSharpness, 0, 100);
            ImGui::SetNextItemWidth(150); ImGui::SliderInt("Socket Slots (0-5)", &g_createSockets, 0, 5);
            ImGui::SetNextItemWidth(150); ImGui::InputInt("Inventory Category", &g_createCategory);
            ImGui::SameLine(); ImGui::TextDisabled("(0=currency, 1=equipment, 4=consumable)");
            if (g_createCategory < 0) g_createCategory = 0;
            if (g_createCategory > 17) g_createCategory = 17;
            ImGui::Checkbox("Mark as New", &g_createNewMark);

            ImGui::Separator();
            bool canCreate = (g_createSelectedKey > 0 && g_saveLoaded);
            if (!canCreate) ImGui::BeginDisabled();
            if (ImGui::Button("Create Item", ImVec2(200, 35))) {
                QueueCreateItem();
            }
            if (!canCreate) ImGui::EndDisabled();
            ImGui::SameLine();
            if (ImGui::Button("Cancel", ImVec2(100, 35))) g_customItemOpen = false;
        }
        ImGui::End();
    }

    // Item table
    if (ImGui::BeginTable("Items", 12,
        ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
        ImGuiTableFlags_ScrollY | ImGuiTableFlags_Resizable |
        ImGuiTableFlags_Sortable,
        ImVec2(0, 0))) {

        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("##icon", ImGuiTableColumnFlags_WidthFixed | ImGuiTableColumnFlags_NoResize, 24);
        ImGui::TableSetupColumn("Name", ImGuiTableColumnFlags_WidthStretch, 200);
        ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 55);
        ImGui::TableSetupColumn("Cat", ImGuiTableColumnFlags_WidthFixed, 70);
        ImGui::TableSetupColumn("Src", ImGuiTableColumnFlags_WidthFixed, 50);
        ImGui::TableSetupColumn("Stack", ImGuiTableColumnFlags_WidthFixed, 90);
        ImGui::TableSetupColumn("Enc", ImGuiTableColumnFlags_WidthFixed, 35);
        ImGui::TableSetupColumn("Dur", ImGuiTableColumnFlags_WidthFixed, 45);
        ImGui::TableSetupColumn("Sw", ImGuiTableColumnFlags_WidthFixed, 30);
        ImGui::TableSetupColumn("Dump", ImGuiTableColumnFlags_WidthFixed, 40);
        ImGui::TableSetupColumn("Load", ImGuiTableColumnFlags_WidthFixed, 40);
        ImGui::TableSetupColumn("+", ImGuiTableColumnFlags_WidthFixed, 22);
        ImGui::TableHeadersRow();

        // Sort by clicked column header
        if (ImGuiTableSortSpecs* specs = ImGui::TableGetSortSpecs()) {
            if (specs->SpecsDirty && specs->SpecsCount > 0) {
                const auto& s = specs->Specs[0];
                int col = s.ColumnIndex;
                bool asc = (s.SortDirection == ImGuiSortDirection_Ascending);
                std::stable_sort(g_items.begin(), g_items.end(),
                    [col, asc](const DisplayItem& a, const DisplayItem& b) {
                        int cmp = 0;
                        switch (col) {
                        case 1: cmp = _stricmp(a.name.c_str(), b.name.c_str()); break;
                        case 2: cmp = (a.itemKey ? a.itemKey : a.itemNo) - (b.itemKey ? b.itemKey : b.itemNo); break;
                        case 3: cmp = _stricmp(a.category.c_str(), b.category.c_str()); break;
                        case 4: cmp = _stricmp(a.source.c_str(), b.source.c_str()); break;
                        case 5: cmp = (a.stackCount < b.stackCount) ? -1 : (a.stackCount > b.stackCount) ? 1 : 0; break;
                        case 6: cmp = a.enchantLevel - b.enchantLevel; break;
                        case 7: cmp = a.endurance - b.endurance; break;
                        default: return false;
                        }
                        return asc ? (cmp < 0) : (cmp > 0);
                    });
                specs->SpecsDirty = false;
            }
        }

        std::string filterLower = filterBuf;
        std::transform(filterLower.begin(), filterLower.end(), filterLower.begin(), ::tolower);

        for (int i = 0; i < (int)g_items.size(); i++) {
            auto& item = g_items[i];

            if (filterBuf[0] != 0) {
                std::string nL = item.name; std::transform(nL.begin(), nL.end(), nL.begin(), ::tolower);
                std::string cL = item.category; std::transform(cL.begin(), cL.end(), cL.begin(), ::tolower);
                std::string sL = item.source; std::transform(sL.begin(), sL.end(), sL.begin(), ::tolower);
                if (nL.find(filterLower) == std::string::npos &&
                    cL.find(filterLower) == std::string::npos &&
                    sL.find(filterLower) == std::string::npos)
                    continue;
            }

            ImGui::TableNextRow(0, 24);
            ImGui::PushID(i);

            // Col 0: Icon
            ImGui::TableSetColumnIndex(0);
            {
                auto* icon = IconCache::Get(item.itemKey);
                if (icon) ImGui::Image((ImTextureID)icon, ImVec2(20, 20));
            }

            // Col 1: Name
            ImGui::TableSetColumnIndex(1);
            if (item.modified)
                ImGui::TextColored(ImVec4(1.0f, 0.8f, 0.2f, 1.0f), "%s", item.name.c_str());
            else
                ImGui::Text("%s", item.name.c_str());

            // Col 2: Key
            ImGui::TableSetColumnIndex(2);
            ImGui::Text("%d", item.itemKey ? item.itemKey : item.itemNo);

            // Col 3: Category
            ImGui::TableSetColumnIndex(3);
            ImGui::Text("%s", item.category.c_str());

            // Col 4: Source
            ImGui::TableSetColumnIndex(4);
            ImGui::TextColored(item.source == "Equipment" ? ImVec4(0.3f,1,0.3f,1) : ImVec4(0.6f,0.6f,0.6f,1),
                "%s", item.source.c_str());

            // Col 5: Stack (editable)
            ImGui::TableSetColumnIndex(5);
            ImGui::SetNextItemWidth(-1);
            int sc = (int)item.stackCount;
            if (ImGui::InputInt("##stk", &sc, 1, 100)) {
                if (sc < 0) sc = 0;
                item.stackCount = sc;
                item.modified = true;
                g_dirty = true;
            }

            // Col 6: Enchant
            ImGui::TableSetColumnIndex(6);
            if (item.enchantLevel > 0) ImGui::Text("+%d", item.enchantLevel);

            // Col 7: Durability
            ImGui::TableSetColumnIndex(7);
            if (item.endurance > 0 && item.endurance < 65535) ImGui::Text("%d", item.endurance);

            // Col 8: Swap
            ImGui::TableSetColumnIndex(8);
            if (item.itemKeyOffset > 0 && ImGui::SmallButton("Sw")) {
                g_swapSelectedItem = i;
                g_swapPopupOpen = true;
                g_swapSearch[0] = 0;
                g_swapResults.clear();
            }

            // Col 9: Dump JSON
            ImGui::TableSetColumnIndex(9);
            if (item.elemStart > 0 && ImGui::SmallButton("D")) {
                DumpItemJson(i);
            }

            // Col 10: Load/Patch JSON
            ImGui::TableSetColumnIndex(10);
            if (item.elemStart > 0 && ImGui::SmallButton("L")) {
                LoadItemJson(i);
            }

            // Col 11: Clone (+)
            ImGui::TableSetColumnIndex(11);
            if (item.elemStart > 0 && ImGui::SmallButton("+")) {
                g_addItemSource = i;
                g_addItemPopup = true;
                g_addItemSearch[0] = 0;
                g_addItemResults.clear();
            }

            ImGui::PopID();
        }

        ImGui::EndTable();
    }

    ImGui::EndChild();
}

// ── Key Lookup (display names for field values) ──
// Maps field_name → { value → display_name }
static std::unordered_map<std::string, std::unordered_map<int64_t, std::string>> g_keyLookup;

static void LoadKeyLookup(const std::string& path) {
    std::ifstream f(path);
    if (!f) return;
    try {
        json j = json::parse(f);

        // Map field names to JSON namespace keys
        struct FieldMapping { const char* jsonKey; std::vector<std::string> fieldNames; };
        FieldMapping mappings[] = {
            {"itemKey", {"_itemKey", "_itemNo", "_spawnItemKey", "_donationItemKey", "_recoveryItemNo"}},
            {"questKey", {"_questKey"}},
            {"knowledgeKey", {"_knowledgeKey"}},
            {"storeKey", {"_storeKey"}},
            {"skillKey", {"_skillKey", "_usableSkillKey"}},
            {"buffKey", {"_buffKey"}},
            {"mercenaryKey", {"_mercenaryNo"}},
            {"stageKey", {"_stageKey"}},
            {"fieldKey", {"_fieldInfoKey", "_spawnFieldInfoKey"}},
            {"factionKey", {"_factionKey"}},
        };

        for (auto& m : mappings) {
            if (!j.contains(m.jsonKey)) continue;
            auto& ns = j[m.jsonKey];
            std::unordered_map<int64_t, std::string> lookup;
            for (auto& [k, v] : ns.items()) {
                try { lookup[std::stoll(k)] = v.get<std::string>(); } catch (...) {}
            }
            for (auto& fn : m.fieldNames) {
                g_keyLookup[fn] = lookup;
            }
        }

        // Character keys need special handling — load from character_names.json instead
        // (key_lookup_index doesn't have characterKey)
    } catch (...) {}
}

static void LoadCharacterLookup(const std::string& path) {
    std::ifstream f(path);
    if (!f) return;
    try {
        json j = json::parse(f);
        std::unordered_map<int64_t, std::string> lookup;
        for (auto& [k, v] : j.items()) {
            try {
                std::string name;
                if (v.is_object() && v.contains("clean"))
                    name = v["clean"].get<std::string>();
                else if (v.is_string())
                    name = v.get<std::string>();
                if (!name.empty())
                    lookup[std::stoll(k)] = name;
            } catch (...) {}
        }
        g_keyLookup["_characterKey"] = lookup;
        g_keyLookup["_ownedCharacterKey"] = lookup;
    } catch (...) {}
}

static const char* LookupFieldName(const std::string& fieldName, int64_t value) {
    auto it = g_keyLookup.find(fieldName);
    if (it == g_keyLookup.end()) return nullptr;
    auto it2 = it->second.find(value);
    if (it2 == it->second.end()) return nullptr;
    return it2->second.c_str();
}

// ── Save Tree Browser ──

// (g_treeSearch, g_activeSearch, g_navTargetOffset, g_navPending declared at top)

// Tree clipboard for copy/paste operations
static std::vector<uint8_t> g_clipboardBytes;
static std::string g_clipboardType;
static uint32_t g_clipboardSize = 0;
static std::string g_clipboardLabel;

// Dual tree view — source save for cross-save operations
static ParcEngine::SaveTree g_srcTree;
static bool g_srcLoaded = false;
static std::string g_srcPath;

// Selection state for dual tree
static uint32_t g_srcSelectStart = 0;
static uint32_t g_srcSelectEnd = 0;
static std::string g_srcSelectLabel;
static uint32_t g_dstSelectStart = 0;
static uint32_t g_dstSelectEnd = 0;
static std::string g_dstSelectLabel;

static void RenderFieldValue(SaveParserCpp::GenericFieldValue& f, const std::vector<uint8_t>& blob) {
    if (!f.present) {
        ImGui::TextDisabled("(absent)");
        return;
    }

    // For scalar fields with known offsets, show editable value
    if (f.meta_kind <= 3 && f.start_offset > 0 && f.end_offset > f.start_offset) {
        uint32_t sz = f.end_offset - f.start_offset;
        if (sz == 1) {
            int v = blob[f.start_offset];
            ImGui::SetNextItemWidth(80);
            if (ImGui::InputInt("##v", &v, 1, 10)) {
                if (v >= 0 && v <= 255) {
                    const_cast<std::vector<uint8_t>&>(blob)[f.start_offset] = (uint8_t)v;
                    g_dirty = true;
                }
            }
        } else if (sz == 2) {
            uint16_t raw = 0; memcpy(&raw, blob.data() + f.start_offset, 2);
            int v = raw;
            ImGui::SetNextItemWidth(80);
            if (ImGui::InputInt("##v", &v, 1, 100)) {
                if (v >= 0 && v <= 65535) {
                    uint16_t nv = (uint16_t)v;
                    const_cast<std::vector<uint8_t>&>(blob)[f.start_offset] = nv & 0xFF;
                    const_cast<std::vector<uint8_t>&>(blob)[f.start_offset+1] = (nv >> 8) & 0xFF;
                    g_dirty = true;
                }
            }
        } else if (sz == 4) {
            uint32_t raw = 0; memcpy(&raw, blob.data() + f.start_offset, 4);
            int v = (int)raw;
            ImGui::SetNextItemWidth(120);
            if (ImGui::InputInt("##v", &v, 1, 100)) {
                uint32_t nv = (uint32_t)v;
                memcpy(const_cast<uint8_t*>(blob.data()) + f.start_offset, &nv, 4);
                g_dirty = true;
            }
        } else if (sz == 8) {
            int64_t raw = 0; memcpy(&raw, blob.data() + f.start_offset, 8);
            int v = (int)raw;
            ImGui::SetNextItemWidth(120);
            if (ImGui::InputInt("##v", &v, 1, 100)) {
                int64_t nv = (int64_t)v;
                memcpy(const_cast<uint8_t*>(blob.data()) + f.start_offset, &nv, 8);
                g_dirty = true;
            }
        } else {
            // Hex dump for other sizes
            ImGui::TextDisabled("%u bytes", sz);
        }
        // Display name lookup
        if (f.start_offset > 0 && f.end_offset > f.start_offset) {
            uint32_t fsz = f.end_offset - f.start_offset;
            if (fsz <= 8 && f.start_offset + fsz <= blob.size()) {
                int64_t fval = 0;
                memcpy(&fval, blob.data() + f.start_offset, fsz);
                const char* displayName = LookupFieldName(f.name, fval);
                if (displayName) {
                    ImGui::SameLine();
                    ImGui::TextColored(ImVec4(1.0f, 0.9f, 0.3f, 1.0f), "(%s)", displayName);
                }
            }
        }
        ImGui::SameLine();
        ImGui::TextDisabled("[0x%X..0x%X]", f.start_offset, f.end_offset);
    } else if (f.meta_kind == 4 || f.meta_kind == 5) {
        ImGui::TextDisabled("type=%s", f.child_type_name.c_str());
        ImGui::SameLine();
        ImGui::TextDisabled("[0x%X..0x%X]", f.start_offset, f.end_offset);
    } else if (f.meta_kind == 6 || f.meta_kind == 7) {
        ImGui::Text("%zu elements", f.list_elements.size());
        ImGui::SameLine();
        ImGui::TextDisabled("prefix=%u [0x%X..0x%X]", f.list_prefix_u8, f.start_offset, f.end_offset);
    } else {
        ImGui::TextDisabled("%s", f.value_repr.c_str());
    }
}

static bool MatchesSearch(const std::string& name) {
    if (g_activeSearch[0] == 0) return true;
    std::string lower = name;
    std::string search = g_activeSearch;
    std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);
    std::transform(search.begin(), search.end(), search.begin(), ::tolower);
    return lower.find(search) != std::string::npos;
}

// Check if a field's VALUE matches the search (for numeric/hex/string searches)
static bool FieldValueMatchesSearch(const SaveParserCpp::GenericFieldValue& f,
                                     const std::vector<uint8_t>& blob) {
    if (g_activeSearch[0] == 0) return false;
    if (!f.present) return false;
    if (f.meta_kind > 3) return false;
    if (f.start_offset == 0 || f.end_offset == 0) return false;
    if (f.end_offset <= f.start_offset) return false;

    uint32_t sz = f.end_offset - f.start_offset;
    if (sz > 8) return false;
    if (f.start_offset + sz > blob.size()) return false;

    std::string search = g_activeSearch;

    // Try as integer
    bool isNum = true;
    for (size_t ci = 0; ci < search.size(); ci++) {
        char c = search[ci];
        if (!isdigit(c) && c != '-') { isNum = false; break; }
    }
    if (isNum && !search.empty()) {
        int64_t searchVal = _atoi64(search.c_str());
        uint64_t fieldVal = 0;
        memcpy(&fieldVal, blob.data() + f.start_offset, sz);
        if ((int64_t)fieldVal == searchVal) return true;
    }

    // Try as hex
    if (search.size() > 2 && search[0] == '0' && (search[1] == 'x' || search[1] == 'X')) {
        uint64_t hexVal = strtoull(search.c_str() + 2, nullptr, 16);
        uint64_t fieldVal = 0;
        memcpy(&fieldVal, blob.data() + f.start_offset, sz);
        if (fieldVal == hexVal) return true;
    }

    // Try matching display name — if this field has a lookup name, check if search matches it
    if (!isNum) {
        uint64_t fieldVal = 0;
        memcpy(&fieldVal, blob.data() + f.start_offset, sz);
        const char* displayName = LookupFieldName(f.name, (int64_t)fieldVal);
        if (displayName) {
            std::string dnLower = displayName;
            std::transform(dnLower.begin(), dnLower.end(), dnLower.begin(), ::tolower);
            std::string searchLower = search;
            std::transform(searchLower.begin(), searchLower.end(), searchLower.begin(), ::tolower);
            if (dnLower.find(searchLower) != std::string::npos) return true;
        }
    }

    return false;
}

// Deep search with result caching — runs ONCE when search changes, stores results
// (g_searchHits declared at top)
static bool g_searchDirty = true;

static FILE* g_logFile = nullptr;
static void LogMsg(const char* fmt, ...) {
    if (!g_logFile) {
        g_logFile = fopen("editor_debug.log", "a");
        if (!g_logFile) return;
    }
    SYSTEMTIME st; GetLocalTime(&st);
    fprintf(g_logFile, "[%02d:%02d:%02d] ", st.wHour, st.wMinute, st.wSecond);
    va_list args; va_start(args, fmt);
    vfprintf(g_logFile, fmt, args);
    va_end(args);
    fprintf(g_logFile, "\n");
    fflush(g_logFile);
}

static void RebuildSearchIndex(const std::vector<uint8_t>& blob) {
    LogMsg("RebuildSearchIndex START: search='%s' blob=%zu", g_activeSearch, blob.size());
    g_searchHits.clear();
    if (g_activeSearch[0] == 0) { LogMsg("Empty search, returning"); return; }

    // Walk ALL fields iteratively using index-based traversal (no pointer vector growth)
    // Phase 1: collect all leaf/scalar fields and check them
    int totalNodes = 0;
    int directHits = 0;
    LogMsg("Walking tree (index-based)...");

    for (size_t oi = 0; oi < g_tree.parsed.objects.size(); oi++) {
        auto& obj = g_tree.parsed.objects[oi];
        if (obj.fields.empty()) continue;

        struct Frame { const std::vector<SaveParserCpp::GenericFieldValue>* vec; size_t idx; };
        Frame fstack[256]; // fixed-size stack, no heap allocation
        int fsp = 0;
        fstack[0] = {&obj.fields, 0};

        while (fsp >= 0) {
            auto& top = fstack[fsp];
            if (top.idx >= top.vec->size()) {
                fsp--;
                continue;
            }
            const auto& f = (*top.vec)[top.idx];
            top.idx++;
            totalNodes++;

            if (MatchesSearch(f.name) || MatchesSearch(f.type_name) || FieldValueMatchesSearch(f, blob)) {
                g_searchHits.insert(&f);
                directHits++;
            }

            // Push children (check stack depth)
            if (!f.list_elements.empty() && fsp < 254) {
                fsp++;
                fstack[fsp] = {&f.list_elements, 0};
            }
            if (!f.child_fields.empty() && fsp < 254) {
                fsp++;
                fstack[fsp] = {&f.child_fields, 0};
            }
        }

        if (oi % 200 == 0)
            LogMsg("  objects %zu/%zu, nodes so far: %d, hits: %d",
                oi, g_tree.parsed.objects.size(), totalNodes, directHits);
    }
    LogMsg("Walk done: %d nodes, %d direct hits", totalNodes, directHits);

    // Mark ancestors using the same index-based approach
    // For each object, do a post-order walk: returns true if any descendant is a hit
    LogMsg("Ancestor pass...");
    {
        struct AncFrame {
            const std::vector<SaveParserCpp::GenericFieldValue>* vec;
            size_t idx;
            const SaveParserCpp::GenericFieldValue* parent;
        };

        for (auto& obj : g_tree.parsed.objects) {
            // Skip objects with no hits at all (optimization)
            bool anyHit = false;
            for (auto& f : obj.fields) {
                if (g_searchHits.count(&f)) { anyHit = true; break; }
            }
            // Even if no direct field hit, descendants might have hits
            // But checking all descendants is what we're trying to do here...
            // Simple approach: for each field, check if it or any child is in hits
            // Use a recursive-style post-order with the index stack

            struct POFrame { const SaveParserCpp::GenericFieldValue* field; };
            // Just iterate and propagate upward via parent pointers
            // Actually simplest: if directHits == 0, skip entirely
        }

        // Simpler approach: for each direct hit, walk up manually
        // But we don't have parent pointers...
        // Easiest: re-walk, check children, propagate
        for (auto& obj : g_tree.parsed.objects) {
            struct WFrame { const std::vector<SaveParserCpp::GenericFieldValue>* vec; size_t idx; };
            std::vector<WFrame> ws;
            ws.reserve(64);
            ws.push_back({&obj.fields, 0});

            // Post-order: process children first, then parent
            // Use a two-pass: first push all, then pop and check
            // Actually just use the simple rule: if ANY child_field or list_element is in hits, add self
            // Walk top-down, but repeat until no new hits added
            bool changed = true;
            int passes = 0;
            while (changed && passes < 20) {
                changed = false;
                passes++;
                ws.clear();
                ws.push_back({&obj.fields, 0});
                while (!ws.empty()) {
                    auto& top = ws.back();
                    if (top.idx >= top.vec->size()) { ws.pop_back(); continue; }
                    const auto& f = (*top.vec)[top.idx];
                    top.idx++;
                    bool childHit = false;
                    for (auto& cf : f.child_fields) if (g_searchHits.count(&cf)) childHit = true;
                    for (auto& el : f.list_elements) if (g_searchHits.count(&el)) childHit = true;
                    if (childHit && !g_searchHits.count(&f)) {
                        g_searchHits.insert(&f);
                        changed = true;
                    }
                    if (!f.list_elements.empty()) ws.push_back({&f.list_elements, 0});
                    if (!f.child_fields.empty()) ws.push_back({&f.child_fields, 0});
                }
            }
        }
    }
    LogMsg("Search complete: %zu total hits (direct+ancestors)", g_searchHits.size());

    g_searchDirty = false;
}

static bool AnyDescendantMatches(const SaveParserCpp::GenericFieldValue& f,
                                  const std::vector<uint8_t>&) {
    return g_searchHits.count(&f) > 0;
}


static void RenderFieldTree(std::vector<SaveParserCpp::GenericFieldValue>& fields,
                             const std::vector<uint8_t>& blob, int depth = 0,
                             const std::string& parentPath = "") {
    if (depth > 20) {
        ImGui::TextDisabled("(max depth reached)");
        return;
    }
    for (size_t i = 0; i < fields.size(); i++) {
        auto& f = fields[i];
        ImGui::PushID((int)(depth * 10000 + i));

        // XML node path: elements are named "[i]" and append without a dot
        std::string nodePath = parentPath;
        if (!f.name.empty() && f.name[0] == '[') nodePath += f.name;
        else nodePath += (parentPath.empty() ? "" : ".") + f.name;

        bool hasChildren = !f.child_fields.empty() || !f.list_elements.empty();
        bool matchSelf = MatchesSearch(f.name) || MatchesSearch(f.type_name) || FieldValueMatchesSearch(f, blob);
        bool matchAny = matchSelf;
        if (!matchAny && hasChildren) matchAny = AnyDescendantMatches(f, blob);

        // Skip if search active and nothing matches
        if (g_activeSearch[0] != 0 && !matchAny) {
            ImGui::PopID();
            continue;
        }

        ImGuiTreeNodeFlags flags = ImGuiTreeNodeFlags_SpanAvailWidth;
        if (!hasChildren) flags |= ImGuiTreeNodeFlags_Leaf | ImGuiTreeNodeFlags_NoTreePushOnOpen;

        // Auto-open nodes when searching and a descendant matches
        if (g_activeSearch[0] != 0 && hasChildren && matchAny)
            flags |= ImGuiTreeNodeFlags_DefaultOpen;

        // Auto-open when navigating to a target inside this field's range
        if (g_navPending && f.start_offset > 0 && f.end_offset > 0 &&
            g_navTargetOffset >= f.start_offset && g_navTargetOffset < f.end_offset)
            flags |= ImGuiTreeNodeFlags_DefaultOpen;

        // Color: green for value match, blue for objects, grey for absent
        bool valueMatch = FieldValueMatchesSearch(f, blob);
        if (valueMatch)
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.2f, 1.0f, 0.2f, 1.0f));
        else if (!f.present)
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.5f, 0.5f, 0.5f, 1.0f));
        else if (f.meta_kind >= 4)
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.5f, 0.8f, 1.0f, 1.0f));
        else
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(1.0f, 1.0f, 1.0f, 1.0f));

        // Label
        char label[256];
        if (f.meta_kind == 6 || f.meta_kind == 7)
            snprintf(label, sizeof(label), "%s [%zu]###f%zu", f.name.c_str(), f.list_elements.size(), i);
        else
            snprintf(label, sizeof(label), "%s (%s)###f%zu", f.name.c_str(), f.type_name.c_str(), i);

        bool open = hasChildren ? ImGui::TreeNodeEx(label, flags) : false;
        if (!hasChildren) ImGui::TreeNodeEx(label, flags); // leaf — just render

        ImGui::PopStyleColor();

        // Right-click context menu
        if (ImGui::BeginPopupContextItem("##ctx")) {
            if (ImGui::MenuItem("Go to (clear search)")) {
                g_navTargetOffset = f.start_offset;
                g_navPending = true;
                g_treeSearch[0] = 0;
                g_activeSearch[0] = 0;
                g_searchHits.clear();
            }
            if (f.present && f.start_offset > 0) {
                char offsetStr[32];
                snprintf(offsetStr, sizeof(offsetStr), "0x%X", f.start_offset);
                if (ImGui::MenuItem("Copy offset")) {
                    ImGui::SetClipboardText(offsetStr);
                }
            }
            if (f.present && f.meta_kind <= 3 && f.start_offset > 0 && f.end_offset > f.start_offset) {
                uint32_t sz2 = f.end_offset - f.start_offset;
                if (sz2 <= 8 && f.start_offset + sz2 <= blob.size()) {
                    uint64_t val = 0;
                    memcpy(&val, blob.data() + f.start_offset, sz2);
                    char valStr[32];
                    snprintf(valStr, sizeof(valStr), "%llu", (unsigned long long)val);
                    if (ImGui::MenuItem("Copy value")) {
                        ImGui::SetClipboardText(valStr);
                    }
                }
            }

            // Fix list count — for list fields where element count doesn't match header
            if (f.present && (f.meta_kind == 6 || f.meta_kind == 7) && f.start_offset > 0) {
                uint32_t parsedCount = (uint32_t)f.list_elements.size();
                // Scan near start_offset for the count value
                bool countFound = false;
                for (uint32_t probe = f.start_offset; probe < f.start_offset + 8 && probe + 4 <= blob.size(); probe++) {
                    uint32_t le24 = blob[probe] | (blob[probe+1] << 8) | (blob[probe+2] << 16);
                    if (le24 == parsedCount || le24 == parsedCount - 1 || le24 == parsedCount + 1) {
                        countFound = true;
                        char countLabel[128];
                        snprintf(countLabel, sizeof(countLabel), "Set count to %u at 0x%X (current: %u)",
                            parsedCount + 1, probe, le24);
                        if (ImGui::MenuItem(countLabel)) {
                            uint32_t newCount = parsedCount + 1;
                            const_cast<std::vector<uint8_t>&>(blob)[probe] = newCount & 0xFF;
                            const_cast<std::vector<uint8_t>&>(blob)[probe+1] = (newCount >> 8) & 0xFF;
                            const_cast<std::vector<uint8_t>&>(blob)[probe+2] = (newCount >> 16) & 0xFF;
                            g_dirty = true;
                            g_statusMsg = "List count patched to " + std::to_string(newCount);
                            LogMsg("LIST COUNT: patched at 0x%X: %u -> %u", probe, le24, newCount);
                        }
                        break;
                    }
                }
                ImGui::Separator();
            }

            // Copy/Paste raw bytes
            if (f.present && f.start_offset > 0 && f.end_offset > f.start_offset &&
                f.end_offset <= blob.size()) {
                uint32_t nodeSize = f.end_offset - f.start_offset;
                ImGui::Separator();

                // Copy
                char copyLabel[64];
                snprintf(copyLabel, sizeof(copyLabel), "Copy (%u bytes)", nodeSize);
                if (ImGui::MenuItem(copyLabel)) {
                    g_clipboardBytes.assign(blob.begin() + f.start_offset, blob.begin() + f.end_offset);
                    g_clipboardType = f.child_type_name.empty() ? f.type_name : f.child_type_name;
                    g_clipboardSize = nodeSize;
                    g_clipboardLabel = f.name + " (" + g_clipboardType + ")";
                    g_statusMsg = "Copied " + std::to_string(nodeSize) + " bytes: " + g_clipboardLabel;
                }

                // Paste (same size = safe overwrite, different size = warn)
                if (!g_clipboardBytes.empty()) {
                    bool sameSize = (g_clipboardSize == nodeSize);
                    bool sameType = (g_clipboardType == (f.child_type_name.empty() ? f.type_name : f.child_type_name));

                    char pasteLabel[128];
                    if (sameSize) {
                        snprintf(pasteLabel, sizeof(pasteLabel), "Paste (overwrite %u bytes from: %s)",
                            g_clipboardSize, g_clipboardLabel.c_str());
                    } else {
                        snprintf(pasteLabel, sizeof(pasteLabel), "Paste (%u -> %u bytes, RESIZE) from: %s",
                            nodeSize, g_clipboardSize, g_clipboardLabel.c_str());
                    }

                    if (sameSize) {
                        if (ImGui::MenuItem(pasteLabel)) {
                            // Same size: direct overwrite, no structural changes
                            memcpy(const_cast<uint8_t*>(blob.data()) + f.start_offset,
                                   g_clipboardBytes.data(), g_clipboardSize);

                            // Fix POs inside the pasted region to be self-referential
                            static const uint8_t SENT8[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
                            for (uint32_t p = f.start_offset; p + 12 <= f.end_offset; ++p) {
                                if (memcmp(blob.data() + p, SENT8, 8) == 0) {
                                    uint32_t po_pos = p + 8;
                                    uint32_t correct = po_pos + 4;
                                    memcpy(const_cast<uint8_t*>(blob.data()) + po_pos, &correct, 4);
                                }
                            }

                            g_dirty = true;
                            g_statusMsg = "Pasted " + std::to_string(g_clipboardSize) + " bytes (same size, safe)";
                            if (!sameType) g_statusMsg += " WARNING: different type!";
                        }
                    } else {
                        // Different size — use deferred ReplaceElement
                        if (ImGui::MenuItem(pasteLabel)) {
                            // Copy clipboard bytes and fix POs for target position
                            std::vector<uint8_t> pasteBytes = g_clipboardBytes;

                            // Find block class for this element
                            std::string pasteBlock;
                            for (auto& obj3 : g_tree.parsed.objects) {
                                if (f.start_offset >= obj3.data_offset &&
                                    f.start_offset < obj3.data_offset + obj3.data_size) {
                                    pasteBlock = obj3.class_name;
                                    break;
                                }
                            }

                            if (!pasteBlock.empty()) {
                                g_pendingDuplicate = true;
                                g_pendingDupBlock = pasteBlock;
                                g_pendingDupField = "_REPLACE_";
                                g_pendingDupBytes = pasteBytes;
                                g_pendingDupSrcOffset = f.start_offset;
                                g_pendingReplaceEnd = f.end_offset;
                                g_statusMsg = "Pasting " + std::to_string(g_clipboardSize) +
                                    " bytes (resize " + std::to_string(nodeSize) + " -> " +
                                    std::to_string(g_clipboardSize) + ")...";
                            } else {
                                g_statusMsg = "Could not find block for paste target";
                            }
                        }
                    }
                }

                // Paste as JSON to clipboard (for external editing)
                if (ImGui::MenuItem("Copy as Hex to clipboard")) {
                    std::string hex;
                    for (uint32_t bi = f.start_offset; bi < f.end_offset && bi < blob.size(); bi++) {
                        char hb[4]; snprintf(hb, sizeof(hb), "%02X", blob[bi]);
                        hex += hb;
                    }
                    ImGui::SetClipboardText(hex.c_str());
                    g_statusMsg = "Copied " + std::to_string(nodeSize) + " bytes as hex";
                }
            }

            // Duplicate element — for list elements (decode_kind contains "list_element")
            if (f.present && f.start_offset > 0 && f.end_offset > f.start_offset &&
                (f.decode_kind == "list_element" || f.decode_kind.find("locator") != std::string::npos)) {
                ImGui::Separator();
                if (ImGui::MenuItem("Duplicate this element")) {
                    // Copy this element's raw bytes from blob
                    uint32_t elemSize = f.end_offset - f.start_offset;
                    std::vector<uint8_t> elemBytes(
                        g_tree.blob.begin() + f.start_offset,
                        g_tree.blob.begin() + f.end_offset);

                    // Find which list this element belongs to — search parent
                    // We need the block class and list field name for SpliceIntoList
                    // Walk parsed objects to find the containing list
                    std::string spliceBlock, spliceField;
                    uint32_t listStart = 0;
                    uint32_t listCount = 0;
                    for (auto& obj : g_tree.parsed.objects) {
                        bool found = false;
                        for (auto& fld : obj.fields) {
                            if (fld.meta_kind != 6 && fld.meta_kind != 7) continue;
                            for (auto& el : fld.list_elements) {
                                if (el.start_offset == f.start_offset) {
                                    spliceBlock = obj.class_name;
                                    spliceField = fld.name;
                                    listStart = fld.start_offset;
                                    listCount = (uint32_t)fld.list_elements.size();
                                    found = true;
                                    break;
                                }
                            }
                            if (found) break;
                            // Check nested lists
                            for (auto& el : fld.list_elements) {
                                for (auto& cf : el.child_fields) {
                                    if (cf.meta_kind != 6 && cf.meta_kind != 7) continue;
                                    for (auto& nel : cf.list_elements) {
                                        if (nel.start_offset == f.start_offset) {
                                            spliceBlock = obj.class_name;
                                            spliceField = cf.name;
                                            listStart = cf.start_offset;
                                            listCount = (uint32_t)cf.list_elements.size();
                                            found = true;
                                            break;
                                        }
                                    }
                                    if (found) break;
                                }
                                if (found) break;
                            }
                            if (found) break;
                        }
                        if (found) break;
                    }

                    if (!spliceBlock.empty()) {
                        // Auto-increment _mercenaryNo: find the highest existing one
                        uint32_t maxMercNo = 0;
                        for (auto& obj2 : g_tree.parsed.objects) {
                            if (obj2.class_name.find("MercenaryClan") == std::string::npos) continue;
                            for (auto& fld2 : obj2.fields) {
                                if (fld2.name != "_mercenaryDataList") continue;
                                for (auto& el2 : fld2.list_elements) {
                                    for (auto& cf2 : el2.child_fields) {
                                        if (cf2.name == "_mercenaryNo" && cf2.present &&
                                            cf2.start_offset > 0 && cf2.end_offset > cf2.start_offset) {
                                            uint32_t sz2 = cf2.end_offset - cf2.start_offset;
                                            if (sz2 <= 8 && cf2.start_offset + sz2 <= g_tree.blob.size()) {
                                                uint64_t mno = 0;
                                                memcpy(&mno, g_tree.blob.data() + cf2.start_offset, sz2);
                                                if ((uint32_t)mno > maxMercNo) maxMercNo = (uint32_t)mno;
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        uint32_t newMercNo = maxMercNo + 1;
                        LogMsg("DUPLICATE: maxMercNo=%u, new=%u", maxMercNo, newMercNo);

                        // Find _mercenaryNo offset within the element bytes and patch it
                        bool mercNoPatched = false;
                        for (auto& cf3 : f.child_fields) {
                            if (cf3.name == "_mercenaryNo" && cf3.present &&
                                cf3.start_offset >= f.start_offset && cf3.end_offset > cf3.start_offset) {
                                uint32_t relOff = cf3.start_offset - f.start_offset;
                                uint32_t sz3 = cf3.end_offset - cf3.start_offset;
                                LogMsg("DUPLICATE: found _mercenaryNo at elem+%u, size=%u", relOff, sz3);
                                if (relOff + sz3 <= elemBytes.size() && sz3 <= 8) {
                                    uint64_t newVal64 = (uint64_t)newMercNo;
                                    memcpy(elemBytes.data() + relOff, &newVal64, sz3);
                                    mercNoPatched = true;
                                    LogMsg("DUPLICATE: patched _mercenaryNo: %u (wrote %u bytes)", newMercNo, sz3);
                                }
                                break;
                            }
                        }
                        if (!mercNoPatched) {
                            LogMsg("DUPLICATE: WARNING - _mercenaryNo NOT FOUND in child_fields!");
                            LogMsg("DUPLICATE: child_fields count: %zu", f.child_fields.size());
                            for (size_t cfi = 0; cfi < f.child_fields.size() && cfi < 10; cfi++) {
                                LogMsg("DUPLICATE:   cf[%zu] = '%s' present=%d",
                                    cfi, f.child_fields[cfi].name.c_str(), f.child_fields[cfi].present);
                            }
                        }

                        // Defer splice to OUTSIDE the render loop
                        g_pendingDuplicate = true;
                        g_pendingDupBlock = spliceBlock;
                        g_pendingDupField = spliceField;
                        g_pendingDupBytes = elemBytes;
                        g_pendingDupSrcOffset = f.start_offset;
                        g_statusMsg = "Duplicating...";
                        LogMsg("DUPLICATE: deferred (block=%s field=%s size=%u)",
                            spliceBlock.c_str(), spliceField.c_str(), (uint32_t)elemBytes.size());
                    } else {
                        g_statusMsg = "Could not find parent list for this element";
                    }
                }
            }

            // XML export/import — works on any present node at any depth
            if (f.present && !nodePath.empty()) {
                ImGui::Separator();
                if (ImGui::MenuItem("Export node as XML...")) {
                    char xpath[MAX_PATH] = {};
                    // suggest a filename from the node path
                    std::string suggest;
                    for (char c : nodePath) {
                        suggest += (isalnum((unsigned char)c) ? c : '_');
                    }
                    suggest += ".xml";
                    strncpy(xpath, suggest.c_str(), MAX_PATH - 1);
                    OPENFILENAMEA ofn = {};
                    ofn.lStructSize = sizeof(ofn);
                    ofn.lpstrFilter = "XML files (*.xml)\0*.xml\0All files\0*.*\0";
                    ofn.lpstrFile = xpath;
                    ofn.nMaxFile = MAX_PATH;
                    ofn.Flags = OFN_OVERWRITEPROMPT;
                    ofn.lpstrTitle = "Export Node as XML";
                    if (GetSaveFileNameA(&ofn)) {
                        auto err = ParcXml::ExportXml(g_tree, xpath, nodePath);
                        g_statusMsg = err.empty()
                            ? ("Exported XML: " + nodePath) : ("XML export failed: " + err);
                        LogMsg("XML_EXPORT: %s -> %s (%s)", nodePath.c_str(), xpath,
                               err.empty() ? "ok" : err.c_str());
                    }
                }
                if (ImGui::MenuItem("Import XML here (replace this node)...")) {
                    char xpath[MAX_PATH] = {};
                    OPENFILENAMEA ofn = {};
                    ofn.lStructSize = sizeof(ofn);
                    ofn.lpstrFilter = "XML files (*.xml)\0*.xml\0All files\0*.*\0";
                    ofn.lpstrFile = xpath;
                    ofn.nMaxFile = MAX_PATH;
                    ofn.Flags = OFN_FILEMUSTEXIST;
                    ofn.lpstrTitle = "Import XML Node (replaces this node)";
                    if (GetOpenFileNameA(&ofn)) {
                        g_pendingXmlNodeImport = true;
                        g_pendingXmlNodeFile = xpath;
                        g_pendingXmlNodePath = nodePath;
                        g_statusMsg = "Importing XML node...";
                    }
                }
                if ((f.meta_kind == 6 || f.meta_kind == 7) &&
                    ImGui::MenuItem("Append element from XML...")) {
                    char xpath[MAX_PATH] = {};
                    OPENFILENAMEA ofn = {};
                    ofn.lStructSize = sizeof(ofn);
                    ofn.lpstrFilter = "XML files (*.xml)\0*.xml\0All files\0*.*\0";
                    ofn.lpstrFile = xpath;
                    ofn.nMaxFile = MAX_PATH;
                    ofn.Flags = OFN_FILEMUSTEXIST;
                    ofn.lpstrTitle = "Append Element from XML (<e> file)";
                    if (GetOpenFileNameA(&ofn)) {
                        g_pendingXmlNodeImport = true;
                        g_pendingXmlNodeFile = xpath;
                        g_pendingXmlNodePath = nodePath + "[" +
                            std::to_string(f.list_elements.size()) + "]";
                        g_statusMsg = "Appending XML element...";
                    }
                }
            }

            ImGui::EndPopup();
        }

        // Value on same line
        if (f.present && (f.meta_kind <= 3)) {
            ImGui::SameLine(ImGui::GetWindowWidth() * 0.45f);
            RenderFieldValue(f, blob);
        } else if (f.present && (f.meta_kind == 6 || f.meta_kind == 7)) {
            ImGui::SameLine(ImGui::GetWindowWidth() * 0.45f);
            RenderFieldValue(f, blob);
        }

        // Auto-scroll to navigation target
        if (g_navPending && f.start_offset == g_navTargetOffset && g_navTargetOffset > 0) {
            ImGui::SetScrollHereY(0.3f);
            g_navPending = false;
        }

        if (open && hasChildren) {
            if (!f.child_fields.empty())
                RenderFieldTree(f.child_fields, blob, depth + 1, nodePath);
            if (!f.list_elements.empty()) {
                size_t count = f.list_elements.size();
                // For large lists, paginate to avoid stack overflow on render
                auto& listPage = g_listPage;
                size_t pageSize = 50;
                size_t& page = listPage[&f];
                size_t startIdx = page * pageSize;
                size_t endIdx = startIdx + pageSize;
                if (endIdx > count) endIdx = count;

                if (count > pageSize) {
                    ImGui::Text("Showing %zu-%zu of %zu", startIdx, endIdx, count);
                    ImGui::SameLine();
                    if (startIdx > 0 && ImGui::SmallButton("<< Prev")) page--;
                    ImGui::SameLine();
                    if (endIdx < count && ImGui::SmallButton("Next >>")) page++;
                    ImGui::SameLine();
                    // Jump to last page (where new duplicate would be)
                    size_t lastPage = (count - 1) / pageSize;
                    if (page != lastPage && ImGui::SmallButton("Last")) page = lastPage;

                    // Render only current page
                    std::vector<SaveParserCpp::GenericFieldValue> pageElems(
                        f.list_elements.begin() + startIdx,
                        f.list_elements.begin() + endIdx);
                    RenderFieldTree(pageElems, blob, depth + 1, nodePath);
                } else {
                    RenderFieldTree(f.list_elements, blob, depth + 1, nodePath);
                }
            }
            ImGui::TreePop();
        }

        ImGui::PopID();
    }
}

// Whole-save XML import: pick the XML, pick where to write the rebuilt .save,
// then defer the actual work to the main loop (outside the render pass).
static void DrawImportSaveXmlButton() {
    if (ImGui::SmallButton("Import Save from XML")) {
        char xpath[MAX_PATH] = {};
        OPENFILENAMEA ofn = {};
        ofn.lStructSize = sizeof(ofn);
        ofn.lpstrFilter = "XML files (*.xml)\0*.xml\0All files\0*.*\0";
        ofn.lpstrFile = xpath;
        ofn.nMaxFile = MAX_PATH;
        ofn.Flags = OFN_FILEMUSTEXIST;
        ofn.lpstrTitle = "Import Whole Save from XML";
        if (GetOpenFileNameA(&ofn)) {
            char opath[MAX_PATH] = "save_imported.save";
            OPENFILENAMEA sfn = {};
            sfn.lStructSize = sizeof(sfn);
            sfn.lpstrFilter = "Save files (*.save)\0*.save\0All files\0*.*\0";
            sfn.lpstrFile = opath;
            sfn.nMaxFile = MAX_PATH;
            sfn.Flags = OFN_OVERWRITEPROMPT;
            sfn.lpstrTitle = "Write Rebuilt Save As";
            if (GetSaveFileNameA(&sfn)) {
                g_pendingXmlSaveImport = true;
                g_pendingXmlSaveFile = xpath;
                g_pendingXmlSaveOut = opath;
                g_statusMsg = "Importing save from XML...";
            }
        }
    }
    if (ImGui::IsItemHovered()) {
        ImGui::SetTooltip("Rebuild a full .save from a whole-save XML export.\n"
                          "Writes to a new file, then loads it in the editor.\n"
                          "Refuses to write if the rebuilt blob fails self-check.");
    }
    ImGui::SameLine();
    ImGui::Checkbox("+ lobby.save", &g_xmlImportWriteLobby);
    if (ImGui::IsItemHovered()) {
        ImGui::SetTooltip("Also generate a lobby.save next to the imported save.\n"
                          "The game needs lobby.save + save.save in a slot folder,\n"
                          "but lobby.save does not have to match the save — so the\n"
                          "pair is drag-and-drop ready for any save slot.");
    }
}

static void RenderSaveTree() {
    ImGui::BeginChild("TreeArea", ImVec2(0, 0), false);

    if (!g_saveLoaded) {
        ImGui::Text("Load a save to browse the tree.");
        DrawImportSaveXmlButton();
        ImGui::EndChild();
        return;
    }

    // Search — only activates on Enter to avoid searching on every keystroke
    ImGui::SetNextItemWidth(300);
    if (ImGui::InputText("Search (Enter to search)", g_treeSearch, sizeof(g_treeSearch),
                          ImGuiInputTextFlags_EnterReturnsTrue)) {
        memcpy(g_activeSearch, g_treeSearch, sizeof(g_activeSearch));
        RebuildSearchIndex(g_tree.blob);
    }
    ImGui::SameLine();
    if (ImGui::SmallButton("Clear")) {
        g_treeSearch[0] = 0;
        g_activeSearch[0] = 0;
        g_searchHits.clear();
    }
    ImGui::SameLine();
    if (ImGui::SmallButton("Export Save as XML")) {
        char xpath[MAX_PATH] = "save_export.xml";
        OPENFILENAMEA ofn = {};
        ofn.lStructSize = sizeof(ofn);
        ofn.lpstrFilter = "XML files (*.xml)\0*.xml\0All files\0*.*\0";
        ofn.lpstrFile = xpath;
        ofn.nMaxFile = MAX_PATH;
        ofn.Flags = OFN_OVERWRITEPROMPT;
        ofn.lpstrTitle = "Export Whole Save as XML";
        if (GetSaveFileNameA(&ofn)) {
            auto err = ParcXml::ExportXml(g_tree, xpath);
            g_statusMsg = err.empty() ? ("Save exported to " + std::string(xpath))
                                      : ("XML export failed: " + err);
        }
    }
    if (ImGui::IsItemHovered()) {
        ImGui::SetTooltip("Export the entire save as editable XML.\n"
                          "Re-import via the Import Save from XML button,\n"
                          "or CLI: parc_engine_cli import-xml <file> -o out.save\n"
                          "For single nodes, right-click any tree node instead.");
    }
    ImGui::SameLine();
    DrawImportSaveXmlButton();
    ImGui::SameLine();
    ImGui::Text("Schema: %zu types, TOC: %zu blocks, Blob: %.1f MB",
        g_tree.parsed.schema.types.size(),
        g_tree.parsed.toc.entries.size(),
        g_tree.blob.size() / (1024.0 * 1024.0));

    ImGui::Separator();

    // Object blocks
    static bool loggedRender = false;
    if (g_activeSearch[0] != 0 && !loggedRender) {
        LogMsg("RenderSaveTree: active search='%s', %zu hits, %zu objects",
            g_activeSearch, g_searchHits.size(), g_tree.parsed.objects.size());
        loggedRender = true;
    }
    if (g_activeSearch[0] == 0) loggedRender = false;

    ImGui::BeginChild("TreeScroll", ImVec2(0, 0), false);
    for (size_t bi = 0; bi < g_tree.parsed.objects.size(); bi++) {
        auto& obj = g_tree.parsed.objects[bi];

        // Count list elements for display
        int listCount = 0;
        for (auto& f : obj.fields)
            listCount += (int)f.list_elements.size();

        // Skip empty blocks unless searching
        if (g_activeSearch[0] == 0 && obj.fields.empty()) continue;

        // Search filter at block level — deep search through all descendants
        if (g_activeSearch[0] != 0 && !MatchesSearch(obj.class_name)) {
            bool anyMatch = false;
            for (auto& f : obj.fields) {
                if (AnyDescendantMatches(f, g_tree.blob)) { anyMatch = true; break; }
            }
            if (!anyMatch) continue;
        }

        ImGui::PushID((int)bi);
        char blockLabel[256];
        snprintf(blockLabel, sizeof(blockLabel), "[%zu] %s (%zu fields, %d list elems) [0x%X +%u]###b%zu",
            bi, obj.class_name.c_str(), obj.fields.size(), listCount,
            obj.data_offset, obj.data_size, bi);

        // Auto-open block if navigation target is inside it
        ImGuiTreeNodeFlags blockFlags = ImGuiTreeNodeFlags_SpanAvailWidth;
        if (g_navPending && g_navTargetOffset >= obj.data_offset &&
            g_navTargetOffset < obj.data_offset + obj.data_size) {
            blockFlags |= ImGuiTreeNodeFlags_DefaultOpen;
        }

        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(1.0f, 0.7f, 0.3f, 1.0f));
        bool blockOpen = ImGui::TreeNodeEx(blockLabel, blockFlags);
        ImGui::PopStyleColor();

        // Right-click on block header
        if (ImGui::BeginPopupContextItem("##bctx")) {
            if (ImGui::MenuItem("Go to (clear search)")) {
                g_navTargetOffset = obj.data_offset;
                g_navPending = true;
                g_treeSearch[0] = 0;
                g_activeSearch[0] = 0;
                g_searchHits.clear();
            }
            char offStr[32];
            snprintf(offStr, sizeof(offStr), "0x%X +%u", obj.data_offset, obj.data_size);
            if (ImGui::MenuItem("Copy offset+size")) {
                ImGui::SetClipboardText(offStr);
            }
            ImGui::EndPopup();
        }

        if (blockOpen) {
            RenderFieldTree(obj.fields, g_tree.blob, 0, obj.class_name);
            ImGui::TreePop();
        }
        ImGui::PopID();
    }
    ImGui::EndChild();
    ImGui::EndChild();
}

static void RenderUI() {
    ImGuiIO& io = ImGui::GetIO();
    ImGui::SetNextWindowPos(ImVec2(0, 0));
    ImGui::SetNextWindowSize(io.DisplaySize);
    ImGui::Begin("##Main", nullptr,
        ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
        ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
        ImGuiWindowFlags_MenuBar);

    static float g_uiScale = 1.0f;
    static bool g_scaleChanged = false;

    if (ImGui::BeginMenuBar()) {
        if (ImGui::BeginMenu("File")) {
            if (ImGui::MenuItem("Save", "Ctrl+S", false, g_saveLoaded && g_dirty)) DoSave();
            if (ImGui::MenuItem("Save As...", nullptr, false, g_saveLoaded)) SaveAsDialog();
            ImGui::Separator();
            if (ImGui::MenuItem("Restore from Backup", nullptr, false, g_saveLoaded)) RestoreBackup();
            ImGui::Separator();
            if (ImGui::MenuItem("Refresh Saves")) ScanSaveFiles();
            ImGui::Separator();
            if (ImGui::MenuItem("Exit")) PostQuitMessage(0);
            ImGui::EndMenu();
        }
        if (ImGui::BeginMenu("Settings")) {
            static char g_setSaveDir[MAX_PATH] = {};
            static char g_setLocalAppData[MAX_PATH] = {};
            static bool g_settingsInit = false;
            if (!g_settingsInit) {
                strncpy(g_setSaveDir, g_settings.customSaveDir.c_str(), MAX_PATH - 1);
                strncpy(g_setLocalAppData, g_settings.customLocalAppData.c_str(), MAX_PATH - 1);
                g_settingsInit = true;
            }

            ImGui::Text("Custom Save Directory:");
            ImGui::SetNextItemWidth(400);
            ImGui::InputText("##savedir", g_setSaveDir, MAX_PATH);
            ImGui::SameLine();
            if (ImGui::SmallButton("Browse##save")) {
                BROWSEINFOA bi = {};
                bi.lpszTitle = "Select Save Files Directory";
                bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE;
                LPITEMIDLIST pidl = SHBrowseForFolderA(&bi);
                if (pidl) { SHGetPathFromIDListA(pidl, g_setSaveDir); CoTaskMemFree(pidl); }
            }
            ImGui::TextWrapped(
                "Set this if saves aren't auto-detected. Supports:\n"
                "  - Standard layout (userId/slotId/save.save)\n"
                "  - Game Pass WGS folders (GUID subfolders)\n"
                "  - Flat folder with .save files");

            ImGui::Spacing();
            ImGui::Text("Custom LocalAppData:");
            ImGui::SetNextItemWidth(400);
            ImGui::InputText("##localappdata", g_setLocalAppData, MAX_PATH);
            ImGui::SameLine();
            if (ImGui::SmallButton("Browse##lad")) {
                BROWSEINFOA bi = {};
                bi.lpszTitle = "Select LocalAppData Directory";
                bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE;
                LPITEMIDLIST pidl = SHBrowseForFolderA(&bi);
                if (pidl) { SHGetPathFromIDListA(pidl, g_setLocalAppData); CoTaskMemFree(pidl); }
            }
            ImGui::TextDisabled("For non-C: drive installs or Wine prefixes.");

            ImGui::Spacing();
            ImGui::TextColored(ImVec4(0.5f, 0.8f, 1.0f, 1.0f),
                "Linux/Wine: edit settings.json next to the exe.");
            ImGui::TextColored(ImVec4(0.5f, 0.8f, 1.0f, 1.0f),
                "Proton saves auto-detected at ~/.local/share/Steam/...");

            ImGui::Separator();
            if (ImGui::MenuItem("Save Settings")) {
                g_settings.customSaveDir = g_setSaveDir;
                g_settings.customLocalAppData = g_setLocalAppData;
                g_settings.Save();
                ScanSaveFiles();
                g_statusMsg = "Settings saved!";
            }
            ImGui::EndMenu();
        }
        if (ImGui::BeginMenu("View")) {
            ImGui::Text("UI Scale: %.0f%%", g_uiScale * 100.0f);
            if (ImGui::MenuItem("Small (80%%)"))  { g_uiScale = 0.8f; g_scaleChanged = true; }
            if (ImGui::MenuItem("Normal (100%%)")) { g_uiScale = 1.0f; g_scaleChanged = true; }
            if (ImGui::MenuItem("Large (120%%)"))  { g_uiScale = 1.2f; g_scaleChanged = true; }
            if (ImGui::MenuItem("XL (150%%)"))     { g_uiScale = 1.5f; g_scaleChanged = true; }
            ImGui::EndMenu();
        }
        ImGui::EndMenuBar();
    }

    if (g_scaleChanged) {
        g_scaleChanged = false;
        ImGui::GetIO().FontGlobalScale = g_uiScale;
        ImGui::GetStyle().ScaleAllSizes(g_uiScale);
    }

    if (io.KeyCtrl && ImGui::IsKeyPressed(ImGuiKey_S) && g_saveLoaded && g_dirty) DoSave();

    // Layout: save browser on left, tabbed content on right
    RenderSaveBrowser(220);
    ImGui::SameLine();

    ImGui::BeginChild("RightPane", ImVec2(0, 0), false);
    if (ImGui::BeginTabBar("MainTabs")) {
        if (TabOn("Inventory") && ImGui::BeginTabItem("Inventory")) {
            RenderInventory();
            ImGui::EndTabItem();
        }
        if (TabOn("Equipment") && ImGui::BeginTabItem("Equipment")) {
            if (!g_saveLoaded) {
                ImGui::Text("Load a save first.");
            } else {
                ImGui::TextColored(ImVec4(0.4f, 1.0f, 0.6f, 1.0f), "Equipment Editor — Enchant, Endurance, Sockets");
                ImGui::SameLine();
                if (g_dirty && ImGui::SmallButton("Save (Ctrl+S)")) DoSave();

                static int g_equipSelected = -1;
                static char g_equipFilter[64] = {};
                ImGui::InputText("Filter##equip", g_equipFilter, sizeof(g_equipFilter));

                if (ImGui::BeginTable("EquipTable", 8,
                    ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg | ImGuiTableFlags_ScrollY |
                    ImGuiTableFlags_Resizable | ImGuiTableFlags_SizingStretchProp,
                    ImVec2(0, ImGui::GetContentRegionAvail().y * 0.55f))) {

                    ImGui::TableSetupScrollFreeze(0, 1);
                    ImGui::TableSetupColumn("Name", ImGuiTableColumnFlags_WidthStretch);
                    ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 60);
                    ImGui::TableSetupColumn("Source", ImGuiTableColumnFlags_WidthFixed, 70);
                    ImGui::TableSetupColumn("Enchant", ImGuiTableColumnFlags_WidthFixed, 70);
                    ImGui::TableSetupColumn("Endurance", ImGuiTableColumnFlags_WidthFixed, 80);
                    ImGui::TableSetupColumn("Sharpness", ImGuiTableColumnFlags_WidthFixed, 70);
                    ImGui::TableSetupColumn("Sockets", ImGuiTableColumnFlags_WidthFixed, 60);
                    ImGui::TableSetupColumn("Actions", ImGuiTableColumnFlags_WidthFixed, 100);
                    ImGui::TableHeadersRow();

                    int row = 0;
                    for (int i = 0; i < (int)g_items.size(); i++) {
                        auto& item = g_items[i];
                        // Show only equipment-like items (have enchant OR endurance OR sockets)
                        if (item.enchantOffset == 0 && item.enduranceOffset == 0 && item.maxSocketOffset == 0) continue;

                        if (g_equipFilter[0]) {
                            std::string lower = item.name;
                            for (auto& c : lower) c = (char)tolower(c);
                            std::string needle(g_equipFilter);
                            for (auto& c : needle) c = (char)tolower(c);
                            if (lower.find(needle) == std::string::npos) continue;
                        }

                        ImGui::PushID(i + 200000);
                        ImGui::TableNextRow();

                        // Name (with icon) — no SpanAllColumns so buttons work
                        ImGui::TableNextColumn();
                        {
                            auto* icon = IconCache::Get(item.itemKey);
                            if (icon) {
                                ImGui::Image((ImTextureID)icon, ImVec2(20, 20));
                                ImGui::SameLine();
                            }
                        }
                        bool selected = (g_equipSelected == i);
                        if (ImGui::Selectable(item.name.c_str(), selected)) {
                            g_equipSelected = i;
                        }

                        // Key
                        ImGui::TableNextColumn();
                        ImGui::Text("%d", item.itemKey);

                        // Source
                        ImGui::TableNextColumn();
                        ImGui::TextColored(item.source == "Equipment" ? ImVec4(0.3f,1,0.3f,1) : ImVec4(0.7f,0.7f,0.7f,1),
                            "%s", item.source.c_str());

                        // Enchant (editable)
                        ImGui::TableNextColumn();
                        if (item.enchantOffset > 0) {
                            ImGui::SetNextItemWidth(-1);
                            int enc = item.enchantLevel;
                            if (ImGui::InputInt("##enc", &enc, 1, 5)) {
                                if (enc < 0) enc = 0;
                                if (enc > 20) enc = 20;
                                item.enchantLevel = enc;
                                uint16_t v = (uint16_t)enc;
                                memcpy(g_tree.blob.data() + item.enchantOffset, &v, 2);
                                g_dirty = true;
                            }
                        } else { ImGui::TextDisabled("-"); }

                        // Endurance (editable)
                        ImGui::TableNextColumn();
                        if (item.enduranceOffset > 0) {
                            ImGui::SetNextItemWidth(-1);
                            int dur = item.endurance;
                            if (ImGui::InputInt("##dur", &dur, 100, 1000)) {
                                if (dur < 0) dur = 0;
                                if (dur > 65535) dur = 65535;
                                item.endurance = dur;
                                uint16_t v = (uint16_t)dur;
                                memcpy(g_tree.blob.data() + item.enduranceOffset, &v, 2);
                                g_dirty = true;
                            }
                        } else { ImGui::TextDisabled("-"); }

                        // Sharpness (editable)
                        ImGui::TableNextColumn();
                        if (item.sharpnessOffset > 0) {
                            ImGui::SetNextItemWidth(-1);
                            int sh = item.sharpness;
                            if (ImGui::InputInt("##sh", &sh, 1, 5)) {
                                if (sh < 0) sh = 0;
                                if (sh > 100) sh = 100;
                                item.sharpness = sh;
                                uint16_t v = (uint16_t)sh;
                                memcpy(g_tree.blob.data() + item.sharpnessOffset, &v, 2);
                                g_dirty = true;
                            }
                        } else { ImGui::TextDisabled("-"); }

                        // Sockets (valid/max)
                        ImGui::TableNextColumn();
                        if (item.maxSocketOffset > 0) {
                            ImGui::Text("%d/%d", item.validSockets, item.maxSockets);
                        } else { ImGui::TextDisabled("-"); }

                        // Actions
                        ImGui::TableNextColumn();
                        if (item.validSockets >= 5 && item.validSocketOffset > 0) {
                            ImGui::TextColored(ImVec4(0.3f, 1.0f, 0.3f, 1.0f), "5/5");
                        } else if (item.validSocketOffset > 0) {
                            if (ImGui::SmallButton("Unlock 5")) {
                                uint8_t five = 5;
                                memcpy(g_tree.blob.data() + item.validSocketOffset, &five, 1);
                                item.validSockets = 5;
                                if (item.maxSocketOffset > 0 && item.maxSockets < 5) {
                                    memcpy(g_tree.blob.data() + item.maxSocketOffset, &five, 1);
                                    item.maxSockets = 5;
                                }
                                g_dirty = true;
                            }
                        } else {
                            ImGui::TextDisabled("-");
                        }
                        if (item.enchantOffset > 0) {
                            ImGui::SameLine();
                            if (ImGui::SmallButton("+10")) {
                                item.enchantLevel = 10;
                                uint16_t v = 10;
                                memcpy(g_tree.blob.data() + item.enchantOffset, &v, 2);
                                g_dirty = true;
                            }
                        }

                        ImGui::PopID();
                        row++;
                    }
                    ImGui::EndTable();
                }

                // Gem database
                struct GemEntry { int key; const char* name; };
                struct GemCategory { const char* name; std::vector<GemEntry> gems; };
                static const GemCategory GEM_DB[] = {
                    {"Empty", {{0, "Empty (remove gem)"}}},
                    {"Damage", {{1002785,"Destruction I"},{1002786,"Destruction II"},{1002787,"Destruction III"},{1002862,"Greater Destruction"}}},
                    {"Defense", {{1002794,"Fortification I"},{1002795,"Fortification II"},{1002796,"Fortification III"}}},
                    {"Crit Rate", {{1002791,"Insight I"},{1002792,"Insight II"},{1002793,"Insight III"},{1002969,"Greater Insight"}}},
                    {"Dmg Reduction", {{1002797,"Aegis I"},{1002798,"Aegis II"},{1002799,"Aegis III"}}},
                    {"Atk Speed", {{1002810,"Swift I"},{1002811,"Swift II"},{1002812,"Swift III"},{1002970,"Greater Swift"}}},
                    {"Move Speed", {{1002813,"Haste I"},{1002814,"Haste II"},{1002815,"Haste III"}}},
                    {"HP Regen", {{1002822,"Vitality I"},{1002823,"Vitality II"},{1002824,"Vitality III"}}},
                    {"Stamina", {{1002747,"Vigor I"},{1002748,"Vigor II"},{1002751,"Vigor III"}}},
                    {"MP Regen", {{1002752,"Composure I"},{1002753,"Composure II"},{1002754,"Composure III"}}},
                    {"Guard Def", {{1002807,"Fortitude I"},{1002808,"Fortitude II"},{1002809,"Fortitude III"}}},
                    {"Fire Resist", {{1001424,"Flameward I"},{1001425,"Flameward II"},{1001426,"Flameward III"},{1002972,"Greater Flameward"}}},
                    {"Ice Resist", {{1002167,"Frostward I"},{1002389,"Frostward II"},{1002467,"Frostward III"},{1002973,"Greater Frostward"}}},
                    {"Lightning", {{1002497,"Shockward I"},{1002498,"Shockward II"},{1002499,"Shockward III"},{1002974,"Greater Shockward"}}},
                    {"Skills", {{1002578,"Storm Fang"},{1002766,"Volcanic Eruption"},{1002764,"Frost Hail"},{1002848,"Orbs of Lightning"},{1002569,"Wind Slash"},{1002583,"Karmic Pulse"},{1002805,"Tempest of Destruction"},{1002855,"Kinetic Burst"}}},
                };
                static const int GEM_DB_COUNT = sizeof(GEM_DB) / sizeof(GEM_DB[0]);

                // Socket panel for selected item
                ImGui::Separator();
                if (g_equipSelected >= 0 && g_equipSelected < (int)g_items.size()) {
                    auto& sel = g_items[g_equipSelected];
                    auto* selIcon = IconCache::Get(sel.itemKey);
                    if (selIcon) { ImGui::Image((ImTextureID)selIcon, ImVec2(24, 24)); ImGui::SameLine(); }
                    ImGui::Text("%s [%d]", sel.name.c_str(), sel.itemKey);
                    ImGui::SameLine();
                    ImGui::TextColored(ImVec4(0.5f,1,0.5f,1), "Sockets: %d/%d", sel.validSockets, sel.maxSockets);

                    // Find the socket list in the tree
                    // Search all blocks and nested lists for the item element
                    SaveParserCpp::GenericFieldValue* socketField = nullptr;
                    auto findSocketField = [&](SaveParserCpp::GenericFieldValue& elem) -> bool {
                        if (elem.start_offset != sel.elemStart) return false;
                        for (auto& cf : elem.child_fields) {
                            if (cf.name == "_socketSaveDataList" && cf.present) {
                                socketField = &cf;
                                return true;
                            }
                        }
                        return true; // found item but no socket field
                    };
                    for (auto& obj : g_tree.parsed.objects) {
                        for (auto& f : obj.fields) {
                            if (f.meta_kind != 6 && f.meta_kind != 7) continue;
                            for (auto& elem : f.list_elements) {
                                if (findSocketField(elem)) goto found_item;
                                for (auto& cf2 : elem.child_fields) {
                                    if (cf2.meta_kind != 6 && cf2.meta_kind != 7) continue;
                                    for (auto& elem2 : cf2.list_elements) {
                                        if (findSocketField(elem2)) goto found_item;
                                    }
                                }
                            }
                        }
                    }
                    found_item:;

                    if (socketField) {
                        // Show each socket slot with gem dropdown
                        for (size_t si = 0; si < socketField->list_elements.size(); si++) {
                            auto& sock = socketField->list_elements[si];
                            uint32_t gemKeyOff = 0;
                            int gemKey = 0;
                            for (auto& sf : sock.child_fields) {
                                if (sf.name == "_itemKey" && sf.present) {
                                    gemKey = (int)ReadFieldVal(g_tree.blob, sf.start_offset, sf.end_offset);
                                    gemKeyOff = sf.start_offset;
                                }
                            }
                            auto git = g_itemDB.find(gemKey);
                            const char* gemName = (git != g_itemDB.end()) ? git->second.name.c_str() : (gemKey ? "Unknown" : "Empty");

                            ImGui::PushID((int)(si + 5000));
                            auto* gemIcon = gemKey ? IconCache::Get(gemKey) : nullptr;
                            if (gemIcon) { ImGui::Image((ImTextureID)gemIcon, ImVec2(18, 18)); ImGui::SameLine(); }
                            ImGui::Text("Slot %zu:", si + 1);
                            ImGui::SameLine();
                            ImGui::SetNextItemWidth(200);
                            if (ImGui::BeginCombo("##gem", gemName)) {
                                for (int ci = 0; ci < GEM_DB_COUNT; ci++) {
                                    if (GEM_DB[ci].gems.size() > 1 || ci == 0) {
                                        ImGui::TextColored(ImVec4(1,0.8f,0.3f,1), "%s", GEM_DB[ci].name);
                                    }
                                    for (auto& g : GEM_DB[ci].gems) {
                                        bool isSel = (g.key == gemKey);
                                        if (ImGui::Selectable(g.name, isSel)) {
                                            if (gemKeyOff > 0 && gemKeyOff + 4 <= g_tree.blob.size()) {
                                                uint32_t newKey = (uint32_t)g.key;
                                                memcpy(g_tree.blob.data() + gemKeyOff, &newKey, 4);
                                                g_dirty = true;
                                                // Auto-save so it sticks
                                                DoSave();
                                                g_tree = ParcEngine::LoadSave(g_savePath);
                                                ExtractItems();
                                                g_dirty = false;
                                                g_statusMsg = "Gem changed!";
                                            }
                                        }
                                        if (isSel) ImGui::SetItemDefaultFocus();
                                    }
                                    if (ci < GEM_DB_COUNT - 1) ImGui::Separator();
                                }
                                ImGui::EndCombo();
                            }
                            ImGui::PopID();
                        }

                        if (socketField->list_elements.empty()) {
                            ImGui::TextDisabled("No sockets on this item yet");
                        }
                    } else {
                        ImGui::TextDisabled("No socket data found for this item");
                    }

                    // Max 5 Gem Slots — disabled, causes crashes. TODO: fix rebuild logic
                    ImGui::Separator();
                    if (false && sel.elemStart > 0) {
                        if (ImGui::Button("Max 5 Gem Slots (rebuild item)")) {
                            // Read current gem keys from existing sockets
                            std::vector<ItemFactory::SocketGem> gems;
                            if (socketField) {
                                for (auto& sock : socketField->list_elements) {
                                    int gk = 0;
                                    for (auto& sf : sock.child_fields)
                                        if (sf.name == "_itemKey" && sf.present)
                                            gk = (int)ReadFieldVal(g_tree.blob, sf.start_offset, sf.end_offset);
                                    gems.push_back({(uint32_t)gk, 65535});
                                }
                            }
                            while (gems.size() < 5) gems.push_back({0, 65535});

                            std::unordered_map<std::string, uint16_t> ti_map;
                            for (size_t ti = 0; ti < g_tree.parsed.schema.types.size(); ti++)
                                ti_map[g_tree.parsed.schema.types[ti].name] = (uint16_t)ti;

                            ItemFactory::ItemSpec spec;
                            spec.itemKey = (uint32_t)sel.itemKey;
                            spec.itemNo = (uint64_t)sel.itemNo;
                            spec.slotNo = (uint16_t)sel.slotNo;
                            spec.stackCount = (uint64_t)sel.stackCount;
                            spec.enchantLevel = (uint16_t)sel.enchantLevel;
                            spec.endurance = (uint16_t)sel.endurance;
                            spec.sharpness = (uint16_t)sel.sharpness;
                            spec.maxSocketCount = 5;
                            spec.maskType = ItemFactory::ItemMask::Equipment;
                            spec.sockets = gems;

                            auto newBytes = ItemFactory::BuildItem(spec, ti_map);
                            if (!newBytes.empty()) {
                                // Use tree-based approach: add new item via InsertNested
                                // The old item stays (user can delete it later or the game
                                // will use the new one based on slotNo)
                                // Find which inventory category this item is in
                                int targetCat = 1; // default
                                for (auto& obj : g_tree.parsed.objects) {
                                    if (obj.class_name.find("InventorySaveData") == std::string::npos) continue;
                                    for (auto& f : obj.fields) {
                                        if (f.name != "_inventorylist") continue;
                                        for (int ci = 0; ci < (int)f.list_elements.size(); ci++) {
                                            for (auto& cf : f.list_elements[ci].child_fields) {
                                                if (cf.name != "_itemList") continue;
                                                for (auto& el : cf.list_elements) {
                                                    if (el.start_offset == sel.elemStart) {
                                                        targetCat = ci;
                                                        goto found_cat;
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                                found_cat:;
                                char ipath[64];
                                snprintf(ipath, sizeof(ipath), "_inventorylist[%d]._itemList", targetCat);
                                g_pendingCreatePath = ipath;
                                g_pendingCreateBytes = std::move(newBytes);
                                g_pendingCreateItem = true;
                                g_statusMsg = "Adding item with 5 sockets...";
                            }
                        }
                    }
                } else {
                    ImGui::TextDisabled("Select an equipment item to edit sockets");
                }
            }
            ImGui::EndTabItem();
        }
        if (TabOn("Repurchase") && ImGui::BeginTabItem("Repurchase")) {
            ImGui::BeginChild("RepurchArea", ImVec2(0, 0), false);
            if (!g_saveLoaded) {
                ImGui::Text("Load a save first.");
            } else {
                ImGui::TextColored(ImVec4(1.0f, 0.7f, 0.3f, 1.0f), "Vendor Repurchase — Best Way to Get New Items");
                ImGui::TextWrapped(
                    "1. Sell a SAME-TYPE junk item to any vendor (sword for sword, helm for helm)\n"
                    "2. Open the save here, find your sold item\n"
                    "3. Swap its key to the item you want, save\n"
                    "4. Load in-game, buy it back from the vendor\n\n"
                    "IMPORTANT: Swap SAME TYPE for SAME TYPE!\n"
                    "Sword->Sword, Armor->Armor, Ring->Ring, Potion->Potion.\n"
                    "Cross-type swaps (Arrow->Sword) produce broken items.");
                ImGui::TextColored(ImVec4(1.0f, 0.4f, 0.3f, 1.0f),
                    "The game handles stats, sockets, and enchant on buyback — no manual fixing needed.");
                ImGui::Separator();

                if (g_vendorItems.empty()) {
                    if (ImGui::Button("Scan Vendor Items")) ScanVendorItems();
                } else {
                    if (ImGui::Button("Rescan")) ScanVendorItems();
                    ImGui::SameLine();
                    ImGui::Text("%d vendor items found", (int)g_vendorItems.size());
                }

                if (g_dirty) {
                    ImGui::SameLine();
                    ImGui::TextColored(ImVec4(1.0f, 0.4f, 0.3f, 1.0f), " [MODIFIED]");
                    ImGui::SameLine();
                    if (ImGui::SmallButton("Save (Ctrl+S)")) DoSave();
                }

                // Filter
                static char vpFilter[128] = {};
                ImGui::SetNextItemWidth(300);
                ImGui::InputText("Filter vendor items", vpFilter, sizeof(vpFilter));

                ImGui::Separator();

                // Swap popup (reuse the same one from inventory)
                static int g_vpSwapIdx = -1;
                static bool g_vpSwapOpen = false;
                static char g_vpSwapSearch[128] = {};
                static std::vector<std::pair<int, std::string>> g_vpSwapResults;

                if (g_vpSwapOpen && g_vpSwapIdx >= 0 && g_vpSwapIdx < (int)g_vendorItems.size()) {
                    ImGui::SetNextWindowSize(ImVec2(500, 400), ImGuiCond_FirstUseEver);
                    if (ImGui::Begin("Swap Vendor Item", &g_vpSwapOpen, ImGuiWindowFlags_NoCollapse)) {
                        auto& vi = g_vendorItems[g_vpSwapIdx];
                        ImGui::Text("Swapping: %s (key=%d) from %s", vi.name.c_str(), vi.itemKey, vi.vendorName.c_str());
                        ImGui::Separator();
                        ImGui::SetNextItemWidth(350);
                        if (ImGui::InputText("Search (Enter)", g_vpSwapSearch, sizeof(g_vpSwapSearch),
                                              ImGuiInputTextFlags_EnterReturnsTrue)) {
                            g_vpSwapResults.clear();
                            std::string s = g_vpSwapSearch;
                            std::string sL = s;
                            std::transform(sL.begin(), sL.end(), sL.begin(), ::tolower);
                            bool isNum2 = true;
                            for (char c : s) if (!isdigit(c)) { isNum2 = false; break; }
                            for (auto& [k, def] : g_itemDB) {
                                if (isNum2) {
                                    if (std::to_string(k).find(s) != std::string::npos)
                                        g_vpSwapResults.push_back({k, def.name + " (" + def.category + ")"});
                                } else {
                                    std::string nL = def.name; std::transform(nL.begin(), nL.end(), nL.begin(), ::tolower);
                                    if (nL.find(sL) != std::string::npos)
                                        g_vpSwapResults.push_back({k, def.name + " (" + def.category + ")"});
                                }
                                if (g_vpSwapResults.size() >= 100) break;
                            }
                            std::sort(g_vpSwapResults.begin(), g_vpSwapResults.end(),
                                [](auto& a, auto& b) { return a.second < b.second; });
                        }
                        ImGui::SameLine();
                        ImGui::Text("%zu results", g_vpSwapResults.size());
                        ImGui::Separator();
                        ImGui::BeginChild("VPResults", ImVec2(0, -30), true);
                        for (auto& [k, nm] : g_vpSwapResults) {
                            char lbl[256];
                            snprintf(lbl, sizeof(lbl), "%d - %s", k, nm.c_str());
                            if (ImGui::Selectable(lbl)) {
                                uint32_t nk = (uint32_t)k;
                                memcpy(g_tree.blob.data() + vi.itemKeyOffset, &nk, vi.itemKeySize);
                                // NOTE: _transferredItemKey formula DISPROVEN.
                                // Vendor repurchase: game handles it on buyback.
                                vi.itemKey = k;
                                auto it2 = g_itemDB.find(k);
                                if (it2 != g_itemDB.end()) { vi.name = it2->second.name; vi.category = it2->second.category; }
                                else vi.name = "Item " + std::to_string(k);
                                g_dirty = true;
                                g_statusMsg = "Vendor item swapped to " + vi.name + "! Save with Ctrl+S.";
                                g_vpSwapOpen = false;
                            }
                        }
                        ImGui::EndChild();
                        if (ImGui::Button("Cancel", ImVec2(-1, 0))) g_vpSwapOpen = false;
                    }
                    ImGui::End();
                }

                // Vendor items table
                if (!g_vendorItems.empty() && ImGui::BeginTable("VendorItems", 9,
                    ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                    ImGuiTableFlags_ScrollY | ImGuiTableFlags_Resizable,
                    ImVec2(0, 0))) {

                    ImGui::TableSetupScrollFreeze(0, 1);
                    ImGui::TableSetupColumn("Vendor", ImGuiTableColumnFlags_WidthFixed, 160);
                    ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 70);
                    ImGui::TableSetupColumn("Name", ImGuiTableColumnFlags_WidthStretch);
                    ImGui::TableSetupColumn("Category", ImGuiTableColumnFlags_WidthFixed, 100);
                    ImGui::TableSetupColumn("Stack", ImGuiTableColumnFlags_WidthFixed, 80);
                    ImGui::TableSetupColumn("+Enc", ImGuiTableColumnFlags_WidthFixed, 45);
                    ImGui::TableSetupColumn("End", ImGuiTableColumnFlags_WidthFixed, 45);
                    ImGui::TableSetupColumn("Shp", ImGuiTableColumnFlags_WidthFixed, 45);
                    ImGui::TableSetupColumn("Swap", ImGuiTableColumnFlags_WidthFixed, 50);
                    ImGui::TableHeadersRow();

                    std::string fL = vpFilter;
                    std::transform(fL.begin(), fL.end(), fL.begin(), ::tolower);

                    for (int vi = 0; vi < (int)g_vendorItems.size(); vi++) {
                        auto& v = g_vendorItems[vi];
                        if (vpFilter[0] != 0) {
                            std::string nL = v.name; std::transform(nL.begin(), nL.end(), nL.begin(), ::tolower);
                            std::string vL = v.vendorName; std::transform(vL.begin(), vL.end(), vL.begin(), ::tolower);
                            std::string cL = v.category; std::transform(cL.begin(), cL.end(), cL.begin(), ::tolower);
                            if (nL.find(fL) == std::string::npos && vL.find(fL) == std::string::npos &&
                                cL.find(fL) == std::string::npos && std::to_string(v.itemKey).find(fL) == std::string::npos)
                                continue;
                        }

                        ImGui::TableNextRow();
                        ImGui::PushID(vi + 50000);

                        ImGui::TableSetColumnIndex(0);
                        ImGui::TextColored(ImVec4(0.5f, 0.8f, 1.0f, 1.0f), "%s", v.vendorName.c_str());
                        ImGui::TableSetColumnIndex(1);
                        ImGui::Text("%d", v.itemKey);
                        ImGui::TableSetColumnIndex(2);
                        ImGui::Text("%s", v.name.c_str());
                        ImGui::TableSetColumnIndex(3);
                        ImGui::Text("%s", v.category.c_str());
                        ImGui::TableSetColumnIndex(4);
                        // Editable stack
                        ImGui::SetNextItemWidth(-1);
                        int stk = (int)v.stackCount;
                        if (ImGui::InputInt("##vs", &stk, 1, 100)) {
                            if (stk < 1) stk = 1;
                            v.stackCount = stk;
                            if (v.stackOffset > 0 && v.stackOffset + v.stackSize <= g_tree.blob.size()) {
                                uint64_t sv = (uint64_t)stk;
                                memcpy(g_tree.blob.data() + v.stackOffset, &sv, v.stackSize);
                            }
                            g_dirty = true;
                        }
                        ImGui::TableSetColumnIndex(5);
                        ImGui::Text("%d", v.enchantLevel);
                        ImGui::TableSetColumnIndex(6);
                        ImGui::Text("%d", v.endurance);
                        ImGui::TableSetColumnIndex(7);
                        ImGui::Text("%d", v.sharpness);
                        ImGui::TableSetColumnIndex(8);
                        if (ImGui::SmallButton("Swap")) {
                            g_vpSwapIdx = vi;
                            g_vpSwapOpen = true;
                            g_vpSwapSearch[0] = 0;
                            g_vpSwapResults.clear();
                        }

                        ImGui::PopID();
                    }
                    ImGui::EndTable();
                }
            }
            ImGui::EndChild();
            ImGui::EndTabItem();
        }
        if (TabOn("Mounts") && ImGui::BeginTabItem("Mounts")) {
            ImGui::BeginChild("MountsArea", ImVec2(0, 0), false);
            if (!g_saveLoaded) {
                ImGui::Text("Load a save first.");
            } else {
                // Scan existing mounts from save
                struct OwnedMount { int characterKey; std::string name; uint32_t elemStart; uint32_t elemEnd; };
                static std::vector<OwnedMount> ownedMounts;
                static bool mountsScanned = false;
                if (!mountsScanned) {
                    ownedMounts.clear();
                    for (auto& obj : g_tree.parsed.objects) {
                        if (obj.class_name.find("MercenaryClan") == std::string::npos) continue;
                        for (auto& fld : obj.fields) {
                            if (fld.name != "_mercenaryDataList") continue;
                            for (auto& el : fld.list_elements) {
                                for (auto& cf : el.child_fields) {
                                    if (cf.name == "_characterKey" && cf.present &&
                                        cf.start_offset > 0 && cf.end_offset > cf.start_offset) {
                                        uint32_t sz = cf.end_offset - cf.start_offset;
                                        if (sz <= 8 && cf.start_offset + sz <= g_tree.blob.size()) {
                                            int64_t ck = 0;
                                            memcpy(&ck, g_tree.blob.data() + cf.start_offset, sz);
                                            if (g_mountKeySet.count((int)ck)) {
                                                std::string mname = "Mount " + std::to_string((int)ck);
                                                const char* dn = LookupFieldName("_characterKey", ck);
                                                if (dn) mname = dn;
                                                ownedMounts.push_back({(int)ck, mname, el.start_offset, el.end_offset});
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        break;
                    }
                    mountsScanned = true;
                }

                // Donor selection
                static int selectedDonor = -1;

                ImGui::TextColored(ImVec4(1.0f, 0.7f, 0.3f, 1.0f), "Add Mount");
                ImGui::TextWrapped(
                    "Step 1: Select a donor mount from your save (the new mount copies its structure).\n"
                    "Step 2: Pick the mount you want to add. Same type = safe (wolf->wolf, horse->horse).");
                ImGui::Separator();

                if (!g_statusMsg.empty())
                    ImGui::TextColored(ImVec4(0.4f, 0.8f, 0.4f, 1.0f), "%s", g_statusMsg.c_str());
                if (g_dirty) {
                    ImGui::SameLine();
                    ImGui::TextColored(ImVec4(1.0f, 0.4f, 0.3f, 1.0f), " [MODIFIED]");
                    ImGui::SameLine();
                    if (ImGui::SmallButton("Save (Ctrl+S)")) DoSave();
                }

                // Your mounts panel
                ImGui::TextColored(ImVec4(0.5f, 0.8f, 1.0f, 1.0f), "Your Mounts (%d):", (int)ownedMounts.size());
                if (ImGui::SmallButton("Rescan")) { mountsScanned = false; }
                ImGui::BeginChild("OwnedMounts", ImVec2(0, 150), true);
                for (int di = 0; di < (int)ownedMounts.size(); di++) {
                    auto& om = ownedMounts[di];
                    char label[256];
                    snprintf(label, sizeof(label), "%d - %s (%u bytes)", om.characterKey, om.name.c_str(), om.elemEnd - om.elemStart);
                    if (ImGui::Selectable(label, selectedDonor == di))
                        selectedDonor = di;
                }
                ImGui::EndChild();

                if (selectedDonor >= 0 && selectedDonor < (int)ownedMounts.size()) {
                    ImGui::TextColored(ImVec4(0.2f, 1.0f, 0.2f, 1.0f), "Donor: %s (key=%d)",
                        ownedMounts[selectedDonor].name.c_str(), ownedMounts[selectedDonor].characterKey);
                } else {
                    ImGui::TextColored(ImVec4(1.0f, 0.4f, 0.3f, 1.0f), "Select a donor mount above first!");
                }

                ImGui::Separator();

                // Mount catalog
                static char mountFilter[64] = {};
                ImGui::SetNextItemWidth(300);
                ImGui::InputText("Filter mounts", mountFilter, sizeof(mountFilter));
                ImGui::SameLine();
                ImGui::Text("(%d available)", (int)g_mountList.size());

                std::string mfLower = mountFilter;
                std::transform(mfLower.begin(), mfLower.end(), mfLower.begin(), ::tolower);

                if (ImGui::BeginTable("Mounts", 3,
                    ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                    ImGuiTableFlags_ScrollY | ImGuiTableFlags_Resizable,
                    ImVec2(0, 0))) {

                    ImGui::TableSetupScrollFreeze(0, 1);
                    ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 80);
                    ImGui::TableSetupColumn("Name", ImGuiTableColumnFlags_WidthStretch);
                    ImGui::TableSetupColumn("Add", ImGuiTableColumnFlags_WidthFixed, 80);
                    ImGui::TableHeadersRow();

                    for (int mi = 0; mi < (int)g_mountList.size(); mi++) {
                        auto& m = g_mountList[mi];
                        if (mountFilter[0] != 0) {
                            std::string nLower = m.name;
                            std::transform(nLower.begin(), nLower.end(), nLower.begin(), ::tolower);
                            std::string kStr = std::to_string(m.characterKey);
                            if (nLower.find(mfLower) == std::string::npos &&
                                kStr.find(mfLower) == std::string::npos)
                                continue;
                        }

                        ImGui::TableNextRow();
                        ImGui::PushID(mi);

                        ImGui::TableSetColumnIndex(0);
                        ImGui::Text("%d", m.characterKey);

                        ImGui::TableSetColumnIndex(1);
                        ImGui::Text("%s", m.name.c_str());

                        ImGui::TableSetColumnIndex(2);
                        bool canAdd = (selectedDonor >= 0 && selectedDonor < (int)ownedMounts.size());
                        if (!canAdd) ImGui::BeginDisabled();
                        if (ImGui::SmallButton("Add")) {
                            auto& donor = ownedMounts[selectedDonor];
                            // Copy donor bytes
                            std::vector<uint8_t> elemBytes(
                                g_tree.blob.begin() + donor.elemStart,
                                g_tree.blob.begin() + donor.elemEnd);

                            // Auto-increment mercenaryNo
                            uint32_t maxMN = 0;
                            for (auto& obj2 : g_tree.parsed.objects) {
                                if (obj2.class_name.find("MercenaryClan") == std::string::npos) continue;
                                for (auto& fld2 : obj2.fields) {
                                    if (fld2.name != "_mercenaryDataList") continue;
                                    for (auto& el2 : fld2.list_elements) {
                                        for (auto& cf2 : el2.child_fields) {
                                            if (cf2.name == "_mercenaryNo" && cf2.present &&
                                                cf2.start_offset > 0 && cf2.end_offset > cf2.start_offset) {
                                                uint32_t sz2 = cf2.end_offset - cf2.start_offset;
                                                if (sz2 <= 8 && cf2.start_offset + sz2 <= g_tree.blob.size()) {
                                                    uint64_t mno = 0;
                                                    memcpy(&mno, g_tree.blob.data() + cf2.start_offset, sz2);
                                                    if ((uint32_t)mno > maxMN) maxMN = (uint32_t)mno;
                                                }
                                            }
                                        }
                                    }
                                }
                            }

                            // Patch mercenaryNo in copy
                            for (auto& obj2 : g_tree.parsed.objects) {
                                if (obj2.class_name.find("MercenaryClan") == std::string::npos) continue;
                                for (auto& fld2 : obj2.fields) {
                                    if (fld2.name != "_mercenaryDataList") continue;
                                    for (auto& el2 : fld2.list_elements) {
                                        if (el2.start_offset == donor.elemStart) {
                                            for (auto& cf2 : el2.child_fields) {
                                                if (cf2.name == "_mercenaryNo" && cf2.present) {
                                                    uint32_t relOff = cf2.start_offset - donor.elemStart;
                                                    uint32_t sz2 = cf2.end_offset - cf2.start_offset;
                                                    if (relOff + sz2 <= elemBytes.size() && sz2 <= 8) {
                                                        uint64_t nv = (uint64_t)(maxMN + 1);
                                                        memcpy(elemBytes.data() + relOff, &nv, sz2);
                                                    }
                                                }
                                                if (cf2.name == "_characterKey" && cf2.present) {
                                                    uint32_t relOff = cf2.start_offset - donor.elemStart;
                                                    uint32_t sz2 = cf2.end_offset - cf2.start_offset;
                                                    if (relOff + sz2 <= elemBytes.size() && sz2 <= 8) {
                                                        int64_t nv = (int64_t)m.characterKey;
                                                        memcpy(elemBytes.data() + relOff, &nv, sz2);
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }

                            auto result = ParcEngine::SpliceIntoList(g_tree,
                                "MercenaryClanSaveData", "_mercenaryDataList", elemBytes, donor.elemStart);
                            if (result.ok) {
                                g_searchHits.clear();
                                g_activeSearch[0] = 0;
                                g_treeSearch[0] = 0;
                                g_navPending = false;
                                g_listPage.clear();
                                g_skipTreeFrames = 3;
                                ExtractItems();
                                g_dirty = true;
                                mountsScanned = false;
                                g_statusMsg = "Added " + m.name + " (from " + donor.name + ")! Save with Ctrl+S.";
                            } else {
                                g_statusMsg = "Failed: " + result.error;
                            }
                        }
                        if (!canAdd) ImGui::EndDisabled();

                        ImGui::PopID();
                    }
                    ImGui::EndTable();
                }
            }
            ImGui::EndChild();
            ImGui::EndTabItem();
        }

        // ── PETS TAB ──
        if (TabOn("Pets") && ImGui::BeginTabItem("Pets")) {
            ImGui::BeginChild("PetsArea", ImVec2(0, 0), false);
            if (!g_saveLoaded) {
                ImGui::Text("Load a save first.");
            } else if (g_petList.empty()) {
                ImGui::TextColored(ImVec4(1,0.4f,0.3f,1), "pet_catalog.json not found in data\\ — cannot list pets.");
            } else {
                // Donor = any element in _mercenaryDataList. Pets, mounts and human
                // mercenaries share this list; cloning an existing element keeps all
                // type indices valid for THIS save (no remap). Same-species donor = safest.
                struct OwnedMerc { int characterKey; std::string label; std::string species; uint32_t elemStart; uint32_t elemEnd; bool isPet; };
                static std::vector<OwnedMerc> ownedMercs;
                static bool petsScanned = false;
                if (!petsScanned) {
                    ownedMercs.clear();
                    for (auto& obj : g_tree.parsed.objects) {
                        if (obj.class_name.find("MercenaryClan") == std::string::npos) continue;
                        for (auto& fld : obj.fields) {
                            if (fld.name != "_mercenaryDataList") continue;
                            for (auto& el : fld.list_elements) {
                                for (auto& cf : el.child_fields) {
                                    if (cf.name == "_characterKey" && cf.present &&
                                        cf.start_offset > 0 && cf.end_offset > cf.start_offset) {
                                        uint32_t sz = cf.end_offset - cf.start_offset;
                                        if (sz <= 8 && cf.start_offset + sz <= g_tree.blob.size()) {
                                            int64_t ck = 0;
                                            memcpy(&ck, g_tree.blob.data() + cf.start_offset, sz);
                                            std::string sp, lbl;
                                            bool isPet = g_petKeySet.count((int)ck) != 0;
                                            for (auto& pd : g_petList) if (pd.characterKey == (int)ck) { sp = pd.species; lbl = pd.name; break; }
                                            if (lbl.empty()) { const char* dn = LookupFieldName("_characterKey", ck); lbl = dn ? dn : ("ID " + std::to_string((int)ck)); }
                                            ownedMercs.push_back({(int)ck, lbl, sp, el.start_offset, el.end_offset, isPet});
                                        }
                                    }
                                }
                            }
                        }
                        break;
                    }
                    // Pets/known first so a same-species donor is easy to pick.
                    std::stable_sort(ownedMercs.begin(), ownedMercs.end(),
                        [](const OwnedMerc& a, const OwnedMerc& b){ return a.isPet > b.isPet; });
                    petsScanned = true;
                }

                static int selectedDonor = -1;
                static bool showOwnableOnly = true;
                static char petFilter[64] = {};

                ImGui::TextColored(ImVec4(1.0f, 0.7f, 0.3f, 1.0f), "Add Pet");
                ImGui::TextWrapped(
                    "Pets live in the same list as mounts/mercenaries; the species is set purely by character key.\n"
                    "Step 1: Pick a donor from your save (the new pet copies its record structure). An existing dog/cat is the safest donor.\n"
                    "Step 2: Pick the pet to add. EXPERIMENTAL pets (Wild/Battle, or vehicle types) may not behave as pets in-game.");
                ImGui::Separator();

                if (!g_statusMsg.empty())
                    ImGui::TextColored(ImVec4(0.4f, 0.8f, 0.4f, 1.0f), "%s", g_statusMsg.c_str());
                if (g_dirty) {
                    ImGui::SameLine();
                    ImGui::TextColored(ImVec4(1.0f, 0.4f, 0.3f, 1.0f), " [MODIFIED]");
                    ImGui::SameLine();
                    if (ImGui::SmallButton("Save (Ctrl+S)")) DoSave();
                }

                ImGui::TextColored(ImVec4(0.5f, 0.8f, 1.0f, 1.0f), "Donor records in your save (%d):", (int)ownedMercs.size());
                ImGui::SameLine();
                if (ImGui::SmallButton("Rescan")) { petsScanned = false; }
                ImGui::BeginChild("OwnedMercs", ImVec2(0, 130), true);
                for (int di = 0; di < (int)ownedMercs.size(); di++) {
                    auto& om = ownedMercs[di];
                    char label[256];
                    snprintf(label, sizeof(label), "%s%d - %s%s%s (%u bytes)",
                        om.isPet ? "[PET] " : "", om.characterKey, om.label.c_str(),
                        om.species.empty() ? "" : "  ", om.species.c_str(), om.elemEnd - om.elemStart);
                    if (ImGui::Selectable(label, selectedDonor == di))
                        selectedDonor = di;
                }
                ImGui::EndChild();

                if (selectedDonor >= 0 && selectedDonor < (int)ownedMercs.size()) {
                    auto& d = ownedMercs[selectedDonor];
                    ImGui::TextColored(ImVec4(0.2f, 1.0f, 0.2f, 1.0f), "Donor: %s (key=%d%s%s)",
                        d.label.c_str(), d.characterKey, d.species.empty()?"":", ", d.species.c_str());
                } else {
                    ImGui::TextColored(ImVec4(1.0f, 0.4f, 0.3f, 1.0f), "Select a donor above first! (an existing dog/cat is safest)");
                }

                ImGui::Separator();
                ImGui::Checkbox("Show only ownable (Friendly + Domestic)", &showOwnableOnly);
                ImGui::SameLine();
                ImGui::SetNextItemWidth(260);
                ImGui::InputText("Filter", petFilter, sizeof(petFilter));

                std::string pfLower = petFilter;
                std::transform(pfLower.begin(), pfLower.end(), pfLower.begin(), ::tolower);

                int shown = 0;
                if (ImGui::BeginTable("Pets", 5,
                    ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                    ImGuiTableFlags_ScrollY | ImGuiTableFlags_Resizable, ImVec2(0, 0))) {
                    ImGui::TableSetupScrollFreeze(0, 1);
                    ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 75);
                    ImGui::TableSetupColumn("Name", ImGuiTableColumnFlags_WidthStretch);
                    ImGui::TableSetupColumn("Species", ImGuiTableColumnFlags_WidthFixed, 90);
                    ImGui::TableSetupColumn("Role", ImGuiTableColumnFlags_WidthFixed, 80);
                    ImGui::TableSetupColumn("Add", ImGuiTableColumnFlags_WidthFixed, 60);
                    ImGui::TableHeadersRow();

                    for (int pi = 0; pi < (int)g_petList.size(); pi++) {
                        auto& p = g_petList[pi];
                        bool ownable = (p.role == "Friendly" || p.role == "Domestic");
                        if (showOwnableOnly && !ownable) continue;
                        if (petFilter[0] != 0) {
                            std::string nLower = p.name + " " + p.species + " " + p.role;
                            std::transform(nLower.begin(), nLower.end(), nLower.begin(), ::tolower);
                            std::string kStr = std::to_string(p.characterKey);
                            if (nLower.find(pfLower) == std::string::npos && kStr.find(pfLower) == std::string::npos)
                                continue;
                        }
                        shown++;
                        ImGui::TableNextRow();
                        ImGui::PushID(pi + 90000);
                        ImGui::TableSetColumnIndex(0); ImGui::Text("%d", p.characterKey);
                        ImGui::TableSetColumnIndex(1);
                        ImGui::Text("%s", p.name.c_str());
                        if (p.vehicleInfo != 0) { ImGui::SameLine(); ImGui::TextColored(ImVec4(1,0.7f,0.2f,1), "(mount)"); }
                        ImGui::TableSetColumnIndex(2); ImGui::Text("%s", p.species.c_str());
                        ImGui::TableSetColumnIndex(3);
                        ImVec4 rc = p.role=="Friendly" ? ImVec4(0.4f,1,0.5f,1)
                                  : p.role=="Domestic" ? ImVec4(0.7f,0.9f,1,1)
                                  : ImVec4(1,0.5f,0.4f,1);
                        ImGui::TextColored(rc, "%s", p.role.c_str());
                        ImGui::TableSetColumnIndex(4);
                        bool canAdd = (selectedDonor >= 0 && selectedDonor < (int)ownedMercs.size());
                        if (!canAdd) ImGui::BeginDisabled();
                        if (ImGui::SmallButton("Add")) {
                            auto& donor = ownedMercs[selectedDonor];
                            std::vector<uint8_t> elemBytes(
                                g_tree.blob.begin() + donor.elemStart,
                                g_tree.blob.begin() + donor.elemEnd);
                            // Fresh unique _mercenaryNo
                            uint32_t maxMN = 0;
                            for (auto& obj2 : g_tree.parsed.objects) {
                                if (obj2.class_name.find("MercenaryClan") == std::string::npos) continue;
                                for (auto& fld2 : obj2.fields) {
                                    if (fld2.name != "_mercenaryDataList") continue;
                                    for (auto& el2 : fld2.list_elements)
                                        for (auto& cf2 : el2.child_fields)
                                            if (cf2.name == "_mercenaryNo" && cf2.present &&
                                                cf2.start_offset > 0 && cf2.end_offset > cf2.start_offset) {
                                                uint32_t sz2 = cf2.end_offset - cf2.start_offset;
                                                if (sz2 <= 8 && cf2.start_offset + sz2 <= g_tree.blob.size()) {
                                                    uint64_t mno = 0;
                                                    memcpy(&mno, g_tree.blob.data() + cf2.start_offset, sz2);
                                                    if ((uint32_t)mno > maxMN) maxMN = (uint32_t)mno;
                                                }
                                            }
                                }
                            }
                            // Patch _mercenaryNo + _characterKey inside the clone
                            for (auto& obj2 : g_tree.parsed.objects) {
                                if (obj2.class_name.find("MercenaryClan") == std::string::npos) continue;
                                for (auto& fld2 : obj2.fields) {
                                    if (fld2.name != "_mercenaryDataList") continue;
                                    for (auto& el2 : fld2.list_elements) {
                                        if (el2.start_offset != donor.elemStart) continue;
                                        for (auto& cf2 : el2.child_fields) {
                                            uint32_t relOff = cf2.start_offset - donor.elemStart;
                                            uint32_t sz2 = cf2.end_offset - cf2.start_offset;
                                            if (!cf2.present || sz2 > 8 || relOff + sz2 > elemBytes.size()) continue;
                                            if (cf2.name == "_mercenaryNo") {
                                                uint64_t nv = (uint64_t)(maxMN + 1);
                                                memcpy(elemBytes.data() + relOff, &nv, sz2);
                                            } else if (cf2.name == "_characterKey") {
                                                int64_t nv = (int64_t)p.characterKey;
                                                memcpy(elemBytes.data() + relOff, &nv, sz2);
                                            }
                                        }
                                    }
                                }
                            }
                            auto result = ParcEngine::SpliceIntoList(g_tree,
                                "MercenaryClanSaveData", "_mercenaryDataList", elemBytes, donor.elemStart);
                            if (result.ok) {
                                g_searchHits.clear(); g_activeSearch[0] = 0; g_treeSearch[0] = 0;
                                g_navPending = false; g_listPage.clear(); g_skipTreeFrames = 3;
                                ExtractItems();
                                g_dirty = true; petsScanned = false;
                                g_statusMsg = "Added " + p.name + " (cloned from " + donor.label + ")! Save with Ctrl+S.";
                            } else {
                                g_statusMsg = "Failed: " + result.error;
                            }
                        }
                        if (!canAdd) ImGui::EndDisabled();
                        ImGui::PopID();
                    }
                    ImGui::EndTable();
                }
            }
            ImGui::EndChild();
            ImGui::EndTabItem();
        }
        if (g_skipTreeFrames > 0) g_skipTreeFrames--;

        // ── DATABASE TAB ──
        if (TabOn("Database") && ImGui::BeginTabItem("Database")) {
            static char g_dbSearch[128] = {};
            static char g_dbCatFilter[64] = {};
            static std::vector<std::pair<int,std::string>> g_dbResults;
            static std::vector<int> g_packBuilder;
            static char g_packName[128] = "My Pack";

            // Sync button
            if (ImGui::Button("Sync Item DB from GitHub")) {
                g_statusMsg = "Downloading item_names.json...";
                try {
                    // Use WinHTTP to download
                    std::string url = "https://raw.githubusercontent.com/NattKh/CrimsonDesertCommunityItemMapping/main/item_names.json";
                    // Simple: use URLDownloadToFile
                    std::string destPath;
                    for (auto& c : { g_dataDir + "data\\item_names.json", std::string("data\\item_names.json") }) {
                        if (std::filesystem::exists(std::filesystem::path(c).parent_path())) { destPath = c; break; }
                    }
                    if (!destPath.empty()) {
                        HRESULT hr = URLDownloadToFileA(nullptr, url.c_str(), destPath.c_str(), 0, nullptr);
                        if (SUCCEEDED(hr)) {
                            g_itemDB.clear();
                            LoadItemDB(destPath);
                            g_statusMsg = "Synced! " + std::to_string(g_itemDB.size()) + " items loaded.";
                        } else {
                            g_statusMsg = "Download failed (HRESULT=" + std::to_string(hr) + ")";
                        }
                    }
                } catch (...) { g_statusMsg = "Sync error"; }
            }
            ImGui::SameLine();
            // Self-update straight from the local game install (PAZ -> iteminfo
            // -> bundled localization). Needs the game path set in Settings.
            if (ImGui::Button("Sync from Game (Local)")) {
                std::string destPath;
                for (auto& c : { g_dataDir + "data\\item_names.json", std::string("data\\item_names.json") }) {
                    if (std::filesystem::exists(std::filesystem::path(c).parent_path())) { destPath = c; break; }
                }
                if (destPath.empty()) destPath = "data\\item_names.json";
                g_statusMsg = "Extracting items from game client...";
                auto [ok, msg] = g_settings.SyncItemsFromLocal(g_dataDir, destPath);
                if (ok) {
                    g_itemDB.clear();
                    LoadItemDB(destPath);
                }
                g_statusMsg = msg;
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Rebuild item_names.json from your installed game files.\n"
                                  "Picks up items added by game updates. Set the game path in Settings first.");
            ImGui::SameLine();
            ImGui::Text("%zu items in database", g_itemDB.size());

            ImGui::Separator();

            // Search
            ImGui::SetNextItemWidth(300);
            if (ImGui::InputText("Search##db", g_dbSearch, sizeof(g_dbSearch))) {
                g_dbResults.clear();
                if (strlen(g_dbSearch) >= 2) {
                    // Try numeric key
                    int numKey = atoi(g_dbSearch);
                    if (numKey > 0) {
                        auto it = g_itemDB.find(numKey);
                        if (it != g_itemDB.end()) {
                            g_dbResults.push_back({numKey, it->second.name + " [" + it->second.category + "]"});
                        }
                    }
                    // Text search
                    std::string needle(g_dbSearch);
                    for (auto& c : needle) c = (char)tolower(c);
                    for (auto& [key, def] : g_itemDB) {
                        std::string lower = def.name;
                        for (auto& c : lower) c = (char)tolower(c);
                        if (lower.find(needle) != std::string::npos) {
                            g_dbResults.push_back({key, def.name + " [" + def.category + "]"});
                            if (g_dbResults.size() >= 200) break;
                        }
                    }
                }
            }

            // Results table with "Add to Pack" button
            if (ImGui::BeginTable("DBTable", 4,
                ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg | ImGuiTableFlags_ScrollY,
                ImVec2(0, ImGui::GetContentRegionAvail().y * 0.5f))) {
                ImGui::TableSetupScrollFreeze(0, 1);
                ImGui::TableSetupColumn("##icon", ImGuiTableColumnFlags_WidthFixed, 24);
                ImGui::TableSetupColumn("Item", ImGuiTableColumnFlags_WidthStretch);
                ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 70);
                ImGui::TableSetupColumn("Pack", ImGuiTableColumnFlags_WidthFixed, 40);
                ImGui::TableHeadersRow();

                for (auto& [key, display] : g_dbResults) {
                    ImGui::TableNextRow(0, 24);
                    ImGui::PushID(key + 300000);
                    ImGui::TableNextColumn();
                    auto* icon = IconCache::Get(key);
                    if (icon) ImGui::Image((ImTextureID)icon, ImVec2(20, 20));
                    ImGui::TableNextColumn();
                    ImGui::Text("%s", display.c_str());
                    ImGui::TableNextColumn();
                    ImGui::Text("%d", key);
                    ImGui::TableNextColumn();
                    bool inPack = false;
                    for (auto k : g_packBuilder) if (k == key) { inPack = true; break; }
                    if (inPack) {
                        ImGui::TextColored(ImVec4(0.3f,1,0.3f,1), "OK");
                    } else if (ImGui::SmallButton("+")) {
                        g_packBuilder.push_back(key);
                    }
                    ImGui::PopID();
                }
                ImGui::EndTable();
            }

            // Pack builder
            ImGui::Separator();
            ImGui::Text("Pack Builder (%zu items):", g_packBuilder.size());
            ImGui::SameLine();
            ImGui::SetNextItemWidth(200);
            ImGui::InputText("Pack Name", g_packName, sizeof(g_packName));
            ImGui::SameLine();
            if (g_packBuilder.size() > 0 && ImGui::Button("Save Pack")) {
                json pj;
                pj["name"] = g_packName;
                pj["author"] = "User";
                pj["version"] = 1;
                pj["items"] = json::array();
                for (auto k : g_packBuilder) {
                    json item;
                    item["item_key"] = k;
                    auto it = g_itemDB.find(k);
                    if (it != g_itemDB.end()) item["name"] = it->second.name;
                    pj["items"].push_back(item);
                }
                // Save to packs folder
                std::string packDir = "data\\packs";
                if (!std::filesystem::exists(packDir)) std::filesystem::create_directories(packDir);
                std::string safeName(g_packName);
                for (auto& c : safeName) if (c == ' ' || c == '/' || c == '\\') c = '_';
                std::string packPath = packDir + "\\" + safeName + ".json";
                std::ofstream pf(packPath);
                pf << pj.dump(2);
                g_statusMsg = "Pack saved: " + packPath + " (" + std::to_string(g_packBuilder.size()) + " items)";
            }
            ImGui::SameLine();
            if (ImGui::Button("Clear")) g_packBuilder.clear();

            // Show pack contents
            if (!g_packBuilder.empty()) {
                ImGui::BeginChild("##packcontents", ImVec2(0, 0), true);
                for (int pi = 0; pi < (int)g_packBuilder.size(); pi++) {
                    int k = g_packBuilder[pi];
                    auto it = g_itemDB.find(k);
                    auto* icon = IconCache::Get(k);
                    ImGui::PushID(pi + 400000);
                    if (icon) { ImGui::Image((ImTextureID)icon, ImVec2(16, 16)); ImGui::SameLine(); }
                    ImGui::Text("[%d] %s", k, it != g_itemDB.end() ? it->second.name.c_str() : "?");
                    ImGui::SameLine();
                    if (ImGui::SmallButton("X")) {
                        g_packBuilder.erase(g_packBuilder.begin() + pi);
                        pi--;
                    }
                    ImGui::PopID();
                }
                ImGui::EndChild();
            }

            ImGui::EndTabItem();
        }

        if (TabOn("Quests") && ImGui::BeginTabItem("Quests")) {
            if (!g_saveLoaded) {
                ImGui::Text("Load a save first.");
            } else {
                QuestEditor::RenderQuestTab(g_tree, g_dirty);
            }
            ImGui::EndTabItem();
        }

        if (TabOn("Knowledge") && ImGui::BeginTabItem("Knowledge")) {
            if (!g_saveLoaded) {
                ImGui::Text("Load a save first.");
            } else {
                KnowledgeEditor::RenderKnowledgeTab(g_tree, g_dirty);
            }
            ImGui::EndTabItem();
        }

        if (TabOn("Dye") && ImGui::BeginTabItem("Dye")) {
            if (!g_saveLoaded) {
                ImGui::Text("Load a save first.");
            } else {
                DyeEditor::RenderDyeTab(g_tree, g_dirty);
            }
            ImGui::EndTabItem();
        }

        if (TabOn("World") && ImGui::BeginTabItem("World")) {
            if (!g_saveLoaded) { ImGui::Text("Load a save first."); }
            else { WorldEditor::RenderWorldTab(g_tree, g_dirty); }
            ImGui::EndTabItem();
        }

        if (TabOn("Stores") && ImGui::BeginTabItem("Stores")) {
            if (!g_saveLoaded) { ImGui::Text("Load a save first."); }
            else { WorldEditor::RenderStoresTab(g_tree, g_dirty); }
            ImGui::EndTabItem();
        }

        if (TabOn("Waypoints") && ImGui::BeginTabItem("Waypoints")) {
            if (!g_saveLoaded) { ImGui::Text("Load a save first."); }
            else { WorldEditor::RenderWaypointsTab(g_tree, g_dirty); }
            ImGui::EndTabItem();
        }

        if (TabOn("Misc") && ImGui::BeginTabItem("Misc")) {
            if (!g_saveLoaded) { ImGui::Text("Load a save first."); }
            else { WorldEditor::RenderMiscTab(g_tree, g_dirty); }
            ImGui::EndTabItem();
        }

        if (TabOn("Appearance") && ImGui::BeginTabItem("Appearance")) {
            if (!g_saveLoaded) { ImGui::Text("Load a save first."); }
            else { AppearanceEditor::RenderAppearanceTab(g_tree, g_dirty); }
            ImGui::EndTabItem();
        }

        if (TabOn("Repair") && ImGui::BeginTabItem("Repair")) {
            if (!g_saveLoaded) { ImGui::Text("Load a save first."); }
            else { SaveRepair::RenderRepairTab(g_tree, g_dirty, g_savePath); }
            ImGui::EndTabItem();
        }

        if (TabOn("Transplant") && ImGui::BeginTabItem("Transplant")) {
            if (!g_saveLoaded) { ImGui::Text("Load a save first."); }
            else { TransplantEditor::RenderTransplantTab(g_tree, g_dirty, g_savePath); }
            ImGui::EndTabItem();
        }

        if (TabOn("Save Tree") && ImGui::BeginTabItem("Save Tree")) {
            if (g_skipTreeFrames > 0) {
                ImGui::Text("Refreshing...");
            } else {
                RenderSaveTree();
            }
            ImGui::EndTabItem();
        }
        if (TabOn("Dual Tree") && ImGui::BeginTabItem("Dual Tree")) {
            // Dual tree: source (left) + target/current (right) with replace
            ImGui::BeginChild("DualTreeArea", ImVec2(0, 0), false);

            // Top bar: load source + replace button
            if (ImGui::Button("Load Source Save")) {
                OPENFILENAMEA ofn3 = {};
                char path3[MAX_PATH] = {};
                ofn3.lStructSize = sizeof(ofn3);
                ofn3.lpstrFilter = "Save files (*.save)\0*.save\0All files\0*.*\0";
                ofn3.lpstrFile = path3;
                ofn3.nMaxFile = MAX_PATH;
                ofn3.Flags = OFN_FILEMUSTEXIST;
                ofn3.lpstrTitle = "Load Source Save";
                if (GetOpenFileNameA(&ofn3)) {
                    try {
                        g_srcTree = ParcEngine::LoadSave(path3);
                        g_srcLoaded = true;
                        g_srcPath = path3;
                        g_srcSelectStart = g_srcSelectEnd = 0;
                        g_statusMsg = "Source loaded: " + std::string(path3);
                    } catch (const std::exception& e) {
                        g_statusMsg = "Source load error: " + std::string(e.what());
                    }
                }
            }

            if (g_srcLoaded) {
                ImGui::SameLine();
                ImGui::Text("Source: %s", fs::path(g_srcPath).filename().string().c_str());
            }

            // Selection display + replace button
            if (g_srcSelectStart > 0 && g_dstSelectStart > 0) {
                ImGui::TextColored(ImVec4(0.2f, 1.0f, 0.2f, 1.0f),
                    "Source: %s (%u bytes)", g_srcSelectLabel.c_str(), g_srcSelectEnd - g_srcSelectStart);
                ImGui::SameLine();
                ImGui::TextColored(ImVec4(1.0f, 0.7f, 0.3f, 1.0f),
                    "  ->  Target: %s (%u bytes)", g_dstSelectLabel.c_str(), g_dstSelectEnd - g_dstSelectStart);
                ImGui::SameLine();

                bool sameSize = (g_srcSelectEnd - g_srcSelectStart) == (g_dstSelectEnd - g_dstSelectStart);
                char replaceLabel[64];
                if (sameSize)
                    snprintf(replaceLabel, sizeof(replaceLabel), "Replace (same size, safe)");
                else
                    snprintf(replaceLabel, sizeof(replaceLabel), "Replace (%u -> %u bytes)",
                        g_dstSelectEnd - g_dstSelectStart, g_srcSelectEnd - g_srcSelectStart);

                if (ImGui::Button(replaceLabel)) {
                    // Copy source bytes
                    std::vector<uint8_t> srcBytes(
                        g_srcTree.blob.begin() + g_srcSelectStart,
                        g_srcTree.blob.begin() + g_srcSelectEnd);

                    if (sameSize) {
                        // Same size: direct overwrite + PO fix
                        memcpy(g_tree.blob.data() + g_dstSelectStart, srcBytes.data(), srcBytes.size());
                        static const uint8_t SENT8[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
                        for (uint32_t p = g_dstSelectStart; p + 12 <= g_dstSelectEnd; ++p) {
                            if (memcmp(g_tree.blob.data() + p, SENT8, 8) == 0) {
                                uint32_t po_pos = p + 8;
                                uint32_t correct = po_pos + 4;
                                memcpy(g_tree.blob.data() + po_pos, &correct, 4);
                            }
                        }
                        g_dirty = true;
                        g_statusMsg = "Replaced (same size, safe)! Save with Ctrl+S.";
                    } else {
                        // Different size: deferred ReplaceElement
                        g_pendingDuplicate = true;
                        g_pendingDupBlock = ""; // not used — ReplaceElement finds by offset
                        g_pendingDupField = "_REPLACE_";
                        g_pendingDupBytes = srcBytes;
                        g_pendingDupSrcOffset = g_dstSelectStart;
                        g_pendingReplaceEnd = g_dstSelectEnd;
                        g_statusMsg = "Replacing (resize)...";
                    }
                    g_srcSelectStart = g_srcSelectEnd = 0;
                    g_dstSelectStart = g_dstSelectEnd = 0;
                }
            } else {
                if (g_srcLoaded && g_saveLoaded)
                    ImGui::TextDisabled("Right-click nodes to select source (left) and target (right)");
            }

            ImGui::Separator();

            if (!g_srcLoaded || !g_saveLoaded) {
                ImGui::Text("Load both a main save (left browser) and a source save (button above).");
            } else {
                // Split view
                float halfW = ImGui::GetContentRegionAvail().x * 0.5f - 4;

                // LEFT: Source save tree
                ImGui::BeginChild("SrcTree", ImVec2(halfW, 0), true);
                ImGui::TextColored(ImVec4(0.5f, 0.8f, 1.0f, 1.0f), "SOURCE: %s",
                    fs::path(g_srcPath).filename().string().c_str());
                ImGui::Separator();

                for (size_t bi = 0; bi < g_srcTree.parsed.objects.size(); bi++) {
                    auto& obj = g_srcTree.parsed.objects[bi];
                    if (obj.fields.empty()) continue;
                    ImGui::PushID((int)(bi + 500000));

                    int listCount = 0;
                    for (auto& f3 : obj.fields) listCount += (int)f3.list_elements.size();

                    char blbl[256];
                    snprintf(blbl, sizeof(blbl), "[%zu] %s (%d elems)###sb%zu",
                        bi, obj.class_name.c_str(), listCount, bi);
                    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(1.0f, 0.7f, 0.3f, 1.0f));
                    bool bopen = ImGui::TreeNodeEx(blbl, ImGuiTreeNodeFlags_SpanAvailWidth);
                    ImGui::PopStyleColor();

                    if (bopen) {
                        // Render fields with select-on-right-click
                        std::function<void(std::vector<SaveParserCpp::GenericFieldValue>&, int)> renderSrc;
                        renderSrc = [&](std::vector<SaveParserCpp::GenericFieldValue>& fields, int depth) {
                            if (depth > 10) return;
                            for (size_t fi = 0; fi < fields.size(); fi++) {
                                auto& f3 = fields[fi];
                                ImGui::PushID((int)(f3.start_offset ^ (fi << 16)));
                                bool hasKids = !f3.child_fields.empty() || !f3.list_elements.empty();
                                ImGuiTreeNodeFlags fl = ImGuiTreeNodeFlags_SpanAvailWidth;
                                if (!hasKids) fl |= ImGuiTreeNodeFlags_Leaf | ImGuiTreeNodeFlags_NoTreePushOnOpen;

                                bool selected = (g_srcSelectStart == f3.start_offset && g_srcSelectEnd == f3.end_offset && g_srcSelectStart > 0);
                                if (selected) ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.2f, 1.0f, 0.2f, 1.0f));

                                char flbl[256];
                                uint32_t sz3 = f3.end_offset - f3.start_offset;
                                snprintf(flbl, sizeof(flbl), "%s (%u B)###sf%zu", f3.name.c_str(), sz3, fi);
                                bool fopen = hasKids ? ImGui::TreeNodeEx(flbl, fl) : false;
                                if (!hasKids) ImGui::TreeNodeEx(flbl, fl);

                                if (selected) ImGui::PopStyleColor();

                                // Right-click to select as source
                                if (ImGui::BeginPopupContextItem("##srcctx")) {
                                    if (f3.start_offset > 0 && f3.end_offset > f3.start_offset) {
                                        char selLbl[128];
                                        snprintf(selLbl, sizeof(selLbl), "Select as Source (%u bytes)", sz3);
                                        if (ImGui::MenuItem(selLbl)) {
                                            g_srcSelectStart = f3.start_offset;
                                            g_srcSelectEnd = f3.end_offset;
                                            g_srcSelectLabel = f3.name;
                                        }
                                    }
                                    ImGui::EndPopup();
                                }

                                if (fopen && hasKids) {
                                    if (!f3.child_fields.empty()) renderSrc(f3.child_fields, depth + 1);
                                    if (!f3.list_elements.empty()) {
                                        size_t cnt = f3.list_elements.size();
                                        size_t show = cnt > 50 ? 50 : cnt;
                                        for (size_t ei = 0; ei < show; ei++) {
                                            std::vector<SaveParserCpp::GenericFieldValue> tmp = {f3.list_elements[ei]};
                                            renderSrc(tmp, depth + 1);
                                        }
                                        if (cnt > 50) ImGui::Text("... %zu more", cnt - 50);
                                    }
                                    ImGui::TreePop();
                                }
                                ImGui::PopID();
                            }
                        };
                        renderSrc(obj.fields, 0);
                        ImGui::TreePop();
                    }
                    ImGui::PopID();
                }
                ImGui::EndChild();

                ImGui::SameLine();

                // RIGHT: Current/target save tree
                ImGui::BeginChild("DstTree", ImVec2(halfW, 0), true);
                ImGui::TextColored(ImVec4(1.0f, 0.7f, 0.3f, 1.0f), "TARGET: %s",
                    fs::path(g_savePath).filename().string().c_str());
                ImGui::Separator();

                for (size_t bi = 0; bi < g_tree.parsed.objects.size(); bi++) {
                    auto& obj = g_tree.parsed.objects[bi];
                    if (obj.fields.empty()) continue;
                    ImGui::PushID((int)(bi + 600000));

                    int listCount = 0;
                    for (auto& f3 : obj.fields) listCount += (int)f3.list_elements.size();

                    char blbl[256];
                    snprintf(blbl, sizeof(blbl), "[%zu] %s (%d elems)###db%zu",
                        bi, obj.class_name.c_str(), listCount, bi);
                    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(1.0f, 0.7f, 0.3f, 1.0f));
                    bool bopen = ImGui::TreeNodeEx(blbl, ImGuiTreeNodeFlags_SpanAvailWidth);
                    ImGui::PopStyleColor();

                    if (bopen) {
                        std::function<void(std::vector<SaveParserCpp::GenericFieldValue>&, int)> renderDst;
                        renderDst = [&](std::vector<SaveParserCpp::GenericFieldValue>& fields, int depth) {
                            if (depth > 10) return;
                            for (size_t fi = 0; fi < fields.size(); fi++) {
                                auto& f3 = fields[fi];
                                ImGui::PushID((int)(f3.start_offset ^ (fi << 16) ^ 0x80000000));
                                bool hasKids = !f3.child_fields.empty() || !f3.list_elements.empty();
                                ImGuiTreeNodeFlags fl = ImGuiTreeNodeFlags_SpanAvailWidth;
                                if (!hasKids) fl |= ImGuiTreeNodeFlags_Leaf | ImGuiTreeNodeFlags_NoTreePushOnOpen;

                                bool selected = (g_dstSelectStart == f3.start_offset && g_dstSelectEnd == f3.end_offset && g_dstSelectStart > 0);
                                if (selected) ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(1.0f, 0.4f, 0.3f, 1.0f));

                                char flbl[256];
                                uint32_t sz3 = f3.end_offset - f3.start_offset;
                                snprintf(flbl, sizeof(flbl), "%s (%u B)###df%zu", f3.name.c_str(), sz3, fi);
                                bool fopen = hasKids ? ImGui::TreeNodeEx(flbl, fl) : false;
                                if (!hasKids) ImGui::TreeNodeEx(flbl, fl);

                                if (selected) ImGui::PopStyleColor();

                                // Right-click to select as target
                                if (ImGui::BeginPopupContextItem("##dstctx")) {
                                    if (f3.start_offset > 0 && f3.end_offset > f3.start_offset) {
                                        char selLbl[128];
                                        snprintf(selLbl, sizeof(selLbl), "Select as Target (%u bytes)", sz3);
                                        if (ImGui::MenuItem(selLbl)) {
                                            g_dstSelectStart = f3.start_offset;
                                            g_dstSelectEnd = f3.end_offset;
                                            g_dstSelectLabel = f3.name;
                                        }
                                    }
                                    ImGui::EndPopup();
                                }

                                if (fopen && hasKids) {
                                    if (!f3.child_fields.empty()) renderDst(f3.child_fields, depth + 1);
                                    if (!f3.list_elements.empty()) {
                                        size_t cnt = f3.list_elements.size();
                                        size_t show = cnt > 50 ? 50 : cnt;
                                        for (size_t ei = 0; ei < show; ei++) {
                                            std::vector<SaveParserCpp::GenericFieldValue> tmp = {f3.list_elements[ei]};
                                            renderDst(tmp, depth + 1);
                                        }
                                        if (cnt > 50) ImGui::Text("... %zu more", cnt - 50);
                                    }
                                    ImGui::TreePop();
                                }
                                ImGui::PopID();
                            }
                        };
                        renderDst(obj.fields, 0);
                        ImGui::TreePop();
                    }
                    ImGui::PopID();
                }
                ImGui::EndChild();
            }

            ImGui::EndChild();
            ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    }
    ImGui::EndChild();

    ImGui::End();
}

// ── D3D11 boilerplate ──

static bool CreateDeviceD3D(HWND hWnd) {
    DXGI_SWAP_CHAIN_DESC sd = {};
    sd.BufferCount = 2;
    sd.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    sd.BufferDesc.RefreshRate = {60, 1};
    sd.Flags = DXGI_SWAP_CHAIN_FLAG_ALLOW_MODE_SWITCH;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.OutputWindow = hWnd;
    sd.SampleDesc = {1, 0};
    sd.Windowed = TRUE;
    sd.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    if (D3D11CreateDeviceAndSwapChain(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0,
        &fl, 1, D3D11_SDK_VERSION, &sd, &g_pSwapChain,
        &g_pd3dDevice, nullptr, &g_pd3dDeviceContext) != S_OK) return false;
    CreateRenderTarget();
    return true;
}
static void CleanupDeviceD3D() {
    CleanupRenderTarget();
    if (g_pSwapChain) g_pSwapChain->Release();
    if (g_pd3dDeviceContext) g_pd3dDeviceContext->Release();
    if (g_pd3dDevice) g_pd3dDevice->Release();
    g_pSwapChain = nullptr; g_pd3dDeviceContext = nullptr; g_pd3dDevice = nullptr;
}
static void CreateRenderTarget() {
    ID3D11Texture2D* bb = nullptr;
    g_pSwapChain->GetBuffer(0, IID_PPV_ARGS(&bb));
    if (bb) { g_pd3dDevice->CreateRenderTargetView(bb, nullptr, &g_mainRenderTargetView); bb->Release(); }
}
static void CleanupRenderTarget() {
    if (g_mainRenderTargetView) { g_mainRenderTargetView->Release(); g_mainRenderTargetView = nullptr; }
}
static LRESULT WINAPI WndProc(HWND hWnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    if (ImGui_ImplWin32_WndProcHandler(hWnd, msg, wParam, lParam)) return true;
    if (msg == WM_SIZE && wParam != SIZE_MINIMIZED) { g_ResizeWidth = LOWORD(lParam); g_ResizeHeight = HIWORD(lParam); return 0; }
    if (msg == WM_DESTROY) { PostQuitMessage(0); return 0; }
    return DefWindowProc(hWnd, msg, wParam, lParam);
}

// ── Entry ──

// ── CLI transplant mode ──
static int RunTransplantCLI(int argc, char* argv[]) {
    // Usage: exe --transplant <target.save> <donor.save> <BlockName> [BlockName2...]
    // Writes result to target.save (backs up to .bak first)
    if (argc < 5) {
        printf("Usage: %s --transplant <target.save> <donor.save> <Block> [Block2...]\n", argv[0]);
        printf("Example: %s --transplant slot100.save slot108.save EquipmentSaveData InventorySaveData\n", argv[0]);
        return 1;
    }

    std::string targetPath = argv[2];
    std::string donorPath = argv[3];

    printf("=== Save Transplant CLI ===\n");
    printf("Target: %s\n", targetPath.c_str());
    printf("Donor:  %s\n", donorPath.c_str());

    // Backup target
    std::string bakPath = targetPath + ".bak";
    if (std::ifstream(targetPath).good()) {
        std::filesystem::copy_file(targetPath, bakPath, std::filesystem::copy_options::overwrite_existing);
        printf("Backup: %s\n", bakPath.c_str());
    }

    // Load both saves
    printf("Loading target...\n");
    ParcEngine::SaveTree target;
    try { target = ParcEngine::LoadSave(targetPath); }
    catch (const std::exception& e) { printf("FAILED to load target: %s\n", e.what()); return 1; }

    printf("Loading donor...\n");
    ParcEngine::SaveTree donor;
    try { donor = ParcEngine::LoadSave(donorPath); }
    catch (const std::exception& e) { printf("FAILED to load donor: %s\n", e.what()); return 1; }

    printf("Target: %zuB blob, %zu types, %zu objects\n",
        target.blob.size(), target.parsed.schema.types.size(), target.parsed.objects.size());
    printf("Donor:  %zuB blob, %zu types, %zu objects\n",
        donor.blob.size(), donor.parsed.schema.types.size(), donor.parsed.objects.size());

    // Snapshot BEFORE state for comparison
    auto before_blob = target.blob;
    auto before_parsed = target.parsed;
    ParcEngine::SaveTree before_snapshot;
    before_snapshot.blob = before_blob;
    before_snapshot.parsed = before_parsed;

    // Dump full state BEFORE
    SaveRepair::DumpFullSaveState("BEFORE TRANSPLANT (target)", target);
    SaveRepair::DumpFullSaveState("DONOR SAVE", donor);

    // Collect block names being transplanted
    std::vector<std::string> transplanted_blocks;
    for (int i = 4; i < argc; i++) transplanted_blocks.push_back(argv[i]);

    // Transplant each requested block with direct byte replacement and reparse.
    EC::Log("Transplanting %d blocks...", argc - 4);
    int successCount = 0;
    for (int i = 4; i < argc; i++) {
        std::string blockName = argv[i];
        printf("\nTransplanting %s...\n", blockName.c_str());
        if (SaveRepair::TransplantBlock(target, donorPath, blockName)) {
            printf("  OK\n");
            successCount++;

            // Dump state after EACH transplant
            char label[128];
            snprintf(label, sizeof(label), "AFTER TRANSPLANT: %s", blockName.c_str());
            SaveRepair::DumpFullSaveState(label, target);
            SaveRepair::CompareStates(label, before_snapshot, before_blob, target, transplanted_blocks);
        } else {
            printf("  FAILED\n");
        }
    }

    if (successCount == 0) {
        printf("No blocks transplanted.\n");
        return 1;
    }

    printf("Transplanted %d block(s): %zu bytes\n", successCount, target.blob.size());

    // Final full comparison against original
    EC::Log("");
    SaveRepair::CompareStates("FINAL vs ORIGINAL", before_snapshot, before_blob, target, transplanted_blocks);

    // Verify roundtrip
    printf("Verifying roundtrip...\n");
    std::string report;
    SaveRepair::VerifyRoundtrip(target, report);
    printf("  %s\n", report.c_str());
    EC::Log("FINAL roundtrip: %s", report.c_str());

    // Save
    printf("Writing %s...\n", targetPath.c_str());
    try {
        ParcEngine::WriteSave(target, targetPath);
        printf("Done! File size: %zu bytes\n", std::filesystem::file_size(targetPath));
    } catch (const std::exception& e) {
        printf("FAILED to write: %s\n", e.what());
        return 1;
    }

    // Dump final save state
    SaveRepair::DumpFullSaveState("FINAL WRITTEN SAVE", target);

    return 0;
}

int WINAPI WinMain(HINSTANCE hInstance, HINSTANCE, LPSTR lpCmdLine, int) {
    // Check for CLI mode
    if (lpCmdLine && strstr(lpCmdLine, "--transplant")) {
        // Reparse command line into argc/argv
        int argc = 0;
        LPWSTR* wargv = CommandLineToArgvW(GetCommandLineW(), &argc);
        if (wargv && argc >= 5) {
            // Attach console for printf output
            AttachConsole(ATTACH_PARENT_PROCESS);
            if (GetStdHandle(STD_OUTPUT_HANDLE) == INVALID_HANDLE_VALUE) AllocConsole();
            freopen("CONOUT$", "w", stdout);
            freopen("CONOUT$", "w", stderr);

            std::vector<std::string> args_storage(argc);
            std::vector<char*> argv_ptrs(argc);
            for (int i = 0; i < argc; i++) {
                int sz = WideCharToMultiByte(CP_UTF8, 0, wargv[i], -1, nullptr, 0, nullptr, nullptr);
                args_storage[i].resize(sz);
                WideCharToMultiByte(CP_UTF8, 0, wargv[i], -1, &args_storage[i][0], sz, nullptr, nullptr);
                argv_ptrs[i] = &args_storage[i][0];
            }
            LocalFree(wargv);

            EC::LogInit("");
            return RunTransplantCLI(argc, argv_ptrs.data());
        }
        if (wargv) LocalFree(wargv);
    }

    char exePath[MAX_PATH];
    GetModuleFileNameA(nullptr, exePath, MAX_PATH);
    g_dataDir = std::string(exePath);
    g_dataDir = g_dataDir.substr(0, g_dataDir.find_last_of("\\/") + 1);

    EC::LogInit(g_dataDir);
    EC::Log("=== Crimson Desert Save Editor startup ===");
    EC::Log("Exe dir: %s", g_dataDir.c_str());

    EC::Log("Loading tab config...");
    LoadTabConfig(g_dataDir);

    EC::Log("Loading settings...");
    g_settings.Load();

    // Helper lambda: find + load with logging
    auto findData = [&](const char* name) -> std::string {
        std::string p = EC::FindDataFile(g_dataDir, name);
        EC::Log("  %s: %s", name, p.empty() ? "NOT FOUND" : p.c_str());
        return p;
    };

    EC::Log("Loading item database...");
    try {
        std::string p = findData("item_names.json");
        if (!p.empty()) LoadItemDB(p);
        EC::Log("  item_names: %d items", (int)g_itemDB.size());
    } catch (const std::exception& e) { EC::Log("  CRASH: %s", e.what()); }

    EC::Log("Loading store names...");
    try {
        std::string p = findData("store_names.json");
        if (!p.empty()) LoadStoreNames(p);
    } catch (const std::exception& e) { EC::Log("  CRASH: %s", e.what()); }

    EC::Log("Loading mount keys...");
    try {
        std::string p = findData("mount_keys.txt");
        if (!p.empty()) LoadMountKeys(p);
    } catch (const std::exception& e) { EC::Log("  CRASH: %s", e.what()); }

    EC::Log("Loading pet catalog...");
    try {
        std::string p = findData("pet_catalog.json");
        if (!p.empty()) LoadPetCatalog(p);
        EC::Log("  pet_catalog: %d pets", (int)g_petList.size());
    } catch (const std::exception& e) { EC::Log("  CRASH: %s", e.what()); }

    EC::Log("Loading key lookup...");
    try {
        std::string p = findData("key_lookup_index.json");
        if (!p.empty()) LoadKeyLookup(p);
    } catch (const std::exception& e) { EC::Log("  CRASH: %s", e.what()); }

    EC::Log("Loading character names...");
    try {
        std::string p = findData("character_names.json");
        if (!p.empty()) LoadCharacterLookup(p);
    } catch (const std::exception& e) { EC::Log("  CRASH: %s", e.what()); }

    EC::Log("Loading display names...");
    try { EC::LoadDisplayNames(g_dataDir); }
    catch (const std::exception& e) { EC::Log("  CRASH: %s", e.what()); }

    if (TabOn("Quests")) {
        EC::Log("Loading quest game data (8.4MB)...");
        try {
            std::string p = findData("quest_game_data.json");
            if (!p.empty()) QuestEditor::LoadGameData(p);
            EC::Log("  quest data loaded OK");
        } catch (const std::exception& e) { EC::Log("  CRASH: %s", e.what()); }
    } else {
        EC::Log("Quests tab disabled — skipping quest_game_data.json (saves ~8MB RAM + load time)");
    }

    if (TabOn("Knowledge")) {
        EC::Log("Loading knowledge data...");
        try {
            std::string keysPath = findData("knowledge_keys_all.json");
            std::string commPath = findData("community_knowledge_keys.json");
            std::string groupsPath = findData("knowledge_group_info.json");
            if (!keysPath.empty())
                KnowledgeEditor::LoadGameData(keysPath, commPath, groupsPath);
            EC::Log("  knowledge loaded OK");
        } catch (const std::exception& e) { EC::Log("  CRASH: %s", e.what()); }
    } else {
        EC::Log("Knowledge tab disabled — skipping knowledge data");
    }

    EC::Log("Scanning save files...");
    ScanSaveFiles();
    EC::Log("Startup complete. Creating window...");

    WNDCLASSEXW wc = {sizeof(wc), CS_CLASSDC, WndProc, 0, 0, hInstance, nullptr, nullptr, nullptr, nullptr, L"CrimsonSaveEditor", nullptr};
    RegisterClassExW(&wc);
    HWND hwnd = CreateWindowW(wc.lpszClassName, L"Crimson Desert Save Editor",
        WS_OVERLAPPEDWINDOW, 100, 100, 1400, 800, nullptr, nullptr, hInstance, nullptr);

    if (!CreateDeviceD3D(hwnd)) { CleanupDeviceD3D(); return 1; }

    // Init icon cache (webp→DX11 texture via WIC)
    for (auto& candidate : {
        g_dataDir + "icons_local",
        std::string("icons_local"),
        std::string("..\\icons_local")
    }) {
        if (std::filesystem::is_directory(candidate)) {
            IconCache::Init(g_pd3dDevice, candidate);
            break;
        }
    }

    ShowWindow(hwnd, SW_SHOWDEFAULT);

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;
    io.ConfigDebugHighlightIdConflicts = false;
    ImGui::StyleColorsDark();
    auto& style = ImGui::GetStyle();
    style.FrameRounding = 4.0f;
    style.GrabRounding = 4.0f;
    style.WindowRounding = 0.0f;

    // Default font size (can be changed at runtime)
    static float g_uiScale = 1.0f;
    io.Fonts->AddFontDefault();

    ImGui_ImplWin32_Init(hwnd);
    ImGui_ImplDX11_Init(g_pd3dDevice, g_pd3dDeviceContext);

    if (lpCmdLine && lpCmdLine[0]) {
        std::string arg = lpCmdLine;
        if (arg.front() == '"' && arg.back() == '"') arg = arg.substr(1, arg.size() - 2);
        DoLoadSave(arg);
    }

    const float cc[4] = {0.08f, 0.08f, 0.10f, 1.0f};
    MSG msg = {};
    while (msg.message != WM_QUIT) {
        if (PeekMessage(&msg, nullptr, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessage(&msg); continue; }
        if (g_ResizeWidth) { CleanupRenderTarget(); g_pSwapChain->ResizeBuffers(0, g_ResizeWidth, g_ResizeHeight, DXGI_FORMAT_UNKNOWN, 0); g_ResizeWidth = g_ResizeHeight = 0; CreateRenderTarget(); }
        ImGui_ImplDX11_NewFrame(); ImGui_ImplWin32_NewFrame(); ImGui::NewFrame();
        g_asyncTask.Update();

        // Process deferred duplicate BEFORE any rendering
        if (g_pendingDuplicate && g_saveLoaded) {
            g_pendingDuplicate = false;
            ParcEngine::InsertResult result;

            if (g_pendingDupField == "_REPLACE_") {
                LogMsg("DEFERRED_REPLACE: executing replace [0x%X..0x%X] with %zu bytes...",
                    g_pendingDupSrcOffset, g_pendingReplaceEnd, g_pendingDupBytes.size());
                result = ParcEngine::ReplaceElement(g_tree, g_pendingDupBlock,
                    g_pendingDupSrcOffset, g_pendingReplaceEnd, g_pendingDupBytes);
            } else {
                LogMsg("DEFERRED_DUP: executing splice...");
                result = ParcEngine::SpliceIntoList(g_tree, g_pendingDupBlock,
                    g_pendingDupField, g_pendingDupBytes, g_pendingDupSrcOffset);
            }
            if (result.ok) {
                LogMsg("DEFERRED_DUP: splice ok, saving + reloading...");
                EnsureBackup();
                ParcEngine::WriteSave(g_tree, g_savePath);
                g_tree = ParcEngine::LoadSave(g_savePath);
                g_saveLoaded = true;
                g_searchHits.clear();
                g_activeSearch[0] = 0;
                g_treeSearch[0] = 0;
                g_navPending = false;
                g_navTargetOffset = 0;
                g_listPage.clear();
                g_swapPopupOpen = false;
                g_swapSelectedItem = -1;
                g_skipTreeFrames = 3;
                g_vendorItems.clear();
                ExtractItems();
                ScanVendorItems();
                g_dirty = false;
                g_statusMsg = "Duplicated + auto-saved!";
                LogMsg("DEFERRED_DUP: complete");
            } else {
                g_statusMsg = "Duplicate failed: " + result.error;
                LogMsg("DEFERRED_DUP: FAILED: %s", result.error.c_str());
            }
            g_pendingDupBytes.clear();
        }

        // Process deferred XML node import (replace or append at recorded path)
        if (g_pendingXmlNodeImport && g_saveLoaded) {
            g_pendingXmlNodeImport = false;
            LogMsg("XML_IMPORT: %s at %s", g_pendingXmlNodeFile.c_str(), g_pendingXmlNodePath.c_str());
            auto err = ParcXml::ImportNodeXml(g_tree, g_pendingXmlNodeFile, g_pendingXmlNodePath);
            if (err.empty()) {
                // Self-check: reserialize must be stable before we persist
                auto re = ParcSerializer::Serialize(g_tree.parsed, g_tree.blob);
                if (re != g_tree.blob) {
                    err = "self-check failed (unstable reserialize) — NOT saved, reloading";
                    g_tree = ParcEngine::LoadSave(g_savePath);
                }
            }
            if (err.empty()) {
                EnsureBackup();
                ParcEngine::WriteSave(g_tree, g_savePath);
                g_tree = ParcEngine::LoadSave(g_savePath);
                g_saveLoaded = true;
                g_searchHits.clear();
                g_activeSearch[0] = 0;
                g_treeSearch[0] = 0;
                g_listPage.clear();
                g_skipTreeFrames = 3;
                g_vendorItems.clear();
                ExtractItems();
                ScanVendorItems();
                g_dirty = false;
                g_statusMsg = "XML node imported + saved!";
                LogMsg("XML_IMPORT: success");
            } else {
                g_statusMsg = "XML import failed: " + err;
                LogMsg("XML_IMPORT: FAILED: %s", err.c_str());
            }
        }

        // Process deferred whole-save XML import (does not require a loaded save)
        if (g_pendingXmlSaveImport) {
            g_pendingXmlSaveImport = false;
            LogMsg("XML_SAVE_IMPORT: %s -> %s",
                g_pendingXmlSaveFile.c_str(), g_pendingXmlSaveOut.c_str());
            std::vector<uint8_t> blob, header;
            auto err = ParcXml::ImportXml(g_pendingXmlSaveFile, blob, header);
            if (err.empty()) {
                try {
                    ParcEngine::SaveTree tree;
                    tree.blob = std::move(blob);
                    tree.is_encrypted = !header.empty();
                    tree.original_header = std::move(header);
                    ParcEngine::WriteSave(tree, g_pendingXmlSaveOut);
                    LogMsg("XML_SAVE_IMPORT: written %s, loading...",
                        g_pendingXmlSaveOut.c_str());

                    // Optionally pair it with a generated lobby.save so the
                    // folder is drag-and-drop ready as a save slot.
                    if (g_xmlImportWriteLobby) {
                        fs::path outp(g_pendingXmlSaveOut);
                        fs::path lobbyPath = outp.parent_path() / "lobby.save";
                        if (fs::exists(lobbyPath)) {
                            LogMsg("XML_SAVE_IMPORT: lobby.save already present, keeping it");
                        } else {
                            std::string slotName = outp.stem().string();
                            std::vector<uint8_t> lblob, lheader;
                            auto lerr = ParcXml::BuildLobbySave(slotName, lblob, lheader);
                            if (lerr.empty()) {
                                ParcEngine::SaveTree lt;
                                lt.blob = std::move(lblob);
                                lt.is_encrypted = true;
                                lt.original_header = std::move(lheader);
                                ParcEngine::WriteSave(lt, lobbyPath.string());
                                LogMsg("XML_SAVE_IMPORT: lobby.save written (name=%s)",
                                    slotName.c_str());
                            } else {
                                LogMsg("XML_SAVE_IMPORT: lobby.save FAILED: %s", lerr.c_str());
                            }
                        }
                    }
                    DoLoadSave(g_pendingXmlSaveOut);
                } catch (const std::exception& e) {
                    err = e.what();
                }
            }
            if (!err.empty()) {
                g_statusMsg = "XML save import failed: " + err;
                LogMsg("XML_SAVE_IMPORT: FAILED: %s", err.c_str());
            }
        }

        // Process deferred Create Item (uses InsertNested for tree-based insertion)
        if (g_pendingCreateItem && g_saveLoaded) {
            g_pendingCreateItem = false;
            LogMsg("CREATE_ITEM: inserting %zu bytes at %s", g_pendingCreateBytes.size(), g_pendingCreatePath.c_str());
            auto result = ParcEngine::InsertNested(g_tree, "InventorySaveData",
                g_pendingCreatePath, g_pendingCreateBytes, -1);
            if (result.ok) {
                EnsureBackup();
                ParcEngine::WriteSave(g_tree, g_savePath);
                g_tree = ParcEngine::LoadSave(g_savePath);
                g_saveLoaded = true;
                g_searchHits.clear();
                g_activeSearch[0] = 0;
                g_treeSearch[0] = 0;
                g_listPage.clear();
                g_swapPopupOpen = false;
                g_skipTreeFrames = 3;
                g_vendorItems.clear();
                ExtractItems();
                ScanVendorItems();
                g_dirty = false;
                g_statusMsg = "Item created + saved!";
                LogMsg("CREATE_ITEM: success at index %d", result.new_element_index);
            } else {
                g_statusMsg = "Create failed: " + result.error;
                LogMsg("CREATE_ITEM: FAILED: %s", result.error.c_str());
            }
            g_pendingCreateBytes.clear();
        }

        RenderUI();
        RenderLoadingOverlay();
        ImGui::Render();
        g_pd3dDeviceContext->OMSetRenderTargets(1, &g_mainRenderTargetView, nullptr);
        g_pd3dDeviceContext->ClearRenderTargetView(g_mainRenderTargetView, cc);
        ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());
        g_pSwapChain->Present(1, 0);
    }

    ImGui_ImplDX11_Shutdown(); ImGui_ImplWin32_Shutdown(); ImGui::DestroyContext();
    IconCache::Shutdown();
    CleanupDeviceD3D(); DestroyWindow(hwnd); UnregisterClassW(wc.lpszClassName, hInstance);
    return 0;
}
