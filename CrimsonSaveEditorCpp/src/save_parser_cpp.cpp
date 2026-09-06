#include "save_parser_cpp.h"

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <bcrypt.h>
#else
#include "portable_hmac_sha256.h"
#endif

#include <algorithm>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <unordered_map>

namespace SaveParserCpp {
namespace {

constexpr uint8_t SAVE_MAGIC[] = {'S', 'A', 'V', 'E'};
constexpr uint8_t RAW_MAGIC[] = {0xFF, 0xFF, 0x04, 0x00};
constexpr uint32_t SAVE_HEADER_SIZE = 0x80;
constexpr uint32_t NONCE_OFF = 0x1A;
constexpr uint32_t NONCE_SIZE = 0x10;
constexpr uint32_t HMAC_OFF = 0x2A;
constexpr uint32_t HMAC_SIZE = 0x20;
constexpr const char* DEFAULT_KEY_HEX = "9a4beb127f9e748b148d6690c25cc9379a315bd56c28af6319fd559f1152ac00";

void ReportProgress(const ProgressCallback& progress, std::string stage, uint32_t current, uint32_t total) {
    if (progress) {
        progress(ProgressInfo{std::move(stage), current, total});
    }
}

std::string HexString(const uint8_t* data, size_t size) {
    std::ostringstream oss;
    oss << std::hex << std::setfill('0');
    for (size_t i = 0; i < size; ++i) {
        oss << std::setw(2) << static_cast<unsigned>(data[i]);
    }
    return oss.str();
}

std::string HexString(const std::vector<uint8_t>& data) {
    return HexString(data.data(), data.size());
}

std::string ReadAscii(const std::vector<uint8_t>& blob, size_t offset, size_t length) {
    if (offset + length > blob.size()) {
        throw std::runtime_error("ASCII read overruns blob");
    }
    return std::string(reinterpret_cast<const char*>(blob.data() + offset), length);
}

uint16_t U16(const std::vector<uint8_t>& blob, size_t offset) {
    if (offset + 2 > blob.size()) {
        throw std::runtime_error("u16 read overruns blob");
    }
    return static_cast<uint16_t>(blob[offset] | (blob[offset + 1] << 8));
}

uint16_t U16BE(const std::vector<uint8_t>& blob, size_t offset) {
    if (offset + 2 > blob.size()) {
        throw std::runtime_error("u16be read overruns blob");
    }
    return static_cast<uint16_t>((blob[offset] << 8) | blob[offset + 1]);
}

uint32_t U24(const std::vector<uint8_t>& blob, size_t offset) {
    if (offset + 3 > blob.size()) {
        throw std::runtime_error("u24 read overruns blob");
    }
    return static_cast<uint32_t>(blob[offset] | (blob[offset + 1] << 8) | (blob[offset + 2] << 16));
}

uint32_t U32(const std::vector<uint8_t>& blob, size_t offset) {
    if (offset + 4 > blob.size()) {
        throw std::runtime_error("u32 read overruns blob");
    }
    return static_cast<uint32_t>(blob[offset] |
        (blob[offset + 1] << 8) |
        (blob[offset + 2] << 16) |
        (blob[offset + 3] << 24));
}

uint64_t U64(const std::vector<uint8_t>& blob, size_t offset) {
    if (offset + 8 > blob.size()) {
        throw std::runtime_error("u64 read overruns blob");
    }
    uint64_t value = 0;
    for (size_t i = 0; i < 8; ++i) {
        value |= static_cast<uint64_t>(blob[offset + i]) << (i * 8);
    }
    return value;
}

bool LooksLikeRaw(const std::vector<uint8_t>& blob) {
    return blob.size() >= sizeof(RAW_MAGIC) && std::memcmp(blob.data(), RAW_MAGIC, sizeof(RAW_MAGIC)) == 0;
}

bool LooksLikeSave(const std::vector<uint8_t>& blob) {
    return blob.size() >= sizeof(SAVE_MAGIC) && std::memcmp(blob.data(), SAVE_MAGIC, sizeof(SAVE_MAGIC)) == 0;
}

std::vector<uint8_t> HexToBytes(const std::string& hex) {
    if ((hex.size() % 2) != 0) {
        throw std::runtime_error("Hex string must have even length");
    }
    std::vector<uint8_t> out;
    out.reserve(hex.size() / 2);
    for (size_t i = 0; i < hex.size(); i += 2) {
        const auto byte = static_cast<uint8_t>(std::stoul(hex.substr(i, 2), nullptr, 16));
        out.push_back(byte);
    }
    return out;
}

uint32_t RotL32(uint32_t v, uint32_t n) {
    return ((v << n) & 0xFFFFFFFFu) | (v >> (32 - n));
}

void QuarterRound(uint32_t state[16], int a, int b, int c, int d) {
    state[a] = (state[a] + state[b]) & 0xFFFFFFFFu;
    state[d] ^= state[a];
    state[d] = RotL32(state[d], 16);

    state[c] = (state[c] + state[d]) & 0xFFFFFFFFu;
    state[b] ^= state[c];
    state[b] = RotL32(state[b], 12);

    state[a] = (state[a] + state[b]) & 0xFFFFFFFFu;
    state[d] ^= state[a];
    state[d] = RotL32(state[d], 8);

    state[c] = (state[c] + state[d]) & 0xFFFFFFFFu;
    state[b] ^= state[c];
    state[b] = RotL32(state[b], 7);
}

std::vector<uint8_t> ChaCha20Block(const std::vector<uint8_t>& key32, const uint8_t counter_nonce16[16]) {
    static constexpr uint32_t kConst[4] = {0x61707865u, 0x3320646Eu, 0x79622D32u, 0x6B206574u};
    if (key32.size() != 32) {
        throw std::runtime_error("ChaCha key must be 32 bytes");
    }
    uint32_t initial[16] = {};
    initial[0] = kConst[0];
    initial[1] = kConst[1];
    initial[2] = kConst[2];
    initial[3] = kConst[3];
    for (int i = 0; i < 8; ++i) {
        initial[4 + i] = U32(key32, static_cast<size_t>(i) * 4);
    }
    std::vector<uint8_t> ctr(counter_nonce16, counter_nonce16 + 16);
    for (int i = 0; i < 4; ++i) {
        initial[12 + i] = U32(ctr, static_cast<size_t>(i) * 4);
    }

    uint32_t state[16];
    std::memcpy(state, initial, sizeof(state));
    for (int i = 0; i < 10; ++i) {
        QuarterRound(state, 0, 4, 8, 12);
        QuarterRound(state, 1, 5, 9, 13);
        QuarterRound(state, 2, 6, 10, 14);
        QuarterRound(state, 3, 7, 11, 15);

        QuarterRound(state, 0, 5, 10, 15);
        QuarterRound(state, 1, 6, 11, 12);
        QuarterRound(state, 2, 7, 8, 13);
        QuarterRound(state, 3, 4, 9, 14);
    }
    for (int i = 0; i < 16; ++i) {
        state[i] = (state[i] + initial[i]) & 0xFFFFFFFFu;
    }

    std::vector<uint8_t> out(64);
    for (int i = 0; i < 16; ++i) {
        const uint32_t v = state[i];
        out[i * 4 + 0] = static_cast<uint8_t>(v & 0xFF);
        out[i * 4 + 1] = static_cast<uint8_t>((v >> 8) & 0xFF);
        out[i * 4 + 2] = static_cast<uint8_t>((v >> 16) & 0xFF);
        out[i * 4 + 3] = static_cast<uint8_t>((v >> 24) & 0xFF);
    }
    return out;
}

std::vector<uint8_t> ChaCha20Xor(const std::vector<uint8_t>& key32, const std::vector<uint8_t>& counter_nonce16, const std::vector<uint8_t>& data) {
    if (counter_nonce16.size() != 16) {
        throw std::runtime_error("ChaCha counter/nonce must be 16 bytes");
    }
    uint32_t words[4] = {
        U32(counter_nonce16, 0),
        U32(counter_nonce16, 4),
        U32(counter_nonce16, 8),
        U32(counter_nonce16, 12),
    };

    std::vector<uint8_t> out(data.size());
    size_t pos = 0;
    while (pos < data.size()) {
        uint8_t state_bytes[16] = {};
        for (int i = 0; i < 4; ++i) {
            state_bytes[i * 4 + 0] = static_cast<uint8_t>(words[i] & 0xFF);
            state_bytes[i * 4 + 1] = static_cast<uint8_t>((words[i] >> 8) & 0xFF);
            state_bytes[i * 4 + 2] = static_cast<uint8_t>((words[i] >> 16) & 0xFF);
            state_bytes[i * 4 + 3] = static_cast<uint8_t>((words[i] >> 24) & 0xFF);
        }
        const auto stream = ChaCha20Block(key32, state_bytes);
        const size_t chunk = std::min<size_t>(64, data.size() - pos);
        for (size_t i = 0; i < chunk; ++i) {
            out[pos + i] = data[pos + i] ^ stream[i];
        }
        pos += chunk;
        words[0] = (words[0] + 1u) & 0xFFFFFFFFu;
        if (words[0] == 0) {
            words[1] = (words[1] + 1u) & 0xFFFFFFFFu;
        }
    }
    return out;
}

#ifndef _WIN32
std::vector<uint8_t> ComputeHmacSha256(const std::vector<uint8_t>& key, const std::vector<uint8_t>& data) {
    return PortableCrypto::HmacSha256(key, data);
}
#else
std::vector<uint8_t> ComputeHmacSha256(const std::vector<uint8_t>& key, const std::vector<uint8_t>& data) {
    BCRYPT_ALG_HANDLE alg = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    DWORD obj_len = 0;
    DWORD cb_result = 0;
    std::vector<uint8_t> hash_object;
    std::vector<uint8_t> digest(32);

    auto status = BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, nullptr, BCRYPT_ALG_HANDLE_HMAC_FLAG);
    if (status < 0) {
        throw std::runtime_error("BCryptOpenAlgorithmProvider failed");
    }
    status = BCryptGetProperty(alg, BCRYPT_OBJECT_LENGTH, reinterpret_cast<PUCHAR>(&obj_len), sizeof(obj_len), &cb_result, 0);
    if (status < 0) {
        BCryptCloseAlgorithmProvider(alg, 0);
        throw std::runtime_error("BCryptGetProperty failed");
    }
    hash_object.resize(obj_len);
    status = BCryptCreateHash(alg, &hash, hash_object.data(), obj_len,
        const_cast<PUCHAR>(key.data()), static_cast<ULONG>(key.size()), 0);
    if (status < 0) {
        BCryptCloseAlgorithmProvider(alg, 0);
        throw std::runtime_error("BCryptCreateHash failed");
    }
    status = BCryptHashData(hash, const_cast<PUCHAR>(data.data()), static_cast<ULONG>(data.size()), 0);
    if (status < 0) {
        BCryptDestroyHash(hash);
        BCryptCloseAlgorithmProvider(alg, 0);
        throw std::runtime_error("BCryptHashData failed");
    }
    status = BCryptFinishHash(hash, digest.data(), static_cast<ULONG>(digest.size()), 0);
    BCryptDestroyHash(hash);
    BCryptCloseAlgorithmProvider(alg, 0);
    if (status < 0) {
        throw std::runtime_error("BCryptFinishHash failed");
    }
    return digest;
}
#endif // _WIN32

std::vector<uint8_t> Lz4BlockDecompress(const std::vector<uint8_t>& src, uint32_t expected_size) {
    std::vector<uint8_t> dst;
    dst.reserve(expected_size);
    size_t ip = 0;
    while (ip < src.size()) {
        const uint8_t token = src[ip++];
        uint32_t literal_len = token >> 4;
        if (literal_len == 15) {
            while (ip < src.size()) {
                const uint8_t s = src[ip++];
                literal_len += s;
                if (s != 255) {
                    break;
                }
            }
        }
        if (ip + literal_len > src.size()) {
            throw std::runtime_error("LZ4 literal overruns input");
        }
        dst.insert(dst.end(), src.begin() + static_cast<std::ptrdiff_t>(ip), src.begin() + static_cast<std::ptrdiff_t>(ip + literal_len));
        ip += literal_len;
        if (ip >= src.size()) {
            break;
        }
        if (ip + 2 > src.size()) {
            throw std::runtime_error("LZ4 offset overruns input");
        }
        const uint16_t offset = static_cast<uint16_t>(src[ip] | (src[ip + 1] << 8));
        ip += 2;
        if (offset == 0 || offset > dst.size()) {
            throw std::runtime_error("LZ4 invalid match offset");
        }
        uint32_t match_len = token & 0x0F;
        if (match_len == 15) {
            while (ip < src.size()) {
                const uint8_t s = src[ip++];
                match_len += s;
                if (s != 255) {
                    break;
                }
            }
        }
        match_len += 4;
        const size_t match_pos = dst.size() - offset;
        for (uint32_t i = 0; i < match_len; ++i) {
            dst.push_back(dst[match_pos + i]);
        }
    }
    if (dst.size() != expected_size) {
        throw std::runtime_error("LZ4 decompressed size mismatch");
    }
    return dst;
}

bool FieldPresent(const std::vector<uint8_t>& mask_bytes, size_t field_index) {
    const size_t byte_index = field_index / 8;
    const size_t bit_index = field_index % 8;
    if (byte_index >= mask_bytes.size()) {
        return false;
    }
    return (mask_bytes[byte_index] & (1u << bit_index)) != 0;
}

std::string TypeToEditFormat(const std::string& type_name, uint16_t size) {
    std::string lower = type_name;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    if (lower == "bool") {
        return "bool";
    }
    if (lower.find("float") != std::string::npos && size == 4) {
        return "<f";
    }
    if (lower.find("float") != std::string::npos && size == 8) {
        return "<d";
    }
    if (lower.rfind("int", 0) == 0) {
        switch (size) {
        case 1: return "<b";
        case 2: return "<h";
        case 4: return "<i";
        case 8: return "<q";
        default: break;
        }
    }
    switch (size) {
    case 1: return "<B";
    case 2: return "<H";
    case 4: return "<I";
    case 8: return "<Q";
    default: return "";
    }
}

struct FixedValueResult {
    uint32_t end = 0;
    std::string value_repr;
    std::string edit_format;
    bool editable = false;
};

FixedValueResult DecodeFixedValue(const std::vector<uint8_t>& raw, uint32_t offset, const FieldDef& field) {
    if (field.meta_size == 0) {
        throw std::runtime_error("Fixed field has zero size");
    }
    const uint32_t end = offset + field.meta_size;
    if (end > raw.size()) {
        throw std::runtime_error("Fixed field overruns blob");
    }

    FixedValueResult out;
    out.end = end;
    out.edit_format = TypeToEditFormat(field.type_name, field.meta_size);
    out.editable = !out.edit_format.empty();

    std::string lower = field.type_name;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    if (lower == "bool" && field.meta_size == 1) {
        out.value_repr = raw[offset] ? "true" : "false";
        out.edit_format = "bool";
        out.editable = true;
        return out;
    }

    if (out.edit_format == "<f") {
        float value = 0.0f;
        std::memcpy(&value, raw.data() + offset, sizeof(value));
        std::ostringstream oss;
        oss << value;
        out.value_repr = oss.str();
        return out;
    }
    if (out.edit_format == "<d") {
        double value = 0.0;
        std::memcpy(&value, raw.data() + offset, sizeof(value));
        std::ostringstream oss;
        oss << value;
        out.value_repr = oss.str();
        return out;
    }

    switch (field.meta_size) {
    case 1:
        out.value_repr = std::to_string(raw[offset]);
        return out;
    case 2:
        out.value_repr = std::to_string(U16(raw, offset));
        return out;
    case 4:
        out.value_repr = std::to_string(U32(raw, offset));
        return out;
    case 8:
        out.value_repr = std::to_string(U64(raw, offset));
        return out;
    default:
        out.value_repr = HexString(raw.data() + offset, field.meta_size);
        out.edit_format.clear();
        out.editable = false;
        return out;
    }
}

struct InlineBytesResult {
    uint32_t end = 0;
    std::string value_repr;
    std::string note;
};

InlineBytesResult DecodeInlineBytes(const std::vector<uint8_t>& raw, uint32_t offset, const FieldDef& field, uint32_t tail_cursor) {
    if (offset + 4 > tail_cursor) {
        throw std::runtime_error("Inline bytes overrun region");
    }
    const uint32_t count = U32(raw, offset);
    const uint64_t total = 4ull + static_cast<uint64_t>(count) * field.meta_size;
    const uint64_t end64 = static_cast<uint64_t>(offset) + total;
    if (end64 > tail_cursor) {
        throw std::runtime_error("Inline bytes overrun region");
    }

    InlineBytesResult out;
    out.end = static_cast<uint32_t>(end64);
    out.note = "count=" + std::to_string(count);

    const uint8_t* data = raw.data() + offset + 4;
    const size_t data_size = static_cast<size_t>(count) * field.meta_size;
    std::string lower = field.type_name;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    if (field.meta_size == 1 && (lower.find("string") != std::string::npos || (!lower.empty() && lower.back() == 'a'))) {
        size_t trimmed = data_size;
        while (trimmed > 0 && data[trimmed - 1] == 0) {
            --trimmed;
        }
        out.value_repr = "\"" + std::string(reinterpret_cast<const char*>(data), trimmed) + "\"";
    } else if (field.meta_size == 1) {
        out.value_repr = HexString(data, data_size);
    } else {
        const size_t preview_len = std::min<size_t>(data_size, 32);
        out.value_repr = "bytes=" + std::to_string(data_size) + " preview=" + HexString(data, preview_len);
    }
    return out;
}

struct DynamicArrayResult {
    uint32_t end = 0;
    std::string value_repr;
    std::string note;
};

DynamicArrayResult DecodeDynamicArray(const std::vector<uint8_t>& raw, uint32_t offset, const FieldDef& field, uint32_t tail_cursor) {
    if (offset + 5 > tail_cursor) {
        throw std::runtime_error("Dynamic array overruns region");
    }

    auto make_result = [&](uint32_t end, uint32_t data_offset, uint32_t count, const std::string& note) {
        if (end > tail_cursor) {
            throw std::runtime_error("Dynamic array overruns region");
        }
        DynamicArrayResult out;
        out.end = end;
        const uint32_t data_size = count * field.meta_size;
        const uint32_t preview_len = std::min<uint32_t>(data_size, 16);
        out.value_repr = "count=" + std::to_string(count) + " bytes=" + std::to_string(data_size) +
            " preview=" + HexString(raw.data() + data_offset, preview_len);
        out.note = note;
        return out;
    };

    if (offset + 14 <= tail_cursor && std::memcmp(raw.data() + offset, "\x00\x00\x06\x01\x00", 5) == 0) {
        const uint32_t count = U32(raw, offset + 5);
        const uint64_t end64 = static_cast<uint64_t>(offset) + 9ull + static_cast<uint64_t>(count) * field.meta_size + 5ull;
        if (count < 0x10000 && end64 <= tail_cursor &&
            std::memcmp(raw.data() + static_cast<size_t>(end64) - 5, "\x01\x01\x01\x01\x01", 5) == 0) {
            return make_result(static_cast<uint32_t>(end64), offset + 9, count, "dynamic primitive array (prefix 0000060100)");
        }
    }

    if (raw[offset] == 1 && offset + 7 <= tail_cursor) {
        uint32_t marker_end = offset;
        while (marker_end < tail_cursor && raw[marker_end] == 1) {
            ++marker_end;
        }
        if (marker_end > offset && marker_end < tail_cursor && raw[marker_end] == 0 && marker_end + 5 <= tail_cursor) {
            const uint32_t count = U32(raw, marker_end + 1);
            uint64_t end64 = static_cast<uint64_t>(offset) +
                (marker_end - offset + 1ull) +
                4ull +
                static_cast<uint64_t>(count) * field.meta_size;
            if (count < 0x10000 && end64 <= tail_cursor) {
                if (end64 < tail_cursor && raw[static_cast<size_t>(end64)] == 1) {
                    ++end64;
                }
                return make_result(static_cast<uint32_t>(end64), marker_end + 5, count,
                    "dynamic primitive array (marker prefix len=" + std::to_string(marker_end - offset) + ")");
            }
        }
    }

    if (offset + 6 <= tail_cursor &&
        raw[offset] == 0 &&
        raw[offset + 1] == 0 &&
        raw[offset + 4] == 0 &&
        raw[offset + 5] == 0) {
        const uint32_t count = U16(raw, offset + 2);
        const uint32_t end = offset + 6 + count * field.meta_size;
        return make_result(end, offset + 6, count, "dynamic primitive array (compact header)");
    }

    const uint8_t prefix = raw[offset];
    const uint32_t count = U32(raw, offset + 1);
    const uint32_t end = offset + 5 + count * field.meta_size;
    std::string note = "dynamic primitive array";
    if (prefix != 0) {
        std::ostringstream oss;
        oss << note << " prefix=0x" << std::hex << std::uppercase << std::setw(2) << std::setfill('0')
            << static_cast<unsigned>(prefix);
        note = oss.str();
    }
    return make_result(end, offset + 5, count, note);
}

std::vector<std::pair<uint32_t, uint32_t>> ComputeUndecodedRanges(
    uint32_t block_start,
    uint32_t block_end,
    uint32_t header_end,
    const std::vector<GenericFieldValue>& fields) {
    std::vector<std::pair<uint32_t, uint32_t>> decoded;
    for (const auto& field : fields) {
        if (field.start_offset < field.end_offset) {
            decoded.push_back({field.start_offset, field.end_offset});
        }
    }
    std::sort(decoded.begin(), decoded.end());

    std::vector<std::pair<uint32_t, uint32_t>> spans;
    uint32_t cursor = header_end;
    for (const auto& [start, end] : decoded) {
        if (cursor < start) {
            spans.push_back({cursor, start});
        }
        cursor = std::max(cursor, end);
    }
    if (cursor < block_end) {
        spans.push_back({cursor, block_end});
    }
    return spans;
}

struct InlinePayloadResult {
    uint32_t end = 0;
    uint32_t reserved_u32 = 0;
    uint32_t size_u32 = 0;
    std::vector<GenericFieldValue> fields;
};

struct LocatorResult {
    uint32_t end = 0;
    GenericFieldValue field;
};

struct ObjectListResult {
    uint32_t end = 0;
    GenericFieldValue field;
};

InlinePayloadResult DecodeInlineObjectPayload(
    const std::vector<uint8_t>& raw,
    const TypeDef& type_def,
    const std::vector<uint8_t>& mask_bytes,
    uint32_t payload_start,
    uint32_t tail_cursor,
    const std::unordered_map<uint32_t, const TypeDef*>& type_by_index);

LocatorResult DecodeInlineObjectLocator(
    const std::vector<uint8_t>& raw,
    uint32_t cursor,
    uint32_t tail_cursor,
    const std::unordered_map<uint32_t, const TypeDef*>& type_by_index,
    uint16_t locator_kind = 4);

LocatorResult DecodeObjectListElement(
    const std::vector<uint8_t>& raw,
    uint32_t cursor,
    uint32_t tail_cursor,
    const std::unordered_map<uint32_t, const TypeDef*>& type_by_index) {
    // List elements use the same wrapper layout as kind-4 inline objects with a
    // variable mask byte count. The old MBC=1-only compact decoder is gone — it
    // misread MBC>=2 elements (equipment items) and stripped their child_fields.
    return DecodeInlineObjectLocator(raw, cursor, tail_cursor, type_by_index, 4);
}

ObjectListResult DecodeObjectList(
    const std::vector<uint8_t>& raw,
    uint32_t cursor,
    uint32_t tail_cursor,
    const std::unordered_map<uint32_t, const TypeDef*>& type_by_index) {
    if (cursor + 18 > tail_cursor) {
        throw std::runtime_error("Object list overruns region");
    }

    bool have_best = false;
    ObjectListResult best;
    std::string last_error = "Object list decode failed";

    for (uint32_t body_cursor : {cursor, cursor + 1, cursor + 2, cursor + 3}) {
        if (body_cursor + 18 > tail_cursor) {
            continue;
        }
        try {
            const uint8_t prefix_u8 = raw[body_cursor];
            uint32_t count = 0;
            uint32_t reserved1_u32 = 0;
            uint32_t reserved2_u32 = 0;
            uint32_t reserved3_u32 = 0;
            uint16_t reserved4_u16 = 0;
            uint32_t reserved4_u32 = 0;
            uint32_t header_size = 0;
            uint32_t count_offset = 0;   // relative to body_cursor
            uint8_t count_format = 0;    // 1=u32 LE, 2=u24 LE, 3=u16 BE

            uint32_t marker_end = body_cursor;
            while (marker_end < tail_cursor && raw[marker_end] == 1) {
                ++marker_end;
            }
            if (marker_end > body_cursor &&
                marker_end + 17 <= tail_cursor &&
                raw[marker_end] == 0 &&
                std::memcmp(raw.data() + marker_end + 5, "\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00", 13) == 0) {
                count = U32(raw, marker_end + 1);
                header_size = (marker_end - body_cursor + 1) + 4 + 13;
                count_offset = marker_end - body_cursor + 1;
                count_format = 1;
            } else if (prefix_u8 == 0 && raw[body_cursor + 1] == 0 && raw[body_cursor + 2] == 0 && raw[body_cursor + 3] == 0) {
                count = U32(raw, body_cursor + 4);
                reserved2_u32 = U32(raw, body_cursor + 8);
                reserved3_u32 = U32(raw, body_cursor + 12);
                reserved4_u16 = U16(raw, body_cursor + 16);
                header_size = 18;
                count_offset = 4;
                count_format = 1;
            } else if (prefix_u8 == 0) {
                count = U24(raw, body_cursor + 1);
                reserved1_u32 = U32(raw, body_cursor + 4);
                reserved2_u32 = U32(raw, body_cursor + 8);
                reserved3_u32 = U32(raw, body_cursor + 12);
                reserved4_u16 = U16(raw, body_cursor + 16);
                header_size = 18;
                count_offset = 1;
                count_format = 2;
            } else if (prefix_u8 == 1 && body_cursor + 21 <= tail_cursor &&
                raw[body_cursor + 1] == 1 && raw[body_cursor + 2] == 1 && raw[body_cursor + 3] == 0) {
                count = U32(raw, body_cursor + 4);
                reserved1_u32 = U32(raw, body_cursor + 8);
                reserved2_u32 = U32(raw, body_cursor + 12);
                reserved3_u32 = U32(raw, body_cursor + 16);
                header_size = 21;
                count_offset = 4;
                count_format = 1;
            } else if (prefix_u8 == 1) {
                count = U16BE(raw, body_cursor + 1);
                reserved1_u32 = U32(raw, body_cursor + 3);
                reserved2_u32 = U32(raw, body_cursor + 7);
                reserved3_u32 = U32(raw, body_cursor + 11);
                reserved4_u32 = U32(raw, body_cursor + 15);
                header_size = 19;
                count_offset = 1;
                count_format = 3;
            } else {
                throw std::runtime_error("Unsupported object list prefix");
            }

            uint32_t element_cursor = body_cursor + header_size;
            std::vector<GenericFieldValue> elements;
            elements.reserve(count);
            bool elements_ok = true;
            for (uint32_t i = 0; i < count; ++i) {
                try {
                    uint32_t elem_start = element_cursor;
                    auto element = DecodeObjectListElement(raw, element_cursor, tail_cursor, type_by_index);
                    element.field.field_index = i;
                    element.field.name = "[" + std::to_string(i) + "]";
                    element.field.type_name = element.field.child_type_name;
                    element.field.decode_kind = "list_element";
                    if (element.field.raw_value.empty()) {
                        element.field.raw_value.assign(raw.begin() + elem_start, raw.begin() + element.end);
                    }
                    elements.push_back(std::move(element.field));
                    element_cursor = element.end;
                } catch (const std::exception& ex) {
                    // Element failed — still record the list with elements parsed so far
                    if (count < 100000 && elements.size() > 10) {
                        fprintf(stderr, "[PARSER] List elem[%u/%u] failed at 0x%X: %s\n",
                            i, count, element_cursor, ex.what());
                        if (element_cursor + 20 <= raw.size()) {
                            fprintf(stderr, "  bytes: ");
                            for (int b = 0; b < 20; b++)
                                fprintf(stderr, "%02X ", raw[element_cursor + b]);
                            fprintf(stderr, "\n");
                        }
                    }
                    elements_ok = false;
                    break;
                }
            }

            ObjectListResult out;
            out.end = element_cursor;
            out.field.present = true;
            out.field.decode_kind = "object_list";
            out.field.start_offset = cursor;
            out.field.end_offset = element_cursor;
            out.field.value_repr = "prefix=" + std::to_string(prefix_u8) + " count=" + std::to_string(count);
            out.field.list_prefix_u8 = prefix_u8;
            out.field.list_count = count;
            out.field.list_reserved1_u32 = reserved1_u32;
            out.field.list_reserved2_u32 = reserved2_u32;
            out.field.list_reserved3_u32 = reserved3_u32;
            out.field.list_reserved4_u16 = reserved4_u16;
            out.field.list_reserved4_u32 = reserved4_u32;
            out.field.list_header_size = (count == 0) ? (element_cursor - cursor) : ((body_cursor - cursor) + header_size);
            out.field.list_count_offset = (body_cursor - cursor) + count_offset;
            out.field.list_count_format = count_format;
            out.field.list_elements = std::move(elements);
            if (body_cursor != cursor) {
                out.field.note = "header_offset=+" + std::to_string(body_cursor - cursor);
            }

            // Prefer results with more elements; break ties by largest end
            if (!have_best
                || out.field.list_elements.size() > best.field.list_elements.size()
                || (out.field.list_elements.size() == best.field.list_elements.size() && out.end > best.end)) {
                best = std::move(out);
                have_best = true;
            }
        } catch (const std::exception& exc) {
            last_error = exc.what();
        }
    }

    if (!have_best) {
        throw std::runtime_error(last_error);
    }
    return best;
}

LocatorResult DecodeInlineObjectLocator(
    const std::vector<uint8_t>& raw,
    uint32_t cursor,
    uint32_t tail_cursor,
    const std::unordered_map<uint32_t, const TypeDef*>& type_by_index,
    uint16_t locator_kind) {
    uint16_t prefix_u16 = 0;
    uint8_t prefix_u8 = 0;
    uint8_t prefix_len_out = 0;
    uint32_t body_cursor = cursor;

    if (locator_kind == 5) {
        body_cursor = UINT32_MAX;
        for (uint32_t delta : {0u, 1u, 3u}) {
            const uint32_t probe = cursor + delta;
            if (probe + 2 > tail_cursor) {
                continue;
            }
            const uint16_t child_mask_byte_count = U16(raw, probe);
            if (child_mask_byte_count > 0 && child_mask_byte_count <= 16) {
                body_cursor = probe;
                break;
            }
        }
        if (body_cursor == UINT32_MAX) {
            throw std::runtime_error("Invalid object pointer locator");
        }
        const uint32_t prefix_len = body_cursor - cursor;
        if (prefix_len >= 2) {
            prefix_u16 = U16(raw, cursor);
        } else if (prefix_len == 1) {
            prefix_u16 = raw[cursor];
        }
        if (prefix_len >= 3) {
            prefix_u8 = raw[cursor + 2];
        }
        prefix_len_out = static_cast<uint8_t>(prefix_len);
    }

    if (body_cursor + 18 > tail_cursor) {
        throw std::runtime_error("Object locator overruns region");
    }
    const uint16_t child_mask_byte_count = U16(raw, body_cursor);
    if (child_mask_byte_count == 0 || child_mask_byte_count > 16) {
        throw std::runtime_error("Invalid child mask count");
    }
    const uint32_t wrapper_end = body_cursor + 2 + child_mask_byte_count + 2 + 1 + 4 + 4 + 4;
    if (wrapper_end > tail_cursor) {
        throw std::runtime_error("Object locator overruns region");
    }

    std::vector<uint8_t> child_mask_bytes(raw.begin() + body_cursor + 2, raw.begin() + body_cursor + 2 + child_mask_byte_count);
    const uint16_t child_type_index = U16(raw, body_cursor + 2 + child_mask_byte_count);
    const uint8_t child_reserved_u8 = raw[body_cursor + 2 + child_mask_byte_count + 2];
    const uint32_t child_sentinel1_u32 = U32(raw, body_cursor + 2 + child_mask_byte_count + 3);
    const uint32_t child_sentinel2_u32 = U32(raw, body_cursor + 2 + child_mask_byte_count + 7);
    const uint32_t child_payload_offset = U32(raw, body_cursor + 2 + child_mask_byte_count + 11);

    auto it = type_by_index.find(child_type_index);
    const std::string child_type_name = (it != type_by_index.end()) ? it->second->name : ("<type " + std::to_string(child_type_index) + ">");

    uint32_t end = wrapper_end;
    LocatorResult out;
    out.field.present = true;
    out.field.decode_kind = "object_locator";
    out.field.start_offset = cursor;
    out.field.end_offset = wrapper_end;
    {
        std::ostringstream oss;
        oss << "type=" << child_type_name
            << " mask=" << HexString(child_mask_bytes)
            << " target=0x" << std::hex << std::uppercase << child_payload_offset;
        out.field.value_repr = oss.str();
    }
    out.field.child_prefix_u16 = prefix_u16;
    out.field.child_prefix_u8 = prefix_u8;
    out.field.child_prefix_len = prefix_len_out;
    out.field.child_mask_byte_count = child_mask_byte_count;
    out.field.child_mask_bytes = child_mask_bytes;
    out.field.child_type_index = child_type_index;
    out.field.child_type_name = child_type_name;
    out.field.child_reserved_u8 = child_reserved_u8;
    out.field.child_sentinel1_u32 = child_sentinel1_u32;
    out.field.child_sentinel2_u32 = child_sentinel2_u32;
    out.field.child_payload_offset = child_payload_offset;

    if (it != type_by_index.end()) {
        if (child_payload_offset == wrapper_end) {
            const auto child = DecodeInlineObjectPayload(raw, *it->second, child_mask_bytes, child_payload_offset, tail_cursor, type_by_index);
            out.field.child_reserved_u32 = child.reserved_u32;
            out.field.child_size_u32 = child.size_u32;
            out.field.child_fields = child.fields;
            out.field.end_offset = child.end;
            end = child.end;
        } else {
            // Stale PO (blob was modified without fixup). The payload always
            // physically follows the wrapper, so decode at wrapper_end anyway —
            // the serializer recomputes every PO fresh on write. Best-effort:
            // fall back to header-only on failure, as before.
            try {
                const auto child = DecodeInlineObjectPayload(raw, *it->second, child_mask_bytes, wrapper_end, tail_cursor, type_by_index);
                out.field.child_reserved_u32 = child.reserved_u32;
                out.field.child_size_u32 = child.size_u32;
                out.field.child_fields = child.fields;
                out.field.child_payload_offset = wrapper_end;
                out.field.end_offset = child.end;
                out.field.note = "po_stale";
                end = child.end;
            } catch (const std::exception&) {
            }
        }
    }

    out.end = end;
    return out;
}

InlinePayloadResult DecodeInlineObjectPayload(
    const std::vector<uint8_t>& raw,
    const TypeDef& type_def,
    const std::vector<uint8_t>& mask_bytes,
    uint32_t payload_start,
    uint32_t tail_cursor,
    const std::unordered_map<uint32_t, const TypeDef*>& type_by_index) {
    if (payload_start + 8 > tail_cursor) {
        throw std::runtime_error("Inline object payload overruns region");
    }

    InlinePayloadResult out;
    out.reserved_u32 = U32(raw, payload_start);
    uint32_t cursor = payload_start + 4;
    out.fields.reserve(type_def.fields.size());

    bool field_decode_failed = false;
    for (size_t index = 0; index < type_def.fields.size(); ++index) {
        const auto& field = type_def.fields[index];
        GenericFieldValue target;
        target.field_index = static_cast<uint32_t>(index);
        target.name = field.name;
        target.type_name = field.type_name;
        target.meta_kind = field.meta_kind;
        target.meta_size = field.meta_size;
        target.meta_aux = field.meta_aux;
        target.present = FieldPresent(mask_bytes, index);
        if (!target.present) {
            target.decode_kind = "absent";
            out.fields.push_back(std::move(target));
            continue;
        }

        if (field_decode_failed) {
            target.decode_kind = "skipped";
            out.fields.push_back(std::move(target));
            continue;
        }

        try {

        if ((field.meta_kind == 0 || field.meta_kind == 2) && field.meta_size > 0) {
            auto fixed = DecodeFixedValue(raw, cursor, field);
            target.decode_kind = "fixed_prefix";
            target.start_offset = cursor;
            target.end_offset = fixed.end;
            target.value_repr = fixed.value_repr;
            target.edit_format = fixed.edit_format;
            target.editable = fixed.editable;
            cursor = fixed.end;
            out.fields.push_back(std::move(target));
            continue;
        }

        if (field.meta_kind == 1 && field.meta_size > 0) {
            auto bytes = DecodeInlineBytes(raw, cursor, field, tail_cursor);
            target.decode_kind = "inline_bytes";
            target.start_offset = cursor;
            target.end_offset = bytes.end;
            target.value_repr = bytes.value_repr;
            target.note = bytes.note;
            cursor = bytes.end;
            out.fields.push_back(std::move(target));
            continue;
        }

        if (field.meta_kind == 3 && field.meta_size > 0) {
            auto dyn = DecodeDynamicArray(raw, cursor, field, tail_cursor);
            target.decode_kind = "dynamic_array";
            target.start_offset = cursor;
            target.end_offset = dyn.end;
            target.value_repr = dyn.value_repr;
            target.note = dyn.note;
            cursor = dyn.end;
            out.fields.push_back(std::move(target));
            continue;
        }

        if (field.meta_kind == 4 || field.meta_kind == 5) {
            auto loc = DecodeInlineObjectLocator(raw, cursor, tail_cursor, type_by_index, field.meta_kind);
            loc.field.field_index = static_cast<uint32_t>(index);
            loc.field.name = field.name;
            loc.field.type_name = field.type_name;
            loc.field.meta_kind = field.meta_kind;
            loc.field.meta_size = field.meta_size;
            loc.field.meta_aux = field.meta_aux;
            cursor = loc.end;
            out.fields.push_back(std::move(loc.field));
            continue;
        }

        if (field.meta_kind == 6 || field.meta_kind == 7) {
            auto list = DecodeObjectList(raw, cursor, tail_cursor, type_by_index);
            list.field.field_index = static_cast<uint32_t>(index);
            list.field.name = field.name;
            list.field.type_name = field.type_name;
            list.field.meta_kind = field.meta_kind;
            list.field.meta_size = field.meta_size;
            list.field.meta_aux = field.meta_aux;
            cursor = list.end;
            out.fields.push_back(std::move(list.field));
            continue;
        }

        throw std::runtime_error("Unsupported inline field kind");

        } catch (const std::exception&) {
            // Field decode failed — mark remaining fields as skipped.
            // The element's trailing_size will still be found below,
            // so the element boundary is correct and parsing can continue.
            target.decode_kind = "skipped";
            out.fields.push_back(std::move(target));
            field_decode_failed = true;
            continue;
        }
    }

    // Capture raw_value and list_header_raw for ALL child fields at this level.
    // This ensures the tree-based serializer can write every field correctly
    // regardless of nesting depth.
    for (auto& fv : out.fields) {
        if (fv.start_offset > 0 && fv.end_offset > fv.start_offset &&
            fv.end_offset <= raw.size() && fv.raw_value.empty()) {
            fv.raw_value.assign(raw.begin() + fv.start_offset, raw.begin() + fv.end_offset);
        }
        if ((fv.meta_kind == 6 || fv.meta_kind == 7) && fv.list_header_size > 0 &&
            fv.start_offset > 0 && fv.start_offset + fv.list_header_size <= raw.size() &&
            fv.list_header_raw.empty()) {
            fv.list_header_raw.assign(raw.begin() + fv.start_offset,
                                      raw.begin() + fv.start_offset + fv.list_header_size);
        }
    }

    int32_t size_field_offset = -1;
    // Search for trailing_size: a u32 whose value == (position - payload_start).
    // Normally within 64 bytes of cursor, but if fields were skipped due to decode
    // failure, the trailing_size could be much further ahead. Use a wider range.
    uint32_t search_limit = field_decode_failed ? 8192u : 64u;
    const uint32_t max_probe = std::min<uint32_t>(tail_cursor - 4, cursor + search_limit);
    for (uint32_t probe = cursor; probe <= max_probe; ++probe) {
        if (U32(raw, probe) == probe - payload_start) {
            size_field_offset = static_cast<int32_t>(probe);
            break;
        }
    }
    if (size_field_offset < 0) {
        throw std::runtime_error("Inline object missing trailing size");
    }
    out.size_u32 = U32(raw, static_cast<uint32_t>(size_field_offset));
    out.end = static_cast<uint32_t>(size_field_offset) + 4;
    return out;
}

std::pair<std::vector<GenericFieldValue>, std::vector<std::pair<uint32_t, uint32_t>>> DecodeFieldsInRegion(
    const std::vector<uint8_t>& raw,
    const TypeDef& type_def,
    const std::vector<uint8_t>& mask_bytes,
    uint32_t region_start,
    uint32_t region_end,
    const std::unordered_map<uint32_t, const TypeDef*>& type_by_index,
    const std::string& note) {
    std::vector<GenericFieldValue> fields;
    fields.reserve(type_def.fields.size());
    for (size_t i = 0; i < type_def.fields.size(); ++i) {
        const auto& f = type_def.fields[i];
        GenericFieldValue value;
        value.field_index = static_cast<uint32_t>(i);
        value.name = f.name;
        value.type_name = f.type_name;
        value.meta_kind = f.meta_kind;
        value.meta_size = f.meta_size;
        value.meta_aux = f.meta_aux;
        value.present = FieldPresent(mask_bytes, i);
        value.decode_kind = value.present ? "unknown" : "absent";
        value.note = note;
        fields.push_back(std::move(value));
    }

    uint32_t tail_cursor = region_end;
    for (int index = static_cast<int>(type_def.fields.size()) - 1; index >= 0; --index) {
        const auto& field = type_def.fields[static_cast<size_t>(index)];
        auto& target = fields[static_cast<size_t>(index)];
        if (!target.present) {
            target.decode_kind = "absent";
            continue;
        }
        if (field.meta_kind != 0 && field.meta_kind != 2) {
            break;
        }
        if (field.meta_size == 0 || tail_cursor < region_start + field.meta_size) {
            break;
        }
        const uint32_t start = tail_cursor - field.meta_size;
        auto fixed = DecodeFixedValue(raw, start, field);
        if (fixed.end != tail_cursor) {
            break;
        }
        target.decode_kind = "fixed_suffix";
        target.start_offset = start;
        target.end_offset = fixed.end;
        target.value_repr = fixed.value_repr;
        target.edit_format = fixed.edit_format;
        target.editable = fixed.editable;
        tail_cursor = start;
    }

    uint32_t head_cursor = region_start;
    for (size_t index = 0; index < type_def.fields.size(); ++index) {
        const auto& field = type_def.fields[index];
        auto& target = fields[index];
        if (!target.present || (target.start_offset < target.end_offset)) {
            continue;
        }
        if (head_cursor >= tail_cursor) {
            break;
        }

        try {
            if ((field.meta_kind == 0 || field.meta_kind == 2) && field.meta_size > 0) {
                auto fixed = DecodeFixedValue(raw, head_cursor, field);
                target.decode_kind = "fixed_prefix";
                target.start_offset = head_cursor;
                target.end_offset = fixed.end;
                target.value_repr = fixed.value_repr;
                target.edit_format = fixed.edit_format;
                target.editable = fixed.editable;
                head_cursor = fixed.end;
                continue;
            }

            if (field.meta_kind == 1 && field.meta_size > 0) {
                auto bytes = DecodeInlineBytes(raw, head_cursor, field, tail_cursor);
                target.decode_kind = "inline_bytes";
                target.start_offset = head_cursor;
                target.end_offset = bytes.end;
                target.value_repr = bytes.value_repr;
                target.note = bytes.note;
                head_cursor = bytes.end;
                continue;
            }

            if (field.meta_kind == 3 && field.meta_size > 0) {
                auto dyn = DecodeDynamicArray(raw, head_cursor, field, tail_cursor);
                target.decode_kind = "dynamic_array";
                target.start_offset = head_cursor;
                target.end_offset = dyn.end;
                target.value_repr = dyn.value_repr;
                target.note = dyn.note;
                head_cursor = dyn.end;
                continue;
            }

            if (field.meta_kind == 4 || field.meta_kind == 5) {
                auto loc = DecodeInlineObjectLocator(raw, head_cursor, tail_cursor, type_by_index, field.meta_kind);
                loc.field.field_index = static_cast<uint32_t>(index);
                loc.field.name = field.name;
                loc.field.type_name = field.type_name;
                loc.field.meta_kind = field.meta_kind;
                loc.field.meta_size = field.meta_size;
                loc.field.meta_aux = field.meta_aux;
                fields[index] = std::move(loc.field);
                head_cursor = loc.end;
                continue;
            }

            if (field.meta_kind == 6 || field.meta_kind == 7) {
                auto list = DecodeObjectList(raw, head_cursor, tail_cursor, type_by_index);
                list.field.field_index = static_cast<uint32_t>(index);
                list.field.name = field.name;
                list.field.type_name = field.type_name;
                list.field.meta_kind = field.meta_kind;
                list.field.meta_size = field.meta_size;
                list.field.meta_aux = field.meta_aux;
                fields[index] = std::move(list.field);
                head_cursor = list.end;
                continue;
            }
        } catch (const std::exception&) {
            break;
        }
        break;
    }

    // Capture raw_value bytes for every decoded field
    for (auto& fv : fields) {
        if (fv.start_offset > 0 && fv.end_offset > fv.start_offset && fv.end_offset <= raw.size()) {
            fv.raw_value.assign(raw.begin() + fv.start_offset, raw.begin() + fv.end_offset);
        }
        // For object lists, capture the list header raw bytes
        if ((fv.meta_kind == 6 || fv.meta_kind == 7) && fv.list_header_size > 0 &&
            fv.start_offset > 0 && fv.start_offset + fv.list_header_size <= raw.size()) {
            fv.list_header_raw.assign(raw.begin() + fv.start_offset,
                                      raw.begin() + fv.start_offset + fv.list_header_size);
        }
    }

    auto undecoded = ComputeUndecodedRanges(region_start, region_end, region_start, fields);
    return {fields, undecoded};
}

SchemaInfo ParseSchema(const std::vector<uint8_t>& raw, const ProgressCallback& progress) {
    SchemaInfo out;
    uint32_t pos = 0x0E;
    out.header_tag = U16(raw, pos); pos += 2;
    out.header_zero = U16(raw, pos); pos += 2;
    out.type_count = U16(raw, pos); pos += 2;
    const uint32_t root_len = U32(raw, pos); pos += 4;
    out.root_type = ReadAscii(raw, pos, root_len); pos += root_len;

    std::string current_name = out.root_type;
    out.types.reserve(out.type_count);
    for (uint32_t type_index = 0; type_index < out.type_count; ++type_index) {
        TypeDef type;
        type.index = type_index;
        type.name = current_name;
        type.start_offset = pos;
        const uint16_t field_count = U16(raw, pos);
        pos += 2;
        type.fields.reserve(field_count);

        for (uint16_t i = 0; i < field_count; ++i) {
            FieldDef field;
            field.start_offset = pos;
            const uint32_t name_len = U32(raw, pos); pos += 4;
            field.name = ReadAscii(raw, pos, name_len); pos += name_len;
            const uint32_t type_len = U32(raw, pos); pos += 4;
            field.type_name = ReadAscii(raw, pos, type_len); pos += type_len;
            field.meta_kind = U16(raw, pos);
            field.meta_size = U16(raw, pos + 2);
            field.meta_aux = U32(raw, pos + 4);
            pos += 8;
            field.end_offset = pos;
            type.fields.push_back(std::move(field));
        }

        type.end_offset = pos;
        out.types.push_back(std::move(type));
        if (((type_index + 1) % 4u) == 0 || (type_index + 1) == out.type_count) {
            ReportProgress(progress, "Parsing schema", type_index + 1, out.type_count);
        }
        if (type_index + 1 < out.type_count) {
            const uint32_t next_len = U32(raw, pos);
            pos += 4;
            current_name = ReadAscii(raw, pos, next_len);
            pos += next_len;
        }
    }

    out.schema_end = pos;
    return out;
}

TocInfo ParseToc(const std::vector<uint8_t>& raw, uint32_t schema_end, const std::vector<std::string>& type_names, const ProgressCallback& progress) {
    TocInfo out;
    if (schema_end + 12 > raw.size()) {
        out.stream_size = static_cast<uint32_t>(raw.size());
        return out;
    }

    out.prefix_zero = U32(raw, schema_end);
    out.entry_count = U32(raw, schema_end + 4);
    out.stream_size = U32(raw, schema_end + 8);
    const uint32_t toc_start = schema_end + 12;
    out.entries.reserve(out.entry_count);

    for (uint32_t index = 0; index < out.entry_count; ++index) {
        const uint32_t entry_off = toc_start + index * 20;
        if (entry_off + 20 > raw.size()) {
            break;
        }
        TocEntry entry;
        entry.index = index;
        entry.class_index = U32(raw, entry_off);
        entry.class_name = (entry.class_index < type_names.size()) ? type_names[entry.class_index] : ("<class_" + std::to_string(entry.class_index) + ">");
        entry.sentinel1 = U32(raw, entry_off + 4);
        entry.sentinel2 = U32(raw, entry_off + 8);
        entry.data_offset = U32(raw, entry_off + 12);
        entry.data_size = U32(raw, entry_off + 16);
        entry.entry_offset = entry_off;
        out.entries.push_back(std::move(entry));
        if (((index + 1) % 32u) == 0 || (index + 1) == out.entry_count) {
            ReportProgress(progress, "Parsing TOC", index + 1, out.entry_count);
        }
    }

    return out;
}

std::vector<ObjectBlock> DecodeObjectBlocks(
    const std::vector<uint8_t>& raw,
    const std::vector<TocEntry>& toc_entries,
    const std::vector<TypeDef>& types,
    const ProgressCallback& progress) {
    std::unordered_map<uint32_t, const TypeDef*> type_by_index;
    type_by_index.reserve(types.size());
    for (const auto& type : types) {
        type_by_index[type.index] = &type;
    }

    std::vector<ObjectBlock> blocks;
    blocks.reserve(toc_entries.size());

    for (size_t entry_index = 0; entry_index < toc_entries.size(); ++entry_index) {
        const auto& entry = toc_entries[entry_index];
        auto it = type_by_index.find(entry.class_index);
        if (it == type_by_index.end()) {
            if ((((entry_index + 1) % 8u) == 0 || (entry_index + 1) == toc_entries.size()) && !toc_entries.empty()) {
                ReportProgress(progress, "Decoding objects", static_cast<uint32_t>(entry_index + 1), static_cast<uint32_t>(toc_entries.size()));
            }
            continue;
        }
        const auto& type_def = *it->second;
        const uint32_t block_start = entry.data_offset;
        const uint32_t block_end = std::min<uint32_t>(static_cast<uint32_t>(raw.size()), entry.data_offset + entry.data_size);
        if (block_end <= block_start || block_start + 2 > block_end) {
            continue;
        }

        const uint16_t expected_mask_bytes = static_cast<uint16_t>(std::max<uint32_t>(1, (static_cast<uint32_t>(type_def.fields.size()) + 7) / 8));
        const uint16_t actual_mask_bytes = U16(raw, block_start);
        uint16_t mask_byte_count = expected_mask_bytes;
        std::string note;
        if (actual_mask_bytes > 0 && actual_mask_bytes <= 16) {
            mask_byte_count = actual_mask_bytes;
        }
        if (mask_byte_count != expected_mask_bytes) {
            note = "expected_mask_bytes=" + std::to_string(expected_mask_bytes);
        }

        const uint32_t header_end = block_start + 2 + mask_byte_count + 4;
        if (header_end > block_end) {
            continue;
        }

        std::vector<uint8_t> mask_bytes(raw.begin() + block_start + 2, raw.begin() + block_start + 2 + mask_byte_count);
        const uint32_t reserved_u32 = U32(raw, block_start + 2 + mask_byte_count);
        auto [fields, undecoded_ranges] = DecodeFieldsInRegion(raw, type_def, mask_bytes, header_end, block_end, type_by_index, note);

        ObjectBlock block;
        block.entry_index = entry.index;
        block.class_index = entry.class_index;
        block.class_name = entry.class_name;
        block.data_offset = entry.data_offset;
        block.data_size = entry.data_size;
        block.mask_byte_count = mask_byte_count;
        block.header_mask_bytes = std::move(mask_bytes);
        block.reserved_u32 = reserved_u32;
        block.fields = std::move(fields);
        block.undecoded_ranges = std::move(undecoded_ranges);
        blocks.push_back(std::move(block));
        if ((((entry_index + 1) % 8u) == 0 || (entry_index + 1) == toc_entries.size()) && !toc_entries.empty()) {
            ReportProgress(progress, "Decoding objects", static_cast<uint32_t>(entry_index + 1), static_cast<uint32_t>(toc_entries.size()));
        }
    }

    return blocks;
}

void AccumulateFieldStats(const GenericFieldValue& field, ParseStats& stats) {
    if (field.present) {
        ++stats.present_fields;
        if (field.decode_kind != "unknown" && field.decode_kind != "absent") {
            ++stats.decoded_present_fields;
        }
    }
    for (const auto& child : field.child_fields) {
        AccumulateFieldStats(child, stats);
    }
    for (const auto& element : field.list_elements) {
        AccumulateFieldStats(element, stats);
    }
}

void AppendIndent(std::ostringstream& oss, int indent) {
    for (int i = 0; i < indent; ++i) {
        oss << ' ';
    }
}

std::string EscapeJson(const std::string& value) {
    std::ostringstream oss;
    for (unsigned char c : value) {
        switch (c) {
        case '\\': oss << "\\\\"; break;
        case '"': oss << "\\\""; break;
        case '\b': oss << "\\b"; break;
        case '\f': oss << "\\f"; break;
        case '\n': oss << "\\n"; break;
        case '\r': oss << "\\r"; break;
        case '\t': oss << "\\t"; break;
        default:
            if (c < 0x20) {
                oss << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<unsigned>(c) << std::dec;
            } else {
                oss << static_cast<char>(c);
            }
        }
    }
    return oss.str();
}

void WriteRanges(std::ostringstream& oss, const std::vector<std::pair<uint32_t, uint32_t>>& ranges, int indent) {
    oss << "[\n";
    for (size_t i = 0; i < ranges.size(); ++i) {
        AppendIndent(oss, indent + 2);
        oss << "[" << ranges[i].first << ", " << ranges[i].second << "]";
        if (i + 1 < ranges.size()) {
            oss << ",";
        }
        oss << "\n";
    }
    AppendIndent(oss, indent);
    oss << "]";
}

void WriteField(std::ostringstream& oss, const GenericFieldValue& field, int indent) {
    AppendIndent(oss, indent);
    oss << "{\n";
    auto line = [&](const char* key, const std::string& value, bool comma = true) {
        AppendIndent(oss, indent + 2);
        oss << "\"" << key << "\": \"" << EscapeJson(value) << "\"";
        if (comma) {
            oss << ",";
        }
        oss << "\n";
    };
    auto num = [&](const char* key, uint64_t value, bool comma = true) {
        AppendIndent(oss, indent + 2);
        oss << "\"" << key << "\": " << value;
        if (comma) {
            oss << ",";
        }
        oss << "\n";
    };
    auto boolean = [&](const char* key, bool value, bool comma = true) {
        AppendIndent(oss, indent + 2);
        oss << "\"" << key << "\": " << (value ? "true" : "false");
        if (comma) {
            oss << ",";
        }
        oss << "\n";
    };

    num("field_index", field.field_index);
    line("name", field.name);
    line("type_name", field.type_name);
    num("meta_kind", field.meta_kind);
    num("meta_size", field.meta_size);
    num("meta_aux", field.meta_aux);
    boolean("present", field.present);
    line("decode_kind", field.decode_kind);
    num("start_offset", field.start_offset);
    num("end_offset", field.end_offset);
    line("value_repr", field.value_repr);
    line("edit_format", field.edit_format);
    boolean("editable", field.editable);
    line("note", field.note);
    num("child_prefix_u16", field.child_prefix_u16);
    num("child_prefix_u8", field.child_prefix_u8);
    num("child_mask_byte_count", field.child_mask_byte_count);
    line("child_mask_bytes", HexString(field.child_mask_bytes));
    num("child_type_index", static_cast<uint64_t>(static_cast<int64_t>(field.child_type_index)));
    line("child_type_name", field.child_type_name);
    num("child_reserved_u8", field.child_reserved_u8);
    num("child_sentinel1_u32", field.child_sentinel1_u32);
    num("child_sentinel2_u32", field.child_sentinel2_u32);
    num("child_payload_offset", field.child_payload_offset);
    num("child_reserved_u32", field.child_reserved_u32);
    num("child_size_u32", field.child_size_u32);
    num("list_prefix_u8", field.list_prefix_u8);
    num("list_count", field.list_count);
    num("list_reserved1_u32", field.list_reserved1_u32);
    num("list_reserved2_u32", field.list_reserved2_u32);
    num("list_reserved3_u32", field.list_reserved3_u32);
    num("list_reserved4_u16", field.list_reserved4_u16);
    num("list_reserved4_u32", field.list_reserved4_u32);
    num("list_header_size", field.list_header_size);
    num("child_field_count", static_cast<uint64_t>(field.child_fields.size()));
    num("list_element_count", static_cast<uint64_t>(field.list_elements.size()));

    AppendIndent(oss, indent + 2);
    oss << "\"child_fields\": [\n";
    for (size_t i = 0; i < field.child_fields.size(); ++i) {
        WriteField(oss, field.child_fields[i], indent + 4);
        if (i + 1 < field.child_fields.size()) oss << ",";
        oss << "\n";
    }
    AppendIndent(oss, indent + 2);
    oss << "],\n";

    AppendIndent(oss, indent + 2);
    oss << "\"list_elements\": [\n";
    for (size_t i = 0; i < field.list_elements.size(); ++i) {
        WriteField(oss, field.list_elements[i], indent + 4);
        if (i + 1 < field.list_elements.size()) oss << ",";
        oss << "\n";
    }
    AppendIndent(oss, indent + 2);
    oss << "],\n";

    AppendIndent(oss, indent + 2);
    oss << "\"child_undecoded_ranges\": ";
    WriteRanges(oss, field.child_undecoded_ranges, indent + 2);
    oss << "\n";

    AppendIndent(oss, indent);
    oss << "}";
}

void WriteObject(std::ostringstream& oss, const ObjectBlock& object, int indent) {
    AppendIndent(oss, indent);
    oss << "{\n";
    auto line = [&](const char* key, const std::string& value, bool comma = true) {
        AppendIndent(oss, indent + 2);
        oss << "\"" << key << "\": \"" << EscapeJson(value) << "\"";
        if (comma) {
            oss << ",";
        }
        oss << "\n";
    };
    auto num = [&](const char* key, uint64_t value, bool comma = true) {
        AppendIndent(oss, indent + 2);
        oss << "\"" << key << "\": " << value;
        if (comma) {
            oss << ",";
        }
        oss << "\n";
    };

    num("entry_index", object.entry_index);
    num("class_index", object.class_index);
    line("class_name", object.class_name);
    num("data_offset", object.data_offset);
    num("data_size", object.data_size);
    num("mask_byte_count", object.mask_byte_count);
    line("header_mask_bytes", HexString(object.header_mask_bytes));
    num("reserved_u32", object.reserved_u32);

    AppendIndent(oss, indent + 2);
    oss << "\"fields\": [\n";
    for (size_t i = 0; i < object.fields.size(); ++i) {
        WriteField(oss, object.fields[i], indent + 4);
        if (i + 1 < object.fields.size()) {
            oss << ",";
        }
        oss << "\n";
    }
    AppendIndent(oss, indent + 2);
    oss << "],\n";

    AppendIndent(oss, indent + 2);
    oss << "\"undecoded_ranges\": ";
    WriteRanges(oss, object.undecoded_ranges, indent + 2);
    oss << "\n";

    AppendIndent(oss, indent);
    oss << "}";
}

} // namespace

