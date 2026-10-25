#pragma once
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <string>
#include <vector>
#include <unordered_map>
#include <unordered_set>
#include "parc_engine.h"

// Shared editor state — include this in any new .cpp file
extern ParcEngine::SaveTree g_tree;
extern bool g_saveLoaded;
extern std::string g_savePath;
extern std::string g_statusMsg;
extern bool g_dirty;
extern char g_treeSearch[128];
extern char g_activeSearch[128];
extern bool g_navPending;
extern uint32_t g_navTargetOffset;
extern std::unordered_set<const void*> g_searchHits;

void LogMsg(const char* fmt, ...);
void ExtractItems();
void DoSave();
