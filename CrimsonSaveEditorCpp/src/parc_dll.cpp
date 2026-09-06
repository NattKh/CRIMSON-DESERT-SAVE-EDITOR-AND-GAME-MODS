/**
 * PARC Parser DLL — C exports for Python ctypes
 */
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include "save_parser_cpp.h"
#include "parc_engine.h"
#include "parc_serializer.h"
#include "save_writer.h"
#include <cstring>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <string>
#include <vector>

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

namespace {

struct TempFile {
    std::string path;

    TempFile() {
        char temp_dir[MAX_PATH] = {};
        char temp_file[MAX_PATH] = {};
        if (!GetTempPathA(MAX_PATH, temp_dir) ||
            !GetTempFileNameA(temp_dir, "cse", 0, temp_file)) {
            throw std::runtime_error("Unable to allocate a temporary validation file");
        }
        path = temp_file;
    }

    ~TempFile() {
        if (!path.empty()) DeleteFileA(path.c_str());
    }
};

std::string JsonEscape(const std::string& value) {
    std::ostringstream out;
    for (unsigned char ch : value) {
        switch (ch) {
        case '\\': out << "\\\\"; break;
        case '"': out << "\\\""; break;
        case '\b': out << "\\b"; break;
        case '\f': out << "\\f"; break;
        case '\n': out << "\\n"; break;
        case '\r': out << "\\r"; break;
        case '\t': out << "\\t"; break;
        default:
            if (ch < 0x20) {
                out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                    << static_cast<unsigned>(ch) << std::dec;
            } else {
                out << static_cast<char>(ch);
            }
        }
    }
    return out.str();
}

int ReturnJson(const std::string& json, char** out_json, uint32_t* out_size, int code = 0) {
    if (!out_json || !out_size) return -2;
    *out_size = static_cast<uint32_t>(json.size());
    *out_json = static_cast<char*>(std::malloc(json.size() + 1));
    if (!*out_json) {
        *out_size = 0;
        return -3;
    }
    std::memcpy(*out_json, json.c_str(), json.size() + 1);
    return code;
}

int ReturnError(const std::exception& error, char** out_json, uint32_t* out_size) {
    return ReturnJson(
        std::string("{\"ok\":false,\"error\":\"") +
        JsonEscape(error.what()) + "\"}",
        out_json, out_size, -1
    );
}

void WriteBytes(const std::string& path, const uint8_t* data, uint32_t size) {
    if (!data || size == 0) throw std::runtime_error("Edited save blob is empty");
    std::ofstream out(path, std::ios::binary);
    if (!out) throw std::runtime_error("Unable to create temporary validation file");
    out.write(reinterpret_cast<const char*>(data), size);
    if (!out) throw std::runtime_error("Unable to write temporary validation file");
}

SaveParserCpp::ParseResult ParseRawBytes(const uint8_t* data, uint32_t size) {
    TempFile temp;
    WriteBytes(temp.path, data, size);
    return SaveParserCpp::ParseRawFile(temp.path);
}

uint64_t Fnv1aAppend(uint64_t value, const void* data, size_t size) {
    auto bytes = static_cast<const uint8_t*>(data);
    for (size_t i = 0; i < size; ++i) {
        value ^= bytes[i];
        value *= 1099511628211ULL;
    }
    return value;
}

uint64_t Fnv1aAppend(uint64_t value, const std::string& text) {
    value = Fnv1aAppend(value, text.data(), text.size());
    const uint8_t separator = 0;
    return Fnv1aAppend(value, &separator, 1);
}

std::string SchemaFingerprint(const SaveParserCpp::SchemaInfo& schema) {
    uint64_t hash = 14695981039346656037ULL;
    hash = Fnv1aAppend(hash, schema.root_type);
    for (const auto& type : schema.types) {
        hash = Fnv1aAppend(hash, type.name);
        for (const auto& field : type.fields) {
            hash = Fnv1aAppend(hash, field.name);
            hash = Fnv1aAppend(hash, field.type_name);
            hash = Fnv1aAppend(hash, &field.meta_kind, sizeof(field.meta_kind));
            hash = Fnv1aAppend(hash, &field.meta_size, sizeof(field.meta_size));
            hash = Fnv1aAppend(hash, &field.meta_aux, sizeof(field.meta_aux));
        }
    }
    std::ostringstream out;
    out << std::hex << std::setw(16) << std::setfill('0') << hash;
    return out.str();
}

struct ValidationResult {
    bool ok = true;
    bool roundtrip_stable = false;
    std::vector<std::string> errors;
};

ValidationResult ValidateParsed(const SaveParserCpp::ParseResult& parsed, bool require_hmac) {
    ValidationResult result;
    const auto raw_size = static_cast<uint64_t>(parsed.raw_blob.size());

    if (require_hmac && (!parsed.container.present || !parsed.container.hmac_ok)) {
        result.errors.emplace_back("Save container HMAC verification failed");
    }
    if (parsed.schema.types.empty()) result.errors.emplace_back("Save schema contains no types");
    if (parsed.schema.type_count != parsed.schema.types.size()) {
        result.errors.emplace_back("Schema type count does not match parsed type table");
    }
    if (parsed.toc.entry_count != parsed.toc.entries.size()) {
        result.errors.emplace_back("TOC entry count does not match parsed TOC table");
    }
    if (parsed.toc.stream_size != parsed.raw_blob.size()) {
        result.errors.emplace_back("PARC stream size does not match the decompressed blob size");
    }
    if (parsed.objects.size() != parsed.toc.entries.size()) {
        result.errors.emplace_back("Decoded object count does not match the TOC entry count");
    }

    for (size_t index = 0; index < parsed.toc.entries.size(); ++index) {
        const auto& entry = parsed.toc.entries[index];
        if (entry.class_index >= parsed.schema.types.size()) {
            result.errors.emplace_back("TOC entry " + std::to_string(index) + " has an invalid class index");
        }
        const uint64_t end = static_cast<uint64_t>(entry.data_offset) + entry.data_size;
        if (end > raw_size) {
            result.errors.emplace_back("TOC entry " + std::to_string(index) + " extends beyond the blob");
        }
    }

    if (result.errors.empty()) {
        const auto serialized = ParcSerializer::Serialize(parsed, parsed.raw_blob);
        result.roundtrip_stable = (serialized == parsed.raw_blob);
        if (!result.roundtrip_stable) {
            result.errors.emplace_back("Parse/serialize roundtrip is not byte-stable");
        }
    }

    result.ok = result.errors.empty();
    return result;
}

std::string ValidationJson(
    const SaveParserCpp::ParseResult& parsed,
    const ValidationResult& validation,
    const std::string& operation
) {
    std::ostringstream out;
    out << "{\"ok\":" << (validation.ok ? "true" : "false")
        << ",\"operation\":\"" << JsonEscape(operation) << "\""
        << ",\"backend_version\":\"2.1.0\""
        << ",\"input_kind\":\"" << JsonEscape(parsed.input_kind) << "\""
        << ",\"hmac_ok\":" << (parsed.container.present ? (parsed.container.hmac_ok ? "true" : "false") : "null")
        << ",\"raw_size\":" << parsed.raw_blob.size()
        << ",\"schema_type_count\":" << parsed.schema.types.size()
        << ",\"schema_fingerprint\":\"" << SchemaFingerprint(parsed.schema) << "\""
        << ",\"toc_entry_count\":" << parsed.toc.entries.size()
        << ",\"object_count\":" << parsed.objects.size()
        << ",\"roundtrip_stable\":" << (validation.roundtrip_stable ? "true" : "false")
        << ",\"errors\":[";
    for (size_t i = 0; i < validation.errors.size(); ++i) {
        if (i) out << ',';
        out << '"' << JsonEscape(validation.errors[i]) << '"';
    }
    out << "]}";
    return out.str();
}

} // namespace

