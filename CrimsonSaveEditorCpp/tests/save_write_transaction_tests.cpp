#define NOMINMAX
#include <windows.h>
#include "save_writer.h"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>

int main(int argc, char** argv) {
    if (argc != 3) return 2;
    const std::string scenario = argv[1];
    const std::filesystem::path directory = scenario == "long_name"
        ? "\\\\?\\" + std::filesystem::absolute(argv[2]).string() : argv[2];
    const auto destination = directory / (scenario == "long_name" ? std::string(230, 'a') : "original.save");
    HANDLE lock = INVALID_HANDLE_VALUE;
    try {
        std::filesystem::create_directory(directory);
        {
            std::ofstream out(destination, std::ios::binary);
            out << "original";
            if (!out) throw std::logic_error("Cannot create original test fixture");
        }
        // Direct truncation is allowed; atomic replacement is deliberately denied.
        if (scenario != "validation" && scenario != "long_name") {
            lock = CreateFileW(destination.c_str(), GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
            if (lock == INVALID_HANDLE_VALUE) throw std::runtime_error("Cannot lock test destination");
        }
        bool rejected = false;
        try {
            if (scenario == "raw") SaveWriter::WriteRawFile(destination.string(), {1, 2, 3});
            else if (scenario == "validation") {
                SaveWriter::WriteSaveFile(destination.string(), {1, 2, 3}, {},
                    [](const std::string& candidate) {
                        std::ifstream file(candidate, std::ios::binary);
                        if (!file || file.get() != 'S') throw std::logic_error("Candidate could not be reopened");
                        throw std::runtime_error("Simulated candidate validation failure");
                    });
            }
            else SaveWriter::WriteSaveFile(destination.string(), {1, 2, 3}, {});
        } catch (const std::runtime_error&) { rejected = true; }
        if (lock != INVALID_HANDLE_VALUE) CloseHandle(lock);
        lock = INVALID_HANDLE_VALUE;
        std::ifstream input(destination, std::ios::binary);
        const std::string contents((std::istreambuf_iterator<char>(input)), {});
        input.close();
        if (scenario == "long_name" ? (rejected || contents.substr(0, 4) != "SAVE") : (!rejected || contents != "original"))
            throw std::runtime_error("Write did not reject replacement and preserve original bytes");
        if (std::distance(std::filesystem::directory_iterator(directory), {}) != 1)
            throw std::runtime_error("Leaked candidate file");
        std::filesystem::remove(destination);
        std::filesystem::remove(directory);
        return 0;
    } catch (const std::exception& error) {
        if (lock != INVALID_HANDLE_VALUE) CloseHandle(lock);
        std::cerr << error.what() << '\n';
        // Keep failed test artifacts for diagnosis.
        return 1;
    }
}
