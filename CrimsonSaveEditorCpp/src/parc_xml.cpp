/**
 * parc_xml.cpp — Lossless XML export/import for PARC save trees.
 *
 * Export streams XML with fprintf (no DOM — whole-save exports are large).
 * Import uses pugixml.
 *
 * Format (compact attribute names):
 *   <parcsave v="1" root="...">
 *     <schema hex="..."/>            blob bytes 0..schema_end, verbatim
 *     <container hex="..."/>         128-byte .save header (encrypted saves)
 *     <blocks>
 *       <block class="Name" ci="36" mbc="2" mask="FF03" res32="0">
 *         <f fi="0" n="_x" v="123"/>          scalar (typed value)
 *         <f fi="1" n="_y" hex="AABB"/>       scalar/bytes (raw hex)
 *         <f fi="2" n="_z" skip="1"/>         present but undecoded (bytes in gaps)
 *         <gap hex=".." src="N"/>             undecoded bytes before next field
 *         <obj fi="3" n="_o" mbc=".." mask=".." t="Type" ti="5" res8=".."
 *              s1="FFFFFFFF" s2="FFFFFFFF" res32="0" plen="0" p16="0" p8="0">
 *           ...children... <trail hex=".." src="N"/>
 *         </obj>
 *         <list fi="4" n="_l" header="hex" cnt="25" coff="1" cfmt="2">
 *           <e mbc=".." mask=".." t="Type" ti=".." res8=".." s1=".." s2=".." res32="..">
 *             ...children... <trail .../>
 *           </e>
 *         </list>
 *         <trail hex=".." src="N"/>
 *       </block>
 *     </blocks>
 *   </parcsave>
 *
 * Subtree files use <parcnode v="1" path="Block.field[3]..."> with a single
 * <block>/<obj>/<list>/<e>/<f> child.
 *
 * Offsets, POs, trailing sizes, list counts, TOC: never exported, always
 * recomputed by ParcSerializer. Types and fields resolve BY NAME against the
 * importing save's schema, so node files are shareable across saves.
 */
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "parc_xml.h"
#include "pugixml.hpp"
#ifdef _WIN32
#include <windows.h>
#else
#include <unistd.h>
#define GetCurrentProcessId() ((unsigned)getpid())
#endif
#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <unordered_map>

