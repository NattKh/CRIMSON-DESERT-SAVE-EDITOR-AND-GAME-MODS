/**
 * save_writer.cpp — Save file write-back: LZ4 HC compress + HMAC + ChaCha20 encrypt + write.
 */
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <bcrypt.h>
#else
#include "portable_hmac_sha256.h"
#include <random>
#endif
#include "save_writer.h"
#include <lz4.h>
#include <lz4hc.h>
#include <fstream>
#include <stdexcept>
#include <cstring>
#include <algorithm>

namespace SaveWriter {

// ── Key derivation ──

static const uint8_t SAVE_BASE_KEY[31] = {
    0xC4,0x1B,0x8E,0x73,0x0D,0xF2,0x59,0xA6,0x37,0xCC,0x04,0xE9,0xB1,0x2F,0x96,0x68,
    0xDA,0x10,0x7A,0x85,0x3E,0x61,0xF9,0x22,0x4D,0xB8,0x0A,0xD7,0x5C,0x13,0xEF
};

static const char* VERSION_PREFIX_1 = "^Qgbrm/.#@`zsr]\\@rvfal#\"";
static const char* VERSION_PREFIX_2 = "^Pearl--#Abyss__@!!";
static const char* HMAC_SUFFIX = "PRIVATE_HMAC_SECRET_CHECK";

std::vector<uint8_t> GenerateSaveKey(uint16_t version) {
    const char* prefix = (version == 1) ? VERSION_PREFIX_1 : VERSION_PREFIX_2;
    std::string material = std::string(prefix) + HMAC_SUFFIX;

    std::vector<uint8_t> key(32, 0);
    for (size_t i = 0; i < 31 && i < material.size(); ++i) {
        key[i] = SAVE_BASE_KEY[i] ^ (uint8_t)material[i];
    }
    key[31] = 0x00;
    return key;
}

// ── LZ4 HC compression ──

std::vector<uint8_t> Lz4HCCompress(const std::vector<uint8_t>& raw) {
    int max_dst = LZ4_compressBound((int)raw.size());
    std::vector<uint8_t> dst(max_dst);
    int compressed_size = LZ4_compress_HC(
        reinterpret_cast<const char*>(raw.data()),
        reinterpret_cast<char*>(dst.data()),
        (int)raw.size(),
        max_dst,
        LZ4HC_CLEVEL_MAX
    );
    if (compressed_size <= 0) {
        throw std::runtime_error("LZ4 HC compression failed");
    }
    dst.resize(compressed_size);
    return dst;
}

// ── ChaCha20 (duplicated from save_parser_cpp.cpp — it's in anonymous namespace there) ──

static uint32_t RotL32(uint32_t v, uint32_t n) {
    return ((v << n) & 0xFFFFFFFFu) | (v >> (32 - n));
}

static void QuarterRound(uint32_t state[16], int a, int b, int c, int d) {
    state[a] = (state[a] + state[b]) & 0xFFFFFFFFu; state[d] ^= state[a]; state[d] = RotL32(state[d], 16);
    state[c] = (state[c] + state[d]) & 0xFFFFFFFFu; state[b] ^= state[c]; state[b] = RotL32(state[b], 12);
    state[a] = (state[a] + state[b]) & 0xFFFFFFFFu; state[d] ^= state[a]; state[d] = RotL32(state[d], 8);
    state[c] = (state[c] + state[d]) & 0xFFFFFFFFu; state[b] ^= state[c]; state[b] = RotL32(state[b], 7);
}

static uint32_t U32LE(const uint8_t* p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static std::vector<uint8_t> ChaCha20Block(const std::vector<uint8_t>& key32, const uint8_t counter_nonce16[16]) {
    static constexpr uint32_t kConst[4] = {0x61707865u, 0x3320646Eu, 0x79622D32u, 0x6B206574u};
    uint32_t initial[16];
    initial[0] = kConst[0]; initial[1] = kConst[1]; initial[2] = kConst[2]; initial[3] = kConst[3];
    for (int i = 0; i < 8; ++i) initial[4 + i] = U32LE(key32.data() + i * 4);
    for (int i = 0; i < 4; ++i) initial[12 + i] = U32LE(counter_nonce16 + i * 4);

    uint32_t state[16];
    std::memcpy(state, initial, sizeof(state));
    for (int i = 0; i < 10; ++i) {
        QuarterRound(state, 0, 4, 8, 12); QuarterRound(state, 1, 5, 9, 13);
        QuarterRound(state, 2, 6, 10, 14); QuarterRound(state, 3, 7, 11, 15);
        QuarterRound(state, 0, 5, 10, 15); QuarterRound(state, 1, 6, 11, 12);
        QuarterRound(state, 2, 7, 8, 13); QuarterRound(state, 3, 4, 9, 14);
    }
    for (int i = 0; i < 16; ++i) state[i] = (state[i] + initial[i]) & 0xFFFFFFFFu;

    std::vector<uint8_t> out(64);
    for (int i = 0; i < 16; ++i) {
        out[i*4+0] = (uint8_t)(state[i] & 0xFF);
        out[i*4+1] = (uint8_t)((state[i] >> 8) & 0xFF);
        out[i*4+2] = (uint8_t)((state[i] >> 16) & 0xFF);
        out[i*4+3] = (uint8_t)((state[i] >> 24) & 0xFF);
    }
    return out;
}

std::vector<uint8_t> ChaCha20Crypt(const std::vector<uint8_t>& key, const std::vector<uint8_t>& nonce, const std::vector<uint8_t>& data) {
    if (nonce.size() != 16) throw std::runtime_error("Nonce must be 16 bytes");
    uint32_t words[4];
    for (int i = 0; i < 4; ++i) words[i] = U32LE(nonce.data() + i * 4);

    std::vector<uint8_t> out(data.size());
    size_t pos = 0;
    while (pos < data.size()) {
        uint8_t state_bytes[16];
        for (int i = 0; i < 4; ++i) {
            state_bytes[i*4+0] = (uint8_t)(words[i] & 0xFF);
            state_bytes[i*4+1] = (uint8_t)((words[i] >> 8) & 0xFF);
            state_bytes[i*4+2] = (uint8_t)((words[i] >> 16) & 0xFF);
            state_bytes[i*4+3] = (uint8_t)((words[i] >> 24) & 0xFF);
        }
        auto stream = ChaCha20Block(key, state_bytes);
        size_t chunk = std::min<size_t>(64, data.size() - pos);
        for (size_t i = 0; i < chunk; ++i) out[pos + i] = data[pos + i] ^ stream[i];
        pos += chunk;
        words[0] = (words[0] + 1u) & 0xFFFFFFFFu;
        if (words[0] == 0) words[1] = (words[1] + 1u) & 0xFFFFFFFFu;
    }
    return out;
}

// ── HMAC-SHA256 ──

#ifndef _WIN32
std::vector<uint8_t> ComputeHmac(const std::vector<uint8_t>& key, const std::vector<uint8_t>& data) {
    return PortableCrypto::HmacSha256(key, data);
}

std::vector<uint8_t> RandomBytes(size_t count) {
    std::vector<uint8_t> buf(count);
    std::random_device rd; // Emscripten backs this with the browser's crypto RNG
    for (size_t i = 0; i < count; i += 4) {
        uint32_t r = rd();
        for (size_t j = 0; j < 4 && i + j < count; j++)
            buf[i + j] = (uint8_t)(r >> (j * 8));
    }
    return buf;
}
#else
std::vector<uint8_t> ComputeHmac(const std::vector<uint8_t>& key, const std::vector<uint8_t>& data) {
    BCRYPT_ALG_HANDLE alg = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    DWORD obj_len = 0, cb = 0;
    std::vector<uint8_t> digest(32);

    auto s = BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, nullptr, BCRYPT_ALG_HANDLE_HMAC_FLAG);
    if (s < 0) throw std::runtime_error("BCryptOpenAlgorithmProvider failed");

    s = BCryptGetProperty(alg, BCRYPT_OBJECT_LENGTH, (PUCHAR)&obj_len, sizeof(obj_len), &cb, 0);
    if (s < 0) { BCryptCloseAlgorithmProvider(alg, 0); throw std::runtime_error("BCryptGetProperty failed"); }

    std::vector<uint8_t> hash_obj(obj_len);
    s = BCryptCreateHash(alg, &hash, hash_obj.data(), obj_len,
        const_cast<PUCHAR>(key.data()), (ULONG)key.size(), 0);
    if (s < 0) { BCryptCloseAlgorithmProvider(alg, 0); throw std::runtime_error("BCryptCreateHash failed"); }

    s = BCryptHashData(hash, const_cast<PUCHAR>(data.data()), (ULONG)data.size(), 0);
    if (s < 0) { BCryptDestroyHash(hash); BCryptCloseAlgorithmProvider(alg, 0); throw std::runtime_error("BCryptHashData failed"); }

    s = BCryptFinishHash(hash, digest.data(), (ULONG)digest.size(), 0);
    BCryptDestroyHash(hash);
    BCryptCloseAlgorithmProvider(alg, 0);
    if (s < 0) throw std::runtime_error("BCryptFinishHash failed");
    return digest;
}

// ── Random nonce ──

std::vector<uint8_t> RandomBytes(size_t count) {
    std::vector<uint8_t> buf(count);
    auto s = BCryptGenRandom(nullptr, buf.data(), (ULONG)count, BCRYPT_USE_SYSTEM_PREFERRED_RNG);
    if (s < 0) throw std::runtime_error("BCryptGenRandom failed");
    return buf;
}
#endif // _WIN32

// ── Write save file ──

static constexpr uint32_t HEADER_SIZE = 0x80;
static constexpr uint32_t VERSION_OFF = 0x04;
static constexpr uint32_t FLAGS_OFF = 0x06;
static constexpr uint32_t UNCOMP_OFF = 0x12;
static constexpr uint32_t PAYLOAD_OFF = 0x16;
static constexpr uint32_t NONCE_OFF = 0x1A;
static constexpr uint32_t HMAC_OFF = 0x2A;

void WriteSaveFile(const std::string& path,
                   const std::vector<uint8_t>& raw_blob,
                   const std::vector<uint8_t>& original_header) {
    uint16_t version = 2;
    if (original_header.size() >= 6) {
        memcpy(&version, original_header.data() + VERSION_OFF, 2);
    }

    auto key = GenerateSaveKey(version);
    auto compressed = Lz4HCCompress(raw_blob);
    auto hmac_digest = ComputeHmac(key, compressed);
    auto nonce = RandomBytes(16);
    auto encrypted = ChaCha20Crypt(key, nonce, compressed);

    std::vector<uint8_t> header(HEADER_SIZE, 0);
    if (original_header.size() >= 0x12) {
        memcpy(header.data(), original_header.data(), 0x12);
    }
    memcpy(header.data(), "SAVE", 4);
    uint16_t ver2 = 2;
    memcpy(header.data() + VERSION_OFF, &ver2, 2);
    uint16_t flags = 0x0080;
    memcpy(header.data() + FLAGS_OFF, &flags, 2);
    uint32_t uncomp_size = (uint32_t)raw_blob.size();
    memcpy(header.data() + UNCOMP_OFF, &uncomp_size, 4);
    uint32_t payload_size = (uint32_t)compressed.size();
    memcpy(header.data() + PAYLOAD_OFF, &payload_size, 4);
    memcpy(header.data() + NONCE_OFF, nonce.data(), 16);
    memcpy(header.data() + HMAC_OFF, hmac_digest.data(), 32);

    std::ofstream out(path, std::ios::binary);
    if (!out) throw std::runtime_error("Cannot open output file: " + path);
    out.write(reinterpret_cast<const char*>(header.data()), header.size());
    out.write(reinterpret_cast<const char*>(encrypted.data()), encrypted.size());
}

void WriteRawFile(const std::string& path, const std::vector<uint8_t>& raw_blob) {
    std::ofstream out(path, std::ios::binary);
    if (!out) throw std::runtime_error("Cannot open output file: " + path);
    out.write(reinterpret_cast<const char*>(raw_blob.data()), raw_blob.size());
}

} // namespace SaveWriter