SchemaInfo ParseSchemaOnly(const std::vector<uint8_t>& schema_bytes) {
    return ParseSchema(schema_bytes, {});
}

ParseResult ParseRawFile(const std::string& path, ProgressCallback progress) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        throw std::runtime_error("Failed to open input file");
    }
    std::vector<uint8_t> raw((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    ReportProgress(progress, "Reading file", 1, 1);
    if (!LooksLikeRaw(raw)) {
        throw std::runtime_error("Input does not look like an inflated raw save blob");
    }

    ParseResult result;
    result.input_kind = "raw_blob";
    result.input_path = path;
    result.raw_size = static_cast<uint32_t>(raw.size());
    result.raw_blob = raw;
    ReportProgress(progress, "Parsing schema", 0, 1);
    result.schema = ParseSchema(raw, progress);

    std::vector<std::string> type_names;
    type_names.reserve(result.schema.types.size());
    for (const auto& type : result.schema.types) {
        type_names.push_back(type.name);
    }
    ReportProgress(progress, "Parsing TOC", 0, 1);
    result.toc = ParseToc(raw, result.schema.schema_end, type_names, progress);
    ReportProgress(progress, "Decoding objects", 0, result.toc.entry_count ? result.toc.entry_count : 1);
    result.objects = DecodeObjectBlocks(raw, result.toc.entries, result.schema.types, progress);

    for (const auto& object : result.objects) {
        result.stats.object_bytes += object.data_size;
        uint64_t undecoded = 0;
        for (const auto& [start, end] : object.undecoded_ranges) {
            undecoded += end - start;
        }
        result.stats.decoded_object_bytes += object.data_size - undecoded;
        for (const auto& field : object.fields) {
            AccumulateFieldStats(field, result.stats);
        }
    }

    return result;
}

ParseResult ParseFile(const std::string& path, const std::string& key_hex, ProgressCallback progress) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        throw std::runtime_error("Failed to open input file");
    }
    std::vector<uint8_t> blob((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    ReportProgress(progress, "Reading file", 1, 1);
    if (LooksLikeRaw(blob)) {
        return ParseRawFile(path, std::move(progress));
    }
    if (!LooksLikeSave(blob)) {
        throw std::runtime_error("Unsupported input: expected SAVE container or inflated raw save blob");
    }
    if (blob.size() < SAVE_HEADER_SIZE) {
        throw std::runtime_error("SAVE container too small");
    }

    const uint32_t payload_size = U32(blob, 0x16);
    if (blob.size() != SAVE_HEADER_SIZE + payload_size) {
        throw std::runtime_error("SAVE payload size does not match file length");
    }

    const std::vector<uint8_t> key = HexToBytes(key_hex.empty() ? DEFAULT_KEY_HEX : key_hex);
    if (key.size() != 32) {
        throw std::runtime_error("Save key must be 32 bytes");
    }

    std::vector<uint8_t> nonce(blob.begin() + NONCE_OFF, blob.begin() + NONCE_OFF + NONCE_SIZE);
    std::vector<uint8_t> hmac(blob.begin() + HMAC_OFF, blob.begin() + HMAC_OFF + HMAC_SIZE);
    std::vector<uint8_t> ciphertext(blob.begin() + SAVE_HEADER_SIZE, blob.end());
    ReportProgress(progress, "Decrypting payload", 0, 1);
    std::vector<uint8_t> plaintext = ChaCha20Xor(key, nonce, ciphertext);
    ReportProgress(progress, "Decrypting payload", 1, 1);
    std::vector<uint8_t> calc_hmac = ComputeHmacSha256(key, plaintext);
    ReportProgress(progress, "Inflating payload", 0, 1);
    std::vector<uint8_t> raw = Lz4BlockDecompress(plaintext, U32(blob, 0x12));
    ReportProgress(progress, "Inflating payload", 1, 1);

    ParseResult result;
    result.input_kind = "save_container";
    result.input_path = path;
    result.raw_size = static_cast<uint32_t>(raw.size());
    result.raw_blob = raw;
    result.container.present = true;
    result.container.version = U16(blob, 0x04);
    result.container.flags = U16(blob, 0x06);
    std::memcpy(&result.container.float_flag, blob.data() + 0x08, sizeof(float));
    result.container.field_0C = U32(blob, 0x0C);
    result.container.field_10 = U16(blob, 0x10);
    result.container.uncompressed_size = U32(blob, 0x12);
    result.container.payload_size = payload_size;
    result.container.nonce_hex = HexString(nonce);
    result.container.hmac_hex = HexString(hmac);
    result.container.hmac_ok = (calc_hmac == hmac);

    ReportProgress(progress, "Parsing schema", 0, 1);
    result.schema = ParseSchema(raw, progress);
    std::vector<std::string> type_names;
    type_names.reserve(result.schema.types.size());
    for (const auto& type : result.schema.types) {
        type_names.push_back(type.name);
    }
    ReportProgress(progress, "Parsing TOC", 0, 1);
    result.toc = ParseToc(raw, result.schema.schema_end, type_names, progress);
    ReportProgress(progress, "Decoding objects", 0, result.toc.entry_count ? result.toc.entry_count : 1);
    result.objects = DecodeObjectBlocks(raw, result.toc.entries, result.schema.types, progress);

    for (const auto& object : result.objects) {
        result.stats.object_bytes += object.data_size;
        uint64_t undecoded = 0;
        for (const auto& [start, end] : object.undecoded_ranges) {
            undecoded += end - start;
        }
        result.stats.decoded_object_bytes += object.data_size - undecoded;
        for (const auto& field : object.fields) {
            AccumulateFieldStats(field, result.stats);
        }
    }
    return result;
}

std::string ToJson(const ParseResult& result) {
    std::ostringstream oss;
    oss << "{\n";
    AppendIndent(oss, 2);
    oss << "\"input\": {\n";
    AppendIndent(oss, 4);
    oss << "\"input_kind\": \"" << EscapeJson(result.input_kind.empty() ? "raw_blob" : result.input_kind) << "\",\n";
    AppendIndent(oss, 4);
    oss << "\"input_path\": \"" << EscapeJson(result.input_path) << "\",\n";
    AppendIndent(oss, 4);
    oss << "\"raw_size\": " << result.raw_size << "\n";
    AppendIndent(oss, 2);
    oss << "},\n";

    AppendIndent(oss, 2);
    oss << "\"container\": {\n";
    AppendIndent(oss, 4);
    oss << "\"present\": " << (result.container.present ? "true" : "false") << ",\n";
    AppendIndent(oss, 4);
    oss << "\"version\": " << result.container.version << ",\n";
    AppendIndent(oss, 4);
    oss << "\"flags\": " << result.container.flags << ",\n";
    AppendIndent(oss, 4);
    oss << "\"float_flag\": " << result.container.float_flag << ",\n";
    AppendIndent(oss, 4);
    oss << "\"field_0C\": " << result.container.field_0C << ",\n";
    AppendIndent(oss, 4);
    oss << "\"field_10\": " << result.container.field_10 << ",\n";
    AppendIndent(oss, 4);
    oss << "\"uncompressed_size\": " << result.container.uncompressed_size << ",\n";
    AppendIndent(oss, 4);
    oss << "\"payload_size\": " << result.container.payload_size << ",\n";
    AppendIndent(oss, 4);
    oss << "\"nonce_hex\": \"" << EscapeJson(result.container.nonce_hex) << "\",\n";
    AppendIndent(oss, 4);
    oss << "\"hmac_hex\": \"" << EscapeJson(result.container.hmac_hex) << "\",\n";
    AppendIndent(oss, 4);
    oss << "\"hmac_ok\": " << (result.container.hmac_ok ? "true" : "false") << "\n";
    AppendIndent(oss, 2);
    oss << "},\n";

    AppendIndent(oss, 2);
    oss << "\"raw\": {\n";
    AppendIndent(oss, 4);
    oss << "\"size\": " << result.raw_size << ",\n";
    AppendIndent(oss, 4);
    oss << "\"schema_end\": " << result.schema.schema_end << ",\n";
    AppendIndent(oss, 4);
    oss << "\"value_section_offset\": " << result.schema.schema_end << ",\n";
    AppendIndent(oss, 4);
    oss << "\"value_section_size\": " << (result.raw_size - result.schema.schema_end) << "\n";
    AppendIndent(oss, 2);
    oss << "},\n";

    AppendIndent(oss, 2);
    oss << "\"schema\": {\n";
    AppendIndent(oss, 4);
    oss << "\"header_tag\": " << result.schema.header_tag << ",\n";
    AppendIndent(oss, 4);
    oss << "\"header_zero\": " << result.schema.header_zero << ",\n";
    AppendIndent(oss, 4);
    oss << "\"type_count\": " << result.schema.type_count << ",\n";
    AppendIndent(oss, 4);
    oss << "\"root_type\": \"" << EscapeJson(result.schema.root_type) << "\",\n";
    AppendIndent(oss, 4);
    oss << "\"types\": [\n";
    for (size_t i = 0; i < result.schema.types.size(); ++i) {
        const auto& type = result.schema.types[i];
        AppendIndent(oss, 6);
        oss << "{\n";
        AppendIndent(oss, 8);
        oss << "\"index\": " << type.index << ",\n";
        AppendIndent(oss, 8);
        oss << "\"name\": \"" << EscapeJson(type.name) << "\",\n";
        AppendIndent(oss, 8);
        oss << "\"start_offset\": " << type.start_offset << ",\n";
        AppendIndent(oss, 8);
        oss << "\"end_offset\": " << type.end_offset << ",\n";
        AppendIndent(oss, 8);
        oss << "\"fields\": [\n";
        for (size_t j = 0; j < type.fields.size(); ++j) {
            const auto& field = type.fields[j];
            AppendIndent(oss, 10);
            oss << "{"
                << "\"name\": \"" << EscapeJson(field.name) << "\", "
                << "\"type_name\": \"" << EscapeJson(field.type_name) << "\", "
                << "\"meta_kind\": " << field.meta_kind << ", "
                << "\"meta_size\": " << field.meta_size << ", "
                << "\"meta_aux\": " << field.meta_aux << ", "
                << "\"start_offset\": " << field.start_offset << ", "
                << "\"end_offset\": " << field.end_offset
                << "}";
            if (j + 1 < type.fields.size()) {
                oss << ",";
            }
            oss << "\n";
        }
        AppendIndent(oss, 8);
        oss << "]\n";
        AppendIndent(oss, 6);
        oss << "}";
        if (i + 1 < result.schema.types.size()) {
            oss << ",";
        }
        oss << "\n";
    }
    AppendIndent(oss, 4);
    oss << "]\n";
    AppendIndent(oss, 2);
    oss << "},\n";

    AppendIndent(oss, 2);
    oss << "\"toc\": {\n";
    AppendIndent(oss, 4);
    oss << "\"prefix_zero\": " << result.toc.prefix_zero << ",\n";
    AppendIndent(oss, 4);
    oss << "\"entry_count\": " << result.toc.entry_count << ",\n";
    AppendIndent(oss, 4);
    oss << "\"stream_size\": " << result.toc.stream_size << ",\n";
    AppendIndent(oss, 4);
    oss << "\"entries\": [\n";
    for (size_t i = 0; i < result.toc.entries.size(); ++i) {
        const auto& entry = result.toc.entries[i];
        AppendIndent(oss, 6);
        oss << "{"
            << "\"index\": " << entry.index << ", "
            << "\"class_index\": " << entry.class_index << ", "
            << "\"class_name\": \"" << EscapeJson(entry.class_name) << "\", "
            << "\"sentinel1\": " << entry.sentinel1 << ", "
            << "\"sentinel2\": " << entry.sentinel2 << ", "
            << "\"data_offset\": " << entry.data_offset << ", "
            << "\"data_size\": " << entry.data_size << ", "
            << "\"entry_offset\": " << entry.entry_offset
            << "}";
        if (i + 1 < result.toc.entries.size()) {
            oss << ",";
        }
        oss << "\n";
    }
    AppendIndent(oss, 4);
    oss << "]\n";
    AppendIndent(oss, 2);
    oss << "},\n";

    AppendIndent(oss, 2);
    oss << "\"stats\": {\n";
    AppendIndent(oss, 4);
    oss << "\"object_bytes\": " << result.stats.object_bytes << ",\n";
    AppendIndent(oss, 4);
    oss << "\"decoded_object_bytes\": " << result.stats.decoded_object_bytes << ",\n";
    AppendIndent(oss, 4);
    oss << "\"present_fields\": " << result.stats.present_fields << ",\n";
    AppendIndent(oss, 4);
    oss << "\"decoded_present_fields\": " << result.stats.decoded_present_fields << "\n";
    AppendIndent(oss, 2);
    oss << "},\n";

    AppendIndent(oss, 2);
    oss << "\"objects\": [\n";
    for (size_t i = 0; i < result.objects.size(); ++i) {
        WriteObject(oss, result.objects[i], 4);
        if (i + 1 < result.objects.size()) {
            oss << ",";
        }
        oss << "\n";
    }
    AppendIndent(oss, 2);
    oss << "]\n";
    oss << "}\n";
    return oss.str();
}

} // namespace SaveParserCpp