extern "C" {

EXPORT int parc_parse_file(
    const char* file_path,
    const char* key_hex,
    char** out_json,
    uint32_t* out_size
) {
    try {
        auto result = SaveParserCpp::ParseFile(
            file_path,
            key_hex ? key_hex : ""
        );
        std::string json = SaveParserCpp::ToJson(result);
        *out_size = static_cast<uint32_t>(json.size());
        *out_json = static_cast<char*>(std::malloc(json.size() + 1));
        if (!*out_json) return -1;
        std::memcpy(*out_json, json.c_str(), json.size() + 1);
        return 0;
    } catch (const std::exception& e) {
        std::string err = std::string("{\"error\":\"") + e.what() + "\"}";
        *out_size = static_cast<uint32_t>(err.size());
        *out_json = static_cast<char*>(std::malloc(err.size() + 1));
        if (*out_json) std::memcpy(*out_json, err.c_str(), err.size() + 1);
        return -1;
    }
}

EXPORT int parc_parse_raw_file(
    const char* file_path,
    char** out_json,
    uint32_t* out_size
) {
    try {
        auto result = SaveParserCpp::ParseRawFile(file_path);
        std::string json = SaveParserCpp::ToJson(result);
        *out_size = static_cast<uint32_t>(json.size());
        *out_json = static_cast<char*>(std::malloc(json.size() + 1));
        if (!*out_json) return -1;
        std::memcpy(*out_json, json.c_str(), json.size() + 1);
        return 0;
    } catch (const std::exception& e) {
        std::string err = std::string("{\"error\":\"") + e.what() + "\"}";
        *out_size = static_cast<uint32_t>(err.size());
        *out_json = static_cast<char*>(std::malloc(err.size() + 1));
        if (*out_json) std::memcpy(*out_json, err.c_str(), err.size() + 1);
        return -1;
    }
}

EXPORT int parc_parse_blob(
    const uint8_t* blob_data,
    uint32_t blob_size,
    char** out_json,
    uint32_t* out_size
) {
    try {
        // Write blob to temp file and parse it (reuse existing file-based parser)
        char temp_path[MAX_PATH];
        GetTempPathA(MAX_PATH, temp_path);
        std::string temp_file = std::string(temp_path) + "parc_dll_temp.bin";

        {
            std::ofstream ofs(temp_file, std::ios::binary);
            ofs.write(reinterpret_cast<const char*>(blob_data), blob_size);
        }

        auto result = SaveParserCpp::ParseRawFile(temp_file);
        DeleteFileA(temp_file.c_str());

        std::string json = SaveParserCpp::ToJson(result);
        *out_size = static_cast<uint32_t>(json.size());
        *out_json = static_cast<char*>(std::malloc(json.size() + 1));
        if (!*out_json) return -1;
        std::memcpy(*out_json, json.c_str(), json.size() + 1);
        return 0;
    } catch (const std::exception& e) {
        std::string err = std::string("{\"error\":\"") + e.what() + "\"}";
        *out_size = static_cast<uint32_t>(err.size());
        *out_json = static_cast<char*>(std::malloc(err.size() + 1));
        if (*out_json) std::memcpy(*out_json, err.c_str(), err.size() + 1);
        return -1;
    }
}

EXPORT int parc_validate_file(
    const char* file_path,
    char** out_json,
    uint32_t* out_size
) {
    try {
        if (!file_path || !*file_path) throw std::runtime_error("Save path is empty");
        auto parsed = SaveParserCpp::ParseFile(file_path);
        auto validation = ValidateParsed(parsed, parsed.container.present);
        return ReturnJson(
            ValidationJson(parsed, validation, "validate_file"),
            out_json, out_size, validation.ok ? 0 : 1
        );
    } catch (const std::exception& error) {
        return ReturnError(error, out_json, out_size);
    }
}

EXPORT int parc_validate_blob(
    const uint8_t* blob_data,
    uint32_t blob_size,
    char** out_json,
    uint32_t* out_size
) {
    try {
        auto parsed = ParseRawBytes(blob_data, blob_size);
        auto validation = ValidateParsed(parsed, false);
        return ReturnJson(
            ValidationJson(parsed, validation, "validate_blob"),
            out_json, out_size, validation.ok ? 0 : 1
        );
    } catch (const std::exception& error) {
        return ReturnError(error, out_json, out_size);
    }
}

EXPORT int parc_write_validated_save(
    const char* source_save_path,
    const uint8_t* blob_data,
    uint32_t blob_size,
    const char* output_save_path,
    char** out_json,
    uint32_t* out_size
) {
    try {
        if (!source_save_path || !*source_save_path) {
            throw std::runtime_error("Source save path is empty");
        }
        if (!output_save_path || !*output_save_path) {
            throw std::runtime_error("Output save path is empty");
        }

        auto source = ParcEngine::LoadSave(source_save_path);
        if (!source.is_encrypted || !source.parsed.container.hmac_ok) {
            throw std::runtime_error("Source save is not a valid HMAC-protected SAVE container");
        }
        auto source_validation = ValidateParsed(source.parsed, true);
        if (!source_validation.ok) {
            throw std::runtime_error("Source save failed structural validation");
        }

        auto edited = ParseRawBytes(blob_data, blob_size);
        auto edited_validation = ValidateParsed(edited, false);
        if (!edited_validation.ok) {
            throw std::runtime_error(
                edited_validation.errors.empty()
                    ? "Edited save failed structural validation"
                    : "Edited save failed structural validation: " + edited_validation.errors.front()
            );
        }
        if (SchemaFingerprint(source.parsed.schema) != SchemaFingerprint(edited.schema)) {
            throw std::runtime_error("Edited save schema differs from the loaded source schema");
        }

        std::vector<uint8_t> edited_blob(blob_data, blob_data + blob_size);
        SaveWriter::WriteSaveFile(output_save_path, edited_blob, source.original_header);

        auto written = SaveParserCpp::ParseFile(output_save_path);
        auto written_validation = ValidateParsed(written, true);
        if (!written_validation.ok) {
            DeleteFileA(output_save_path);
            throw std::runtime_error("Written save failed post-write validation");
        }
        if (written.raw_blob != edited_blob) {
            DeleteFileA(output_save_path);
            throw std::runtime_error("Written save payload does not match the validated edited data");
        }

        return ReturnJson(
            ValidationJson(written, written_validation, "write_validated_save"),
            out_json, out_size, 0
        );
    } catch (const std::exception& error) {
        return ReturnError(error, out_json, out_size);
    }
}

EXPORT void parc_free(void* ptr) {
    std::free(ptr);
}

EXPORT const char* parc_version() {
    return "crimson_save_backend 2.1.0";
}

} // extern "C"
