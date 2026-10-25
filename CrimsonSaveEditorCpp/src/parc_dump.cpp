/**
 * parc_dump — CLI tool to dump a full PARC save tree as JSON.
 *
 * Usage:
 *   parc_dump <save_path> [output.json]
 *   parc_dump --raw <raw_blob_path> [output.json]
 *
 * If output is omitted, writes to stdout.
 * If output is "-", writes to stdout.
 */
#include "save_parser_cpp.h"
#include <cstdio>
#include <cstring>
#include <fstream>
#include <chrono>

int main(int argc, char* argv[]) {
    if (argc < 2) {
        fprintf(stderr, "Usage: parc_dump <save_path> [output.json]\n");
        fprintf(stderr, "       parc_dump --raw <raw_blob_path> [output.json]\n");
        return 1;
    }

    bool raw_mode = false;
    const char* input_path = nullptr;
    const char* output_path = nullptr;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--raw") == 0) {
            raw_mode = true;
        } else if (!input_path) {
            input_path = argv[i];
        } else if (!output_path) {
            output_path = argv[i];
        }
    }

    if (!input_path) {
        fprintf(stderr, "Error: no input path\n");
        return 1;
    }

    auto t0 = std::chrono::high_resolution_clock::now();

    SaveParserCpp::ParseResult result;
    try {
        if (raw_mode) {
            result = SaveParserCpp::ParseRawFile(input_path);
        } else {
            result = SaveParserCpp::ParseFile(input_path);
        }
    } catch (const std::exception& e) {
        fprintf(stderr, "Parse error: %s\n", e.what());
        return 1;
    }

    auto t1 = std::chrono::high_resolution_clock::now();
    double parse_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();

    fprintf(stderr, "Parsed: %s\n", input_path);
    fprintf(stderr, "  Kind: %s\n", result.input_kind.c_str());
    fprintf(stderr, "  Raw size: %u bytes\n", result.raw_size);
    fprintf(stderr, "  Schema: %u types\n", (unsigned)result.schema.types.size());
    fprintf(stderr, "  TOC: %u entries\n", (unsigned)result.toc.entries.size());
    fprintf(stderr, "  Objects: %u\n", (unsigned)result.objects.size());
    fprintf(stderr, "  Parse time: %.1f ms\n", parse_ms);

    auto t2 = std::chrono::high_resolution_clock::now();
    std::string json = SaveParserCpp::ToJson(result);
    auto t3 = std::chrono::high_resolution_clock::now();
    double json_ms = std::chrono::duration<double, std::milli>(t3 - t2).count();

    fprintf(stderr, "  JSON size: %zu bytes\n", json.size());
    fprintf(stderr, "  JSON time: %.1f ms\n", json_ms);

    if (!output_path || strcmp(output_path, "-") == 0) {
        fwrite(json.data(), 1, json.size(), stdout);
    } else {
        std::ofstream out(output_path, std::ios::binary);
        if (!out) {
            fprintf(stderr, "Error: cannot open %s for writing\n", output_path);
            return 1;
        }
        out.write(json.data(), json.size());
        fprintf(stderr, "  Written to: %s\n", output_path);
    }

    fprintf(stderr, "  Total: %.1f ms\n",
        std::chrono::duration<double, std::milli>(
            std::chrono::high_resolution_clock::now() - t0).count());

    return 0;
}
