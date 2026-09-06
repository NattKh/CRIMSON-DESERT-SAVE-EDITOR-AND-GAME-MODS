#include "icon_cache.h"
#include <wincodec.h>
#include <unordered_map>
#include <string>
#include <filesystem>

#pragma comment(lib, "windowscodecs.lib")

namespace IconCache {

static ID3D11Device* g_device = nullptr;
static std::string g_iconsDir;
static std::unordered_map<int, ID3D11ShaderResourceView*> g_cache;
static IWICImagingFactory* g_wicFactory = nullptr;
static int g_hits = 0;

void Init(ID3D11Device* device, const std::string& icons_dir) {
    g_device = device;
    g_iconsDir = icons_dir;
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER,
                     IID_PPV_ARGS(&g_wicFactory));
}

void Shutdown() {
    for (auto& [k, srv] : g_cache) {
        if (srv) srv->Release();
    }
    g_cache.clear();
    if (g_wicFactory) { g_wicFactory->Release(); g_wicFactory = nullptr; }
}

static ID3D11ShaderResourceView* LoadIcon(const std::string& path) {
    if (!g_wicFactory || !g_device) return nullptr;

    std::wstring wpath(path.begin(), path.end());
    IWICBitmapDecoder* decoder = nullptr;
    HRESULT hr = g_wicFactory->CreateDecoderFromFilename(wpath.c_str(), nullptr,
        GENERIC_READ, WICDecodeMetadataCacheOnDemand, &decoder);
    if (FAILED(hr) || !decoder) return nullptr;

    IWICBitmapFrameDecode* frame = nullptr;
    decoder->GetFrame(0, &frame);
    if (!frame) { decoder->Release(); return nullptr; }

    IWICFormatConverter* converter = nullptr;
    g_wicFactory->CreateFormatConverter(&converter);
    if (!converter) { frame->Release(); decoder->Release(); return nullptr; }

    converter->Initialize(frame, GUID_WICPixelFormat32bppRGBA,
        WICBitmapDitherTypeNone, nullptr, 0.0, WICBitmapPaletteTypeCustom);

    UINT w = 0, h = 0;
    converter->GetSize(&w, &h);
    if (w == 0 || h == 0) { converter->Release(); frame->Release(); decoder->Release(); return nullptr; }

    std::vector<uint8_t> pixels(w * h * 4);
    converter->CopyPixels(nullptr, w * 4, (UINT)pixels.size(), pixels.data());

    // Create DX11 texture
    D3D11_TEXTURE2D_DESC desc = {};
    desc.Width = w;
    desc.Height = h;
    desc.MipLevels = 1;
    desc.ArraySize = 1;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

    D3D11_SUBRESOURCE_DATA initData = {};
    initData.pSysMem = pixels.data();
    initData.SysMemPitch = w * 4;

    ID3D11Texture2D* tex = nullptr;
    hr = g_device->CreateTexture2D(&desc, &initData, &tex);
    if (FAILED(hr) || !tex) { converter->Release(); frame->Release(); decoder->Release(); return nullptr; }

    D3D11_SHADER_RESOURCE_VIEW_DESC srvDesc = {};
    srvDesc.Format = desc.Format;
    srvDesc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    srvDesc.Texture2D.MipLevels = 1;

    ID3D11ShaderResourceView* srv = nullptr;
    g_device->CreateShaderResourceView(tex, &srvDesc, &srv);
    tex->Release();

    converter->Release();
    frame->Release();
    decoder->Release();
    return srv;
}

ID3D11ShaderResourceView* Get(int itemKey) {
    auto it = g_cache.find(itemKey);
    if (it != g_cache.end()) {
        g_hits++;
        return it->second; // may be nullptr (file not found — cached negative)
    }

    // Try to load
    std::string path = g_iconsDir + "\\" + std::to_string(itemKey) + ".webp";
    ID3D11ShaderResourceView* srv = nullptr;
    if (std::filesystem::exists(path)) {
        srv = LoadIcon(path);
    }
    g_cache[itemKey] = srv; // cache even if null (avoids re-trying missing icons)
    return srv;
}

void Preload(const int* keys, int count) {
    for (int i = 0; i < count; i++) {
        Get(keys[i]);
    }
}

int LoadedCount() {
    int n = 0;
    for (auto& [k, v] : g_cache) if (v) n++;
    return n;
}

int CacheHits() { return g_hits; }

} // namespace IconCache