namespace ParcXml {

using namespace SaveParserCpp;

// ── Small helpers ──

static const char* HEXD = "0123456789ABCDEF";

static std::string ToHex(const uint8_t* data, size_t len) {
    std::string s;
    s.resize(len * 2);
    for (size_t i = 0; i < len; ++i) {
        s[i * 2] = HEXD[data[i] >> 4];
        s[i * 2 + 1] = HEXD[data[i] & 0xF];
    }
    return s;
}

static std::string ToHex(const std::vector<uint8_t>& v) { return ToHex(v.data(), v.size()); }

static bool FromHex(const char* s, std::vector<uint8_t>& out) {
    out.clear();
    size_t len = strlen(s);
    if (len % 2) return false;
    out.reserve(len / 2);
    for (size_t i = 0; i < len; i += 2) {
        auto nib = [](char c) -> int {
            if (c >= '0' && c <= '9') return c - '0';
            if (c >= 'A' && c <= 'F') return c - 'A' + 10;
            if (c >= 'a' && c <= 'f') return c - 'a' + 10;
            return -1;
        };
        int hi = nib(s[i]), lo = nib(s[i + 1]);
        if (hi < 0 || lo < 0) return false;
        out.push_back((uint8_t)((hi << 4) | lo));
    }
    return true;
}

static std::string XmlEscape(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (char c : s) {
        switch (c) {
        case '&': out += "&amp;"; break;
        case '<': out += "&lt;"; break;
        case '>': out += "&gt;"; break;
        case '"': out += "&quot;"; break;
        default: out += c;
        }
    }
    return out;
}

// ── Scalar value <-> bytes (exact round-trip or fall back to hex) ──

static bool IsFloatType(const std::string& type_name) {
    std::string l = type_name;
    std::transform(l.begin(), l.end(), l.begin(), [](unsigned char c) { return (char)std::tolower(c); });
    return l.find("float") != std::string::npos;
}

static bool IsSignedType(const std::string& type_name) {
    std::string l = type_name;
    std::transform(l.begin(), l.end(), l.begin(), [](unsigned char c) { return (char)std::tolower(c); });
    return l.rfind("int", 0) == 0;
}

static bool IsBoolType(const std::string& type_name) {
    std::string l = type_name;
    std::transform(l.begin(), l.end(), l.begin(), [](unsigned char c) { return (char)std::tolower(c); });
    return l == "bool";
}

// Encode a text value into meta_size LE bytes. Returns false if unparseable.
static bool EncodeScalar(const std::string& type_name, uint16_t size,
                         const char* text, std::vector<uint8_t>& out) {
    out.assign(size, 0);
    if (IsBoolType(type_name) && size == 1) {
        if (!strcmp(text, "true") || !strcmp(text, "1")) { out[0] = 1; return true; }
        if (!strcmp(text, "false") || !strcmp(text, "0")) { out[0] = 0; return true; }
        return false;
    }
    if (IsFloatType(type_name) && size == 4) {
        char* end = nullptr;
        float v = strtof(text, &end);
        if (end == text) return false;
        memcpy(out.data(), &v, 4);
        return true;
    }
    if (IsFloatType(type_name) && size == 8) {
        char* end = nullptr;
        double v = strtod(text, &end);
        if (end == text) return false;
        memcpy(out.data(), &v, 8);
        return true;
    }
    if (size == 1 || size == 2 || size == 4 || size == 8) {
        char* end = nullptr;
        if (IsSignedType(type_name)) {
            long long v = strtoll(text, &end, 10);
            if (end == text) return false;
            memcpy(out.data(), &v, size);
        } else {
            unsigned long long v = strtoull(text, &end, 10);
            if (end == text) return false;
            memcpy(out.data(), &v, size);
        }
        return true;
    }
    return false;
}

// Format raw bytes as a text value. Returns false if the text would not
// round-trip to the exact same bytes (caller exports hex instead).
static bool FormatScalar(const std::string& type_name, uint16_t size,
                         const std::vector<uint8_t>& raw, std::string& out) {
    if (raw.size() != size) return false;
    char buf[64];
    if (IsBoolType(type_name) && size == 1) {
        if (raw[0] > 1) return false;
        out = raw[0] ? "true" : "false";
        return true;
    }
    if (IsFloatType(type_name) && size == 4) {
        float v; memcpy(&v, raw.data(), 4);
        if (!std::isfinite(v)) return false;
        snprintf(buf, sizeof(buf), "%.9g", v);
    } else if (IsFloatType(type_name) && size == 8) {
        double v; memcpy(&v, raw.data(), 8);
        if (!std::isfinite(v)) return false;
        snprintf(buf, sizeof(buf), "%.17g", v);
    } else if (size == 1 || size == 2 || size == 4 || size == 8) {
        if (IsSignedType(type_name)) {
            long long v = 0;
            memcpy(&v, raw.data(), size);
            // sign-extend
            int shift = (8 - size) * 8;
            v = (v << shift) >> shift;
            snprintf(buf, sizeof(buf), "%lld", v);
        } else {
            unsigned long long v = 0;
            memcpy(&v, raw.data(), size);
            snprintf(buf, sizeof(buf), "%llu", v);
        }
    } else {
        return false;
    }
    // verify exact round-trip
    std::vector<uint8_t> check;
    if (!EncodeScalar(type_name, size, buf, check) || check != raw) return false;
    out = buf;
    return true;
}

// ── Path navigation (shared by export + graft) ──

struct PathSeg {
    std::string field;
    int index = -1;
};

static std::vector<PathSeg> SplitPath(const std::string& path) {
    std::vector<PathSeg> segs;
    size_t pos = 0;
    while (pos < path.size()) {
        if (path[pos] == '.') { pos++; continue; }
        PathSeg s;
        size_t br = path.find('[', pos);
        size_t dot = path.find('.', pos);
        size_t end = std::min(br, dot);
        if (end == std::string::npos) end = path.size();
        s.field = path.substr(pos, end - pos);
        pos = end;
        if (pos < path.size() && path[pos] == '[') {
            size_t close = path.find(']', pos);
            if (close == std::string::npos) break;
            s.index = atoi(path.substr(pos + 1, close - pos - 1).c_str());
            pos = close + 1;
        }
        segs.push_back(std::move(s));
    }
    return segs;
}

// ── Exporter ──

struct ExportCtx {
    FILE* f = nullptr;
    const std::vector<uint8_t>& blob;
};

static void Indent(ExportCtx& ctx, int n) {
    for (int i = 0; i < n; ++i) fputc(' ', ctx.f);
}

static void EmitGap(ExportCtx& ctx, uint32_t start, uint32_t end, int indent) {
    if (end <= start || end > ctx.blob.size()) return;
    Indent(ctx, indent);
    fprintf(ctx.f, "<gap hex=\"%s\" src=\"%u\"/>\n",
            ToHex(ctx.blob.data() + start, end - start).c_str(), start);
}

static void ExportField(ExportCtx& ctx, const GenericFieldValue& f, int indent);

// Walk child fields exactly like SerializeChildFields: gaps between fields,
// then the element/object trailing bytes derived from child_size_u32.
static void ExportChildren(ExportCtx& ctx, const GenericFieldValue& parent, int indent) {
    uint32_t cursor = parent.child_payload_offset + 4;
    for (const auto& cf : parent.child_fields) {
        if (!cf.present) continue;
        if (cf.start_offset > cursor && parent.child_payload_offset > 0) {
            EmitGap(ctx, cursor, cf.start_offset, indent);
        }
        ExportField(ctx, cf, indent);
        if (cf.end_offset > cursor) cursor = cf.end_offset;
    }
    if (parent.child_payload_offset > 0 && parent.child_size_u32 > 0) {
        uint32_t last_end = parent.child_payload_offset + 4;
        for (const auto& cf : parent.child_fields) {
            if (cf.present && cf.end_offset > last_end) last_end = cf.end_offset;
        }
        uint32_t trailing_pos = parent.child_payload_offset + parent.child_size_u32;
        if (last_end < trailing_pos && trailing_pos <= ctx.blob.size()) {
            Indent(ctx, indent);
            fprintf(ctx.f, "<trail hex=\"%s\" src=\"%u\"/>\n",
                    ToHex(ctx.blob.data() + last_end, trailing_pos - last_end).c_str(), last_end);
        }
    }
}

static void EmitLocatorAttrs(ExportCtx& ctx, const GenericFieldValue& f) {
    fprintf(ctx.f, " mbc=\"%u\" mask=\"%s\" t=\"%s\" ti=\"%d\" res8=\"%u\""
                   " s1=\"%08X\" s2=\"%08X\" res32=\"%u\"",
            f.child_mask_byte_count, ToHex(f.child_mask_bytes).c_str(),
            XmlEscape(f.child_type_name).c_str(), f.child_type_index,
            f.child_reserved_u8, f.child_sentinel1_u32, f.child_sentinel2_u32,
            f.child_reserved_u32);
    if (f.child_prefix_len > 0) {
        fprintf(ctx.f, " plen=\"%u\" p16=\"%u\" p8=\"%u\"",
                f.child_prefix_len, f.child_prefix_u16, f.child_prefix_u8);
    }
}

static void ExportField(ExportCtx& ctx, const GenericFieldValue& f, int indent) {
    if (f.meta_kind == 4 || f.meta_kind == 5) {
        Indent(ctx, indent);
        fprintf(ctx.f, "<obj fi=\"%u\" n=\"%s\"", f.field_index, XmlEscape(f.name).c_str());
        EmitLocatorAttrs(ctx, f);
        if (f.child_fields.empty() && f.child_payload_offset == 0) {
            // header-only locator (undecodable child) — preserve raw bytes
            fprintf(ctx.f, " hex=\"%s\"/>\n", ToHex(f.raw_value).c_str());
            return;
        }
        fprintf(ctx.f, ">\n");
        ExportChildren(ctx, f, indent + 1);
        Indent(ctx, indent);
        fprintf(ctx.f, "</obj>\n");
        return;
    }

    if (f.meta_kind == 6 || f.meta_kind == 7) {
        Indent(ctx, indent);
        fprintf(ctx.f, "<list fi=\"%u\" n=\"%s\" header=\"%s\" cnt=\"%u\" coff=\"%u\" cfmt=\"%u\"",
                f.field_index, XmlEscape(f.name).c_str(),
                ToHex(f.list_header_raw).c_str(), f.list_count,
                f.list_count_offset, f.list_count_format);
        if (f.list_elements.empty() && f.list_count > 0) {
            // partially decoded list — preserve raw bytes verbatim
            fprintf(ctx.f, " hex=\"%s\" src=\"%u\"/>\n", ToHex(f.raw_value).c_str(), f.start_offset);
            return;
        }
        fprintf(ctx.f, ">\n");
        for (const auto& e : f.list_elements) {
            Indent(ctx, indent + 1);
            fprintf(ctx.f, "<e");
            EmitLocatorAttrs(ctx, e);
            if (e.child_fields.empty() && !e.raw_value.empty()) {
                fprintf(ctx.f, " hex=\"%s\" src=\"%u\"/>\n", ToHex(e.raw_value).c_str(), e.start_offset);
                continue;
            }
            fprintf(ctx.f, ">\n");
            ExportChildren(ctx, e, indent + 2);
            Indent(ctx, indent + 1);
            fprintf(ctx.f, "</e>\n");
        }
        Indent(ctx, indent);
        fprintf(ctx.f, "</list>\n");
        return;
    }

    // Scalars / byte arrays / dynamic arrays / skipped
    Indent(ctx, indent);
    fprintf(ctx.f, "<f fi=\"%u\" n=\"%s\"", f.field_index, XmlEscape(f.name).c_str());
    if (f.start_offset == 0 && f.end_offset == 0) {
        fprintf(ctx.f, " skip=\"1\"/>\n");
        return;
    }
    std::string v;
    if ((f.meta_kind == 0 || f.meta_kind == 2) &&
        FormatScalar(f.type_name, f.meta_size, f.raw_value, v)) {
        fprintf(ctx.f, " v=\"%s\"/>\n", v.c_str());
    } else {
        fprintf(ctx.f, " hex=\"%s\"/>\n", ToHex(f.raw_value).c_str());
    }
}

static void ExportBlock(ExportCtx& ctx, const ObjectBlock& obj, int indent) {
    Indent(ctx, indent);
    fprintf(ctx.f, "<block class=\"%s\" ci=\"%u\" mbc=\"%u\" mask=\"%s\" res32=\"%u\">\n",
            XmlEscape(obj.class_name).c_str(), obj.class_index,
            obj.mask_byte_count, ToHex(obj.header_mask_bytes).c_str(), obj.reserved_u32);

    uint32_t cursor = obj.data_offset + 2 + (uint32_t)obj.header_mask_bytes.size() + 4;
    for (const auto& f : obj.fields) {
        if (!f.present) continue;
        if (f.start_offset > cursor) EmitGap(ctx, cursor, f.start_offset, indent + 1);
        ExportField(ctx, f, indent + 1);
        if (f.end_offset > cursor) cursor = f.end_offset;
    }
    uint32_t block_end = obj.data_offset + obj.data_size;
    if (cursor < block_end && block_end <= ctx.blob.size()) {
        Indent(ctx, indent + 1);
        fprintf(ctx.f, "<trail hex=\"%s\" src=\"%u\"/>\n",
                ToHex(ctx.blob.data() + cursor, block_end - cursor).c_str(), cursor);
    }
    Indent(ctx, indent);
    fprintf(ctx.f, "</block>\n");
}

std::string ExportXml(const ParcEngine::SaveTree& tree,
                      const std::string& xml_path,
                      const std::string& node_path) {
    FILE* f = fopen(xml_path.c_str(), "wb");
    if (!f) return "Cannot open output: " + xml_path;
    ExportCtx ctx{f, tree.blob};

    if (node_path.empty()) {
        fprintf(f, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
        fprintf(f, "<parcsave v=\"1\" root=\"%s\">\n",
                XmlEscape(tree.parsed.schema.root_type).c_str());
        fprintf(f, " <schema hex=\"%s\"/>\n",
                ToHex(tree.blob.data(), tree.parsed.schema.schema_end).c_str());
        if (tree.is_encrypted && !tree.original_header.empty()) {
            fprintf(f, " <container hex=\"%s\"/>\n", ToHex(tree.original_header).c_str());
        }
        fprintf(f, " <blocks>\n");
        for (const auto& obj : tree.parsed.objects) {
            ExportBlock(ctx, obj, 2);
        }
        fprintf(f, " </blocks>\n");
        fprintf(f, "</parcsave>\n");
        fclose(f);
        return "";
    }

    // Subtree export
    auto segs = SplitPath(node_path);
    if (segs.empty()) { fclose(f); return "Empty node path"; }

    const ObjectBlock* block = nullptr;
    for (const auto& obj : tree.parsed.objects) {
        if (obj.class_name == segs[0].field) { block = &obj; break; }
    }
    if (!block) {
        for (const auto& obj : tree.parsed.objects) {
            if (obj.class_name.find(segs[0].field) != std::string::npos) { block = &obj; break; }
        }
    }
    if (!block) { fclose(f); return "Block not found: " + segs[0].field; }

    fprintf(f, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
    fprintf(f, "<parcnode v=\"1\" path=\"%s\">\n", XmlEscape(node_path).c_str());

    if (segs.size() == 1 && segs[0].index < 0) {
        ExportBlock(ctx, *block, 1);
        fprintf(f, "</parcnode>\n");
        fclose(f);
        return "";
    }

    const GenericFieldValue* cur = nullptr;
    const std::vector<GenericFieldValue>* fields = &block->fields;
    // segs[0].index on a block is not meaningful; start at segs[1] unless
    // the first segment carried an index (treat as error).
    for (size_t i = 1; i < segs.size(); ++i) {
        const auto& s = segs[i];
        const GenericFieldValue* found = nullptr;
        for (const auto& fv : *fields) {
            if (fv.name == s.field) { found = &fv; break; }
        }
        if (!found) { fclose(f); return "Field not found: " + s.field; }
        cur = found;
        if (s.index >= 0) {
            if (s.index >= (int)found->list_elements.size()) {
                fclose(f);
                return "Index out of range: " + s.field + "[" + std::to_string(s.index) + "]";
            }
            cur = &found->list_elements[s.index];
            fields = &cur->child_fields;
        } else {
            fields = &found->child_fields;
        }
    }
    if (!cur) { fclose(f); return "Node not found: " + node_path; }

    if (cur->decode_kind == "list_element") {
        fprintf(f, " <e");
        EmitLocatorAttrs(ctx, *cur);
        fprintf(f, ">\n");
        ExportChildren(ctx, *cur, 2);
        fprintf(f, " </e>\n");
    } else {
        ExportField(ctx, *cur, 1);
    }
    fprintf(f, "</parcnode>\n");
    fclose(f);
    return "";
}

// ── Importer ──

struct ImportCtx {
    const SchemaInfo* schema = nullptr;
    std::unordered_map<std::string, const TypeDef*> type_by_name;
    std::unordered_map<uint32_t, const TypeDef*> type_by_index;

    void Build(const SchemaInfo& s) {
        schema = &s;
        for (const auto& t : s.types) {
            type_by_name[t.name] = &t;
            type_by_index[t.index] = &t;
        }
    }

    // Resolve by name first (per-save type indices differ), index fallback.
    const TypeDef* ResolveType(const char* name, int index) const {
        if (name && *name) {
            auto it = type_by_name.find(name);
            if (it != type_by_name.end()) return it->second;
        }
        auto it2 = type_by_index.find((uint32_t)index);
        if (it2 != type_by_index.end()) return it2->second;
        return nullptr;
    }
};

static int ResolveFieldIndex(const TypeDef& type, const char* name, int fi) {
    if (name && *name) {
        for (size_t i = 0; i < type.fields.size(); ++i) {
            if (type.fields[i].name == name) return (int)i;
        }
    }
    if (fi >= 0 && fi < (int)type.fields.size()) return fi;
    return -1;
}

static GenericFieldValue ImportNode(ImportCtx& ctx, const pugi::xml_node& node,
                                    const TypeDef* owner_type);

// Populate target's child_* members + child_fields from an <obj>/<e> node.
static void ImportLocator(ImportCtx& ctx, const pugi::xml_node& node,
                          GenericFieldValue& out) {
    out.child_mask_byte_count = (uint16_t)node.attribute("mbc").as_uint(1);
    out.child_type_index = node.attribute("ti").as_int(-1);
    out.child_type_name = node.attribute("t").as_string("");
    out.child_reserved_u8 = (uint8_t)node.attribute("res8").as_uint(0);
    out.child_sentinel1_u32 = (uint32_t)strtoul(node.attribute("s1").as_string("FFFFFFFF"), nullptr, 16);
    out.child_sentinel2_u32 = (uint32_t)strtoul(node.attribute("s2").as_string("FFFFFFFF"), nullptr, 16);
    out.child_reserved_u32 = node.attribute("res32").as_uint(0);
    out.child_prefix_len = (uint8_t)node.attribute("plen").as_uint(0);
    out.child_prefix_u16 = (uint16_t)node.attribute("p16").as_uint(0);
    out.child_prefix_u8 = (uint8_t)node.attribute("p8").as_uint(0);

    const TypeDef* child_type = ctx.ResolveType(node.attribute("t").as_string(nullptr),
                                                out.child_type_index);
    if (!child_type) {
        throw std::runtime_error(std::string("Type not in this save's schema: ") +
                                 node.attribute("t").as_string("?"));
    }
    out.child_type_index = (int32_t)child_type->index;
    out.child_type_name = child_type->name;

    // Raw-preserved locator (undecodable child)
    if (node.attribute("hex")) {
        std::vector<uint8_t> raw;
        if (!FromHex(node.attribute("hex").as_string(""), raw)) {
            throw std::runtime_error("Bad hex in locator node");
        }
        out.raw_value = std::move(raw);
        out.start_offset = node.attribute("src").as_uint(0);
        if (out.start_offset > 0) {
            out.end_offset = out.start_offset + (uint32_t)out.raw_value.size();
        }
        return;
    }

    // Children: build full schema-order field vector, attach gaps, set mask.
    std::vector<GenericFieldValue> fields;
    fields.reserve(child_type->fields.size());
    for (size_t i = 0; i < child_type->fields.size(); ++i) {
        const auto& fd = child_type->fields[i];
        GenericFieldValue gv;
        gv.field_index = (uint32_t)i;
        gv.name = fd.name;
        gv.type_name = fd.type_name;
        gv.meta_kind = fd.meta_kind;
        gv.meta_size = fd.meta_size;
        gv.meta_aux = fd.meta_aux;
        gv.present = false;
        gv.decode_kind = "absent";
        fields.push_back(std::move(gv));
    }

    // Mask: start from the exported mask (preserves padding bits), then
    // clear/set bits for schema fields based on what the XML actually contains.
    std::vector<uint8_t> mask;
    FromHex(node.attribute("mask").as_string(""), mask);
    mask.resize(out.child_mask_byte_count, 0);
    for (size_t i = 0; i < child_type->fields.size(); ++i) {
        size_t byte = i / 8;
        if (byte < mask.size()) mask[byte] &= (uint8_t)~(1u << (i % 8));
    }

    std::vector<uint8_t> pending_gap;
    uint32_t pending_gap_src = 0;
    std::vector<uint8_t> trailing;
    uint32_t trailing_src = 0;

    for (pugi::xml_node child = node.first_child(); child; child = child.next_sibling()) {
        const char* tag = child.name();
        if (!strcmp(tag, "gap")) {
            FromHex(child.attribute("hex").as_string(""), pending_gap);
            pending_gap_src = child.attribute("src").as_uint(0);
            continue;
        }
        if (!strcmp(tag, "trail")) {
            FromHex(child.attribute("hex").as_string(""), trailing);
            trailing_src = child.attribute("src").as_uint(0);
            continue;
        }
        if (strcmp(tag, "f") && strcmp(tag, "obj") && strcmp(tag, "list")) continue;

        int fi = ResolveFieldIndex(*child_type, child.attribute("n").as_string(nullptr),
                                   child.attribute("fi").as_int(-1));
        if (fi < 0) {
            throw std::runtime_error(std::string("Field not in type ") + child_type->name +
                                     ": " + child.attribute("n").as_string("?"));
        }
        GenericFieldValue gv = ImportNode(ctx, child, child_type);
        gv.field_index = (uint32_t)fi;
        gv.name = child_type->fields[fi].name;
        gv.type_name = child_type->fields[fi].type_name;
        gv.meta_kind = child_type->fields[fi].meta_kind;
        gv.meta_size = child_type->fields[fi].meta_size;
        gv.meta_aux = child_type->fields[fi].meta_aux;
        if (!pending_gap.empty()) {
            gv.gap_before = std::move(pending_gap);
            gv.gap_before_src = pending_gap_src;
            pending_gap.clear();
            pending_gap_src = 0;
        }
        size_t byte = (size_t)fi / 8;
        if (byte < mask.size()) mask[byte] |= (uint8_t)(1u << (fi % 8));
        fields[(size_t)fi] = std::move(gv);
    }
    if (!pending_gap.empty()) {
        throw std::runtime_error("Dangling <gap> with no following field (use <trail>)");
    }

    out.child_mask_bytes = std::move(mask);
    out.child_fields = std::move(fields);
    out.trailing_bytes = std::move(trailing);
    out.trailing_src = trailing_src;
}

// Build a GenericFieldValue from <f>/<obj>/<list>. Field identity (fi, name,
// meta_*) is filled by the caller, which knows the owner type.
static GenericFieldValue ImportNode(ImportCtx& ctx, const pugi::xml_node& node,
                                    const TypeDef* owner_type) {
    (void)owner_type;
    GenericFieldValue gv;
    gv.present = true;
    const char* tag = node.name();

    if (!strcmp(tag, "f")) {
        if (node.attribute("skip")) {
            gv.decode_kind = "skipped";
            return gv;
        }
        gv.decode_kind = "fixed_prefix";
        if (node.attribute("hex")) {
            if (!FromHex(node.attribute("hex").as_string(""), gv.raw_value)) {
                throw std::runtime_error("Bad hex in <f>");
            }
            return gv;
        }
        // typed value — encoded by the caller once meta is known; stash text
        gv.value_repr = node.attribute("v").as_string("");
        gv.note = "@v";  // marker: encode after meta assignment
        return gv;
    }

    if (!strcmp(tag, "obj")) {
        gv.decode_kind = "object_locator";
        ImportLocator(ctx, node, gv);
        return gv;
    }

    if (!strcmp(tag, "list")) {
        gv.decode_kind = "object_list";
        if (!FromHex(node.attribute("header").as_string(""), gv.list_header_raw)) {
            throw std::runtime_error("Bad header hex in <list>");
        }
        gv.list_header_size = (uint32_t)gv.list_header_raw.size();
        gv.list_count = node.attribute("cnt").as_uint(0);
        gv.list_count_offset = node.attribute("coff").as_uint(0);
        gv.list_count_format = (uint8_t)node.attribute("cfmt").as_uint(0);
        if (!gv.list_header_raw.empty()) {
            gv.list_prefix_u8 = gv.list_header_raw[0];
        }
        if (node.attribute("hex")) {
            // raw-preserved partial list
            if (!FromHex(node.attribute("hex").as_string(""), gv.raw_value)) {
                throw std::runtime_error("Bad hex in <list>");
            }
            gv.start_offset = node.attribute("src").as_uint(0);
            if (gv.start_offset > 0) {
                gv.end_offset = gv.start_offset + (uint32_t)gv.raw_value.size();
            }
            return gv;
        }
        int idx = 0;
        for (pugi::xml_node e = node.child("e"); e; e = e.next_sibling("e")) {
            GenericFieldValue elem;
            elem.present = true;
            elem.decode_kind = "list_element";
            elem.field_index = (uint32_t)idx;
            elem.name = "[" + std::to_string(idx) + "]";
            ImportLocator(ctx, e, elem);
            elem.type_name = elem.child_type_name;
            gv.list_elements.push_back(std::move(elem));
            idx++;
        }
        return gv;
    }

    throw std::runtime_error(std::string("Unexpected node: ") + tag);
}

// Encode any "@v" scalar placeholders now that meta info is assigned.
static void FinalizeScalars(std::vector<GenericFieldValue>& fields) {
    for (auto& f : fields) {
        if (f.note == "@v") {
            std::vector<uint8_t> enc;
            if (!EncodeScalar(f.type_name, f.meta_size, f.value_repr.c_str(), enc)) {
                throw std::runtime_error("Cannot encode value '" + f.value_repr +
                                         "' for field " + f.name);
            }
            f.raw_value = std::move(enc);
            f.note.clear();
        }
        FinalizeScalars(f.child_fields);
        for (auto& e : f.list_elements) {
            FinalizeScalars(e.child_fields);
        }
    }
}

static ObjectBlock ImportBlock(ImportCtx& ctx, const pugi::xml_node& node, uint32_t entry_index) {
    ObjectBlock blk;
    blk.entry_index = entry_index;

    const TypeDef* type = ctx.ResolveType(node.attribute("class").as_string(nullptr),
                                          node.attribute("ci").as_int(-1));
    if (!type) {
        throw std::runtime_error(std::string("Block class not in schema: ") +
                                 node.attribute("class").as_string("?"));
    }
    blk.class_index = type->index;
    blk.class_name = type->name;
    blk.mask_byte_count = (uint16_t)node.attribute("mbc").as_uint(1);
    blk.reserved_u32 = node.attribute("res32").as_uint(0);

    std::vector<uint8_t> mask;
    FromHex(node.attribute("mask").as_string(""), mask);
    mask.resize(blk.mask_byte_count, 0);
    for (size_t i = 0; i < type->fields.size(); ++i) {
        size_t byte = i / 8;
        if (byte < mask.size()) mask[byte] &= (uint8_t)~(1u << (i % 8));
    }

    std::vector<GenericFieldValue> fields;
    fields.reserve(type->fields.size());
    for (size_t i = 0; i < type->fields.size(); ++i) {
        const auto& fd = type->fields[i];
        GenericFieldValue gv;
        gv.field_index = (uint32_t)i;
        gv.name = fd.name;
        gv.type_name = fd.type_name;
        gv.meta_kind = fd.meta_kind;
        gv.meta_size = fd.meta_size;
        gv.meta_aux = fd.meta_aux;
        gv.present = false;
        gv.decode_kind = "absent";
        fields.push_back(std::move(gv));
    }

    std::vector<uint8_t> pending_gap;
    uint32_t pending_gap_src = 0;

    for (pugi::xml_node child = node.first_child(); child; child = child.next_sibling()) {
        const char* tag = child.name();
        if (!strcmp(tag, "gap")) {
            FromHex(child.attribute("hex").as_string(""), pending_gap);
            pending_gap_src = child.attribute("src").as_uint(0);
            continue;
        }
        if (!strcmp(tag, "trail")) {
            FromHex(child.attribute("hex").as_string(""), blk.trailing_bytes);
            blk.trailing_src = child.attribute("src").as_uint(0);
            continue;
        }
        if (strcmp(tag, "f") && strcmp(tag, "obj") && strcmp(tag, "list")) continue;

        int fi = ResolveFieldIndex(*type, child.attribute("n").as_string(nullptr),
                                   child.attribute("fi").as_int(-1));
        if (fi < 0) {
            throw std::runtime_error(std::string("Field not in block ") + type->name +
                                     ": " + child.attribute("n").as_string("?"));
        }
        GenericFieldValue gv = ImportNode(ctx, child, type);
        gv.field_index = (uint32_t)fi;
        gv.name = type->fields[fi].name;
        gv.type_name = type->fields[fi].type_name;
        gv.meta_kind = type->fields[fi].meta_kind;
        gv.meta_size = type->fields[fi].meta_size;
        gv.meta_aux = type->fields[fi].meta_aux;
        if (!pending_gap.empty()) {
            gv.gap_before = std::move(pending_gap);
            gv.gap_before_src = pending_gap_src;
            pending_gap.clear();
        }
        size_t byte = (size_t)fi / 8;
        if (byte < mask.size()) mask[byte] |= (uint8_t)(1u << (fi % 8));
        fields[(size_t)fi] = std::move(gv);
    }
    if (!pending_gap.empty()) {
        throw std::runtime_error("Dangling <gap> in block " + blk.class_name);
    }

    blk.header_mask_bytes = std::move(mask);
    blk.fields = std::move(fields);
    FinalizeScalars(blk.fields);
    return blk;
}

static std::string ImportXmlDoc(pugi::xml_document& doc,
                                std::vector<uint8_t>& out_blob,
                                std::vector<uint8_t>& out_header) {
    try {
        pugi::xml_node root = doc.child("parcsave");
        if (!root) return "Not a <parcsave> file (for node files use import-node)";

        std::vector<uint8_t> schema_bytes;
        if (!FromHex(root.child("schema").attribute("hex").as_string(""), schema_bytes) ||
            schema_bytes.empty()) {
            return "Missing/bad <schema> hex";
        }
        out_header.clear();
        if (root.child("container")) {
            FromHex(root.child("container").attribute("hex").as_string(""), out_header);
        }

        ParseResult pr;
        pr.input_kind = "xml_import";
        pr.schema = ParseSchemaOnly(schema_bytes);
        if (pr.schema.schema_end != schema_bytes.size()) {
            return "Schema hex does not parse cleanly (end=" +
                   std::to_string(pr.schema.schema_end) + " size=" +
                   std::to_string(schema_bytes.size()) + ")";
        }

        ImportCtx ctx;
        ctx.Build(pr.schema);

        uint32_t idx = 0;
        for (pugi::xml_node b = root.child("blocks").child("block"); b;
             b = b.next_sibling("block")) {
            ObjectBlock blk = ImportBlock(ctx, b, idx);
            TocEntry te;
            te.index = idx;
            te.class_index = blk.class_index;
            te.class_name = blk.class_name;
            pr.toc.entries.push_back(std::move(te));
            pr.objects.push_back(std::move(blk));
            idx++;
        }
        pr.toc.entry_count = idx;
        if (idx == 0) return "No <block> nodes found";

        out_blob = ParcSerializer::Serialize(pr, schema_bytes);

        // Self-check: the produced blob must parse and reserialize identically.
        {
            char tmp[512];
            snprintf(tmp, sizeof(tmp), "%s\\parc_xml_%u.tmp",
                     getenv("TEMP") ? getenv("TEMP") : ".", (unsigned)GetCurrentProcessId());
            std::ofstream o(tmp, std::ios::binary);
            o.write((const char*)out_blob.data(), (std::streamsize)out_blob.size());
            o.close();
            ParseResult check = SaveParserCpp::ParseRawFile(tmp);
            std::remove(tmp);
            auto re = ParcSerializer::Serialize(check, out_blob);
            if (re != out_blob) {
                return "Self-check failed: imported blob does not reserialize identically "
                       "(refusing to produce a save)";
            }
        }
        return "";
    } catch (const std::exception& e) {
        return std::string("Import error: ") + e.what();
    }
}

std::string ImportXml(const std::string& xml_path,
                      std::vector<uint8_t>& out_blob,
                      std::vector<uint8_t>& out_header) {
    pugi::xml_document doc;
    auto res = doc.load_file(xml_path.c_str());
    if (!res) return std::string("XML parse error: ") + res.description();
    return ImportXmlDoc(doc, out_blob, out_header);
}

std::string ImportXmlBuffer(const void* data, size_t size,
                            std::vector<uint8_t>& out_blob,
                            std::vector<uint8_t>& out_header) {
    pugi::xml_document doc;
    auto res = doc.load_buffer(data, size);
    if (!res) return std::string("XML parse error: ") + res.description();
    return ImportXmlDoc(doc, out_blob, out_header);
}

// ── Lobby save generation ──
// lobby.save shares the .save container (ChaCha20+HMAC+LZ4+PARC) but holds a
// tiny LobbySaveData block — just load-menu metadata (slot display name,
// character key, level). The game does not require it to match save.save, so
// a generated one paired with any save.save makes a drag-and-drop slot.
// Template captured from a real slot100 lobby.save (game version 2).

static const char* LOBBY_XML_FMT =
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
    "<parcsave v=\"1\" root=\"LobbySaveData\">\n"
    " <schema hex=\"FFFF0400000000000000000000000F00000002000D0000004C6F62627953617665"
    "4461746103000D0000005F736C6F7453617665446174610D0000005265666C6563744F626A65637404"
    "00080008000000100000005F67616D655361766556657273696F6E0600000075696E74333200000400"
    "000000000B0000005F67656E65726174654E6F05000000696E74363400000800000000000C00000053"
    "6C6F74536176654461746106000D0000005F6368617261637465724B65790C00000043686172616374"
    "65724B657900000400000000000E0000005F67656E65726174656454696D65050000004374633634000"
    "0080000000000060000005F6C6576656C06000000544C6576656C00000400000000001A0000005F736C"
    "6F74446973706C61794E616D654D697373696F6E4B65790A0000004D697373696F6E4B657900000400"
    "000000001C0000005F736C6F74446973706C61794E616D654D61696E51756573744B65790800000051"
    "756573744B65790000040000000000120000005F637573746F6D446973706C61794E616D650D000000"
    "737461746963737472696E67410100010000000000\"/>\n"
    " <container hex=\"5341564502008000000000000200000000002402000091010000BE15364B4D81"
    "B92B6ABB4C1D8D7CC4954D874B435718B9F57F0207D7B1837D57443C101B2DA1F50ED6FEC399D6DEE8"
    "5F28C600801B44B7A86FC501000000D1F927C600E01B4405F46EC50100000083EF27C600201C44D0E5"
    "6DC501000000A2EA27C600401C44\"/>\n"
    " <blocks>\n"
    "  <block class=\"LobbySaveData\" ci=\"0\" mbc=\"1\" mask=\"07\" res32=\"0\">\n"
    "   <obj fi=\"0\" n=\"_slotSaveData\" mbc=\"1\" mask=\"3F\" t=\"SlotSaveData\" ti=\"1\""
    " res8=\"0\" s1=\"FFFFFFFF\" s2=\"FFFFFFFF\" res32=\"0\">\n"
    "    <f fi=\"0\" n=\"_characterKey\" v=\"%d\"/>\n"
    "    <f fi=\"1\" n=\"_generatedTime\" v=\"1781280213\"/>\n"
    "    <f fi=\"2\" n=\"_level\" v=\"%d\"/>\n"
    "    <f fi=\"3\" n=\"_slotDisplayNameMissionKey\" v=\"1000052\"/>\n"
    "    <f fi=\"4\" n=\"_slotDisplayNameMainQuestKey\" v=\"1000027\"/>\n"
    "    <f fi=\"5\" n=\"_customDisplayName\" hex=\"%s\"/>\n"
    "   </obj>\n"
    "   <f fi=\"1\" n=\"_gameSaveVersion\" v=\"1\"/>\n"
    "   <f fi=\"2\" n=\"_generateNo\" v=\"8475\"/>\n"
    "  </block>\n"
    " </blocks>\n"
    "</parcsave>\n";

std::string BuildLobbySave(const std::string& display_name,
                           std::vector<uint8_t>& out_blob,
                           std::vector<uint8_t>& out_header,
                           int character_key, int level) {
    // _customDisplayName = u32 LE length + raw bytes (staticstringA).
    // Keep it printable ASCII so the load menu renders it predictably.
    std::string name;
    for (char c : display_name) {
        if ((unsigned char)c >= 0x20 && (unsigned char)c < 0x7F) name += c;
        if (name.size() >= 32) break;
    }
    if (name.empty()) name = "Custom Save";

    uint32_t nlen = (uint32_t)name.size();
    uint8_t lenle[4] = {(uint8_t)(nlen), (uint8_t)(nlen >> 8),
                        (uint8_t)(nlen >> 16), (uint8_t)(nlen >> 24)};
    std::string name_hex = ToHex(lenle, 4) +
                           ToHex((const uint8_t*)name.data(), name.size());

    char xml[8192];
    int n = snprintf(xml, sizeof(xml), LOBBY_XML_FMT,
                     character_key, level, name_hex.c_str());
    if (n <= 0 || n >= (int)sizeof(xml)) return "lobby XML build overflow";

    return ImportXmlBuffer(xml, (size_t)n, out_blob, out_header);
}

std::string ImportNodeXml(ParcEngine::SaveTree& tree,
                          const std::string& xml_path,
                          const std::string& override_path) {
    try {
        pugi::xml_document doc;
        auto res = doc.load_file(xml_path.c_str());
        if (!res) return std::string("XML parse error: ") + res.description();

        pugi::xml_node root = doc.child("parcnode");
        if (!root) return "Not a <parcnode> file (for full saves use import)";

        std::string path = override_path.empty()
            ? root.attribute("path").as_string("") : override_path;
        if (path.empty()) return "No path in file and none given";

        auto segs = SplitPath(path);
        if (segs.empty()) return "Bad path: " + path;

        // Refresh tree from blob (blob is canonical)
        ParcEngine::Reparse(tree, false);

        ImportCtx ctx;
        ctx.Build(tree.parsed.schema);

        ObjectBlock* block = nullptr;
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name == segs[0].field) { block = &obj; break; }
        }
        if (!block) {
            for (auto& obj : tree.parsed.objects) {
                if (obj.class_name.find(segs[0].field) != std::string::npos) { block = &obj; break; }
            }
        }
        if (!block) return "Block not found: " + segs[0].field;

        pugi::xml_node payload = root.first_child();
        while (payload && payload.type() != pugi::node_element) payload = payload.next_sibling();
        if (!payload) return "No payload node in file";

        // Whole-block replacement
        if (segs.size() == 1 && segs[0].index < 0) {
            if (strcmp(payload.name(), "block")) return "Path is a block but payload is not <block>";
            ObjectBlock nb = ImportBlock(ctx, payload, block->entry_index);
            if (nb.class_index != block->class_index) {
                return "Block class mismatch: " + nb.class_name + " vs " + block->class_name;
            }
            nb.data_offset = 0;
            nb.data_size = 0;
            *block = std::move(nb);
        } else {
            // Navigate to the parent container of the final segment.
            std::vector<GenericFieldValue*> ancestors;
            std::vector<GenericFieldValue>* fields = &block->fields;
            const TypeDef* container_type = ctx.ResolveType(block->class_name.c_str(),
                                                            (int)block->class_index);
            std::vector<uint8_t>* container_mask = &block->header_mask_bytes;

            for (size_t i = 1; i + 1 < segs.size(); ++i) {
                const auto& s = segs[i];
                GenericFieldValue* found = nullptr;
                for (auto& fv : *fields) {
                    if (fv.name == s.field) { found = &fv; break; }
                }
                if (!found) return "Field not found: " + s.field;
                ancestors.push_back(found);
                if (s.index >= 0) {
                    if (s.index >= (int)found->list_elements.size()) {
                        return "Index out of range: " + s.field;
                    }
                    auto& elem = found->list_elements[s.index];
                    fields = &elem.child_fields;
                    container_type = ctx.ResolveType(elem.child_type_name.c_str(),
                                                     elem.child_type_index);
                    container_mask = &elem.child_mask_bytes;
                } else {
                    fields = &found->child_fields;
                    container_type = ctx.ResolveType(found->child_type_name.c_str(),
                                                     found->child_type_index);
                    container_mask = &found->child_mask_bytes;
                }
            }
            if (!container_type) return "Cannot resolve container type for path";

            const auto& last = segs.back();
            GenericFieldValue* target_field = nullptr;
            for (auto& fv : *fields) {
                if (fv.name == last.field) { target_field = &fv; break; }
            }

            if (last.index >= 0) {
                // Element replace/append into target_field's list
                if (!target_field) return "List not found: " + last.field;
                if (strcmp(payload.name(), "e")) return "Path is an element but payload is not <e>";
                ancestors.push_back(target_field);

                GenericFieldValue elem;
                elem.present = true;
                elem.decode_kind = "list_element";
                ImportLocator(ctx, payload, elem);
                elem.type_name = elem.child_type_name;
                std::vector<GenericFieldValue> tmpv;
                tmpv.push_back(std::move(elem));
                FinalizeScalars(tmpv);
                elem = std::move(tmpv[0]);

                auto& elems = target_field->list_elements;
                if (last.index < (int)elems.size()) {
                    elem.field_index = (uint32_t)last.index;
                    elem.name = "[" + std::to_string(last.index) + "]";
                    elems[(size_t)last.index] = std::move(elem);
                } else if (last.index == (int)elems.size()) {
                    elem.field_index = (uint32_t)elems.size();
                    elem.name = "[" + std::to_string(elems.size()) + "]";
                    elems.push_back(std::move(elem));
                } else {
                    return "Element index " + std::to_string(last.index) +
                           " out of range (list has " + std::to_string(elems.size()) + ")";
                }
            } else {
                // Field replacement in the container
                int fi = ResolveFieldIndex(*container_type, last.field.c_str(), -1);
                if (fi < 0) {
                    return "Field " + last.field + " not in type " + container_type->name;
                }
                if (!strcmp(payload.name(), "e")) return "Payload <e> needs an [index] path";
                GenericFieldValue gv = ImportNode(ctx, payload, container_type);
                gv.field_index = (uint32_t)fi;
                gv.name = container_type->fields[fi].name;
                gv.type_name = container_type->fields[fi].type_name;
                gv.meta_kind = container_type->fields[fi].meta_kind;
                gv.meta_size = container_type->fields[fi].meta_size;
                gv.meta_aux = container_type->fields[fi].meta_aux;
                std::vector<GenericFieldValue> tmpv;
                tmpv.push_back(std::move(gv));
                FinalizeScalars(tmpv);
                gv = std::move(tmpv[0]);

                size_t byte = (size_t)fi / 8;
                if (byte < container_mask->size()) {
                    (*container_mask)[byte] |= (uint8_t)(1u << (fi % 8));
                } else {
                    return "Field bit " + std::to_string(fi) +
                           " does not fit the container mask (mbc too small)";
                }

                if (target_field) {
                    *target_field = std::move(gv);
                } else {
                    // Previously absent field: replace the placeholder slot
                    bool placed = false;
                    for (auto& fv : *fields) {
                        if ((int)fv.field_index == fi) { fv = std::move(gv); placed = true; break; }
                    }
                    if (!placed) return "Cannot place field " + last.field +
                                        " (container has no slot for it)";
                }
            }

            // Clear raw_value on every ancestor field so the serializer rebuilds the path
            for (auto* a : ancestors) a->raw_value.clear();
        }

        // Serialize + reparse
        tree.blob = ParcSerializer::Serialize(tree.parsed, tree.blob);
        ParcEngine::Reparse(tree, false);
        return "";
    } catch (const std::exception& e) {
        return std::string("Import error: ") + e.what();
    }
}

} // namespace ParcXml
