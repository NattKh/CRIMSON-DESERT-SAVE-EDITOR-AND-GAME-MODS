#pragma once

#include <cstdint>
#include <functional>
#include <string>
#include <utility>
#include <vector>

namespace SaveParserCpp {

struct FieldDef {
    std::string name;
    std::string type_name;
    uint16_t meta_kind = 0;
    uint16_t meta_size = 0;
    uint32_t meta_aux = 0;
    uint32_t start_offset = 0;
    uint32_t end_offset = 0;
};

struct TypeDef {
    uint32_t index = 0;
    std::string name;
    std::vector<FieldDef> fields;
    uint32_t start_offset = 0;
    uint32_t end_offset = 0;
};

struct TocEntry {
    uint32_t index = 0;
    uint32_t class_index = 0;
    std::string class_name;
    uint32_t sentinel1 = 0;
    uint32_t sentinel2 = 0;
    uint32_t data_offset = 0;
    uint32_t data_size = 0;
    uint32_t entry_offset = 0;
};

struct GenericFieldValue {
    uint32_t field_index = 0;
    std::string name;
    std::string type_name;
    uint16_t meta_kind = 0;
    uint16_t meta_size = 0;
    uint32_t meta_aux = 0;
    bool present = false;
    std::string decode_kind = "unknown";
    uint32_t start_offset = 0;
    uint32_t end_offset = 0;
    std::string value_repr;
    std::string edit_format;
    bool editable = false;
    std::string note;
    uint16_t child_prefix_u16 = 0;
    uint8_t child_prefix_u8 = 0;
    uint8_t child_prefix_len = 0;   // kind-5 prefix byte count (0-3), set by parser
    uint16_t child_mask_byte_count = 0;
    std::vector<uint8_t> child_mask_bytes;
    int32_t child_type_index = -1;
    std::string child_type_name;
    uint8_t child_reserved_u8 = 0;
    uint32_t child_sentinel1_u32 = 0;
    uint32_t child_sentinel2_u32 = 0;
    uint32_t child_payload_offset = 0;
    uint32_t child_reserved_u32 = 0;
    uint32_t child_size_u32 = 0;
    std::vector<GenericFieldValue> child_fields;
    std::vector<std::pair<uint32_t, uint32_t>> child_undecoded_ranges;
    uint8_t list_prefix_u8 = 0;
    uint32_t list_count = 0;
    uint32_t list_reserved1_u32 = 0;
    uint32_t list_reserved2_u32 = 0;
    uint32_t list_reserved3_u32 = 0;
    uint16_t list_reserved4_u16 = 0;
    uint32_t list_reserved4_u32 = 0;
    uint32_t list_header_size = 0;
    // Exact location of the element count within the list header, recorded by
    // the parser so writers never have to guess the header layout.
    uint32_t list_count_offset = 0;  // relative to start_offset; valid when list_count_format != 0
    uint8_t list_count_format = 0;   // 0=unknown, 1=u32 LE, 2=u24 LE, 3=u16 BE
    std::vector<GenericFieldValue> list_elements;
    std::vector<uint8_t> raw_value;
    std::vector<uint8_t> list_header_raw;

    // Self-contained serialization data (used by imported/synthetic trees that
    // have no source blob to copy gaps from). When non-empty the serializer
    // writes these instead of gap-copying via offsets. The *_src offsets are
    // the bytes' absolute position in their source blob, so POs hidden inside
    // undecoded data can still be re-anchored after import.
    std::vector<uint8_t> gap_before;      // undecoded bytes preceding this field
    uint32_t gap_before_src = 0;
    std::vector<uint8_t> trailing_bytes;  // undecoded bytes between last child and trailing_size
    uint32_t trailing_src = 0;
};

struct ObjectBlock {
    uint32_t entry_index = 0;
    uint32_t class_index = 0;
    std::string class_name;
    uint32_t data_offset = 0;
    uint32_t data_size = 0;
    uint16_t mask_byte_count = 0;
    std::vector<uint8_t> header_mask_bytes;
    uint32_t reserved_u32 = 0;
    std::vector<GenericFieldValue> fields;
    std::vector<std::pair<uint32_t, uint32_t>> undecoded_ranges;
    std::vector<uint8_t> trailing_bytes;  // imported trees: data after last field
    uint32_t trailing_src = 0;            // source offset of trailing_bytes
};

struct SchemaInfo {
    uint16_t header_tag = 0;
    uint16_t header_zero = 0;
    uint16_t type_count = 0;
    std::string root_type;
    std::vector<TypeDef> types;
    uint32_t schema_end = 0;
};

struct TocInfo {
    uint32_t prefix_zero = 0;
    uint32_t entry_count = 0;
    uint32_t stream_size = 0;
    std::vector<TocEntry> entries;
};

struct ParseStats {
    uint64_t object_bytes = 0;
    uint64_t decoded_object_bytes = 0;
    uint64_t present_fields = 0;
    uint64_t decoded_present_fields = 0;
};

struct ContainerInfo {
    bool present = false;
    uint16_t version = 0;
    uint16_t flags = 0;
    float float_flag = 0.0f;
    uint32_t field_0C = 0;
    uint16_t field_10 = 0;
    uint32_t uncompressed_size = 0;
    uint32_t payload_size = 0;
    std::string nonce_hex;
    std::string hmac_hex;
    bool hmac_ok = false;
};

struct ParseResult {
    std::string input_kind;
    std::string input_path;
    uint32_t raw_size = 0;
    std::vector<uint8_t> raw_blob;
    ContainerInfo container;
    SchemaInfo schema;
    TocInfo toc;
    std::vector<ObjectBlock> objects;
    ParseStats stats;
};

struct ProgressInfo {
    std::string stage;
    uint32_t current = 0;
    uint32_t total = 0;
};

using ProgressCallback = std::function<void(const ProgressInfo&)>;

ParseResult ParseFile(const std::string& path, const std::string& key_hex = "", ProgressCallback progress = {});
ParseResult ParseRawFile(const std::string& path, ProgressCallback progress = {});
std::string ToJson(const ParseResult& result);

// Parse just the schema section from raw blob bytes (bytes 0..schema_end).
// Used by the XML importer to rebuild a ParseResult without a full save.
SchemaInfo ParseSchemaOnly(const std::vector<uint8_t>& schema_bytes);

} // namespace SaveParserCpp
