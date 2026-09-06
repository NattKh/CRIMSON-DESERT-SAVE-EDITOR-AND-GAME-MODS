#pragma once
#include <d3d11.h>
#include <string>
#include <unordered_map>
#include <cstdint>

namespace IconCache {

void Init(ID3D11Device* device, const std::string& icons_dir);
void Shutdown();

// Get texture for an item key. Returns nullptr if not found/not loaded yet.
// Loads lazily on first request (async-friendly but currently synchronous).
ID3D11ShaderResourceView* Get(int itemKey);

// Preload a batch of keys (call during idle to warm cache)
void Preload(const int* keys, int count);

// Stats
int LoadedCount();
int CacheHits();

} // namespace IconCache
