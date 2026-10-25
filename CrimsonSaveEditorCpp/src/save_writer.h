/**
 * save_writer.h — Save file write-back: LZ4 compress + HMAC + ChaCha20 encrypt + write.
 */
#pragma once
#include <vector>
#include <string>
#include <cstdint>

namespace SaveWriter {

std::vector<uint8_t> GenerateSaveKey(uint16_t version);
std::vector<uint8_t> Lz4HCCompress(const std::vector<uint8_t>& raw);
std::vector<uint8_t> ComputeHmac(const std::vector<uint8_t>& key, const std::vector<uint8_t>& data);
std::vector<uint8_t> ChaCha20Crypt(const std::vector<uint8_t>& key, const std::vector<uint8_t>& nonce, const std::vector<uint8_t>& data);
std::vector<uint8_t> RandomBytes(size_t count);

void WriteSaveFile(const std::string& path,
                   const std::vector<uint8_t>& raw_blob,
                   const std::vector<uint8_t>& original_header);

void WriteRawFile(const std::string& path,
                  const std::vector<uint8_t>& raw_blob);

} // namespace SaveWriter
