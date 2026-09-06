/**
 * portable_hmac_sha256.h — self-contained SHA-256 + HMAC-SHA256.
 *
 * Used by save_parser_cpp.cpp and save_writer.cpp on non-Windows builds
 * (wasm/Emscripten) where BCrypt is unavailable. Pure C++, no deps.
 * FIPS 180-4 SHA-256; RFC 2104 HMAC.
 */
#pragma once
#include <cstdint>
#include <cstring>
#include <vector>

namespace PortableCrypto {

struct Sha256Ctx {
    uint32_t state[8];
    uint64_t bitlen;
    uint8_t buf[64];
    size_t buflen;
};

inline void Sha256Init(Sha256Ctx& c) {
    static const uint32_t iv[8] = {
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(c.state, iv, sizeof(iv));
    c.bitlen = 0;
    c.buflen = 0;
}

inline void Sha256Block(Sha256Ctx& c, const uint8_t* p) {
    static const uint32_t K[64] = {
        0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
        0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
        0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
        0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
        0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
        0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
        0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
        0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
    auto rotr = [](uint32_t x, int n) { return (x >> n) | (x << (32 - n)); };
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
        w[i] = (uint32_t)p[i*4] << 24 | (uint32_t)p[i*4+1] << 16 |
               (uint32_t)p[i*4+2] << 8 | (uint32_t)p[i*4+3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = rotr(w[i-15], 7) ^ rotr(w[i-15], 18) ^ (w[i-15] >> 3);
        uint32_t s1 = rotr(w[i-2], 17) ^ rotr(w[i-2], 19) ^ (w[i-2] >> 10);
        w[i] = w[i-16] + s0 + w[i-7] + s1;
    }
    uint32_t a = c.state[0], b = c.state[1], cc = c.state[2], d = c.state[3];
    uint32_t e = c.state[4], f = c.state[5], g = c.state[6], h = c.state[7];
    for (int i = 0; i < 64; i++) {
        uint32_t S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t t1 = h + S1 + ch + K[i] + w[i];
        uint32_t S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        uint32_t maj = (a & b) ^ (a & cc) ^ (b & cc);
        uint32_t t2 = S0 + maj;
        h = g; g = f; f = e; e = d + t1;
        d = cc; cc = b; b = a; a = t1 + t2;
    }
    c.state[0] += a; c.state[1] += b; c.state[2] += cc; c.state[3] += d;
    c.state[4] += e; c.state[5] += f; c.state[6] += g; c.state[7] += h;
}

inline void Sha256Update(Sha256Ctx& c, const uint8_t* data, size_t len) {
    c.bitlen += (uint64_t)len * 8;
    while (len > 0) {
        size_t take = 64 - c.buflen;
        if (take > len) take = len;
        memcpy(c.buf + c.buflen, data, take);
        c.buflen += take;
        data += take;
        len -= take;
        if (c.buflen == 64) {
            Sha256Block(c, c.buf);
            c.buflen = 0;
        }
    }
}

inline void Sha256Final(Sha256Ctx& c, uint8_t out[32]) {
    uint64_t bitlen = c.bitlen;
    uint8_t pad = 0x80;
    Sha256Update(c, &pad, 1);
    c.bitlen -= 8; // padding doesn't count
    uint8_t zero = 0;
    while (c.buflen != 56) {
        Sha256Update(c, &zero, 1);
        c.bitlen -= 8;
    }
    uint8_t lenbe[8];
    for (int i = 0; i < 8; i++) lenbe[i] = (uint8_t)(bitlen >> (56 - i * 8));
    Sha256Update(c, lenbe, 8);
    for (int i = 0; i < 8; i++) {
        out[i*4]   = (uint8_t)(c.state[i] >> 24);
        out[i*4+1] = (uint8_t)(c.state[i] >> 16);
        out[i*4+2] = (uint8_t)(c.state[i] >> 8);
        out[i*4+3] = (uint8_t)(c.state[i]);
    }
}

inline std::vector<uint8_t> HmacSha256(const std::vector<uint8_t>& key,
                                       const std::vector<uint8_t>& data) {
    uint8_t k[64] = {};
    if (key.size() > 64) {
        Sha256Ctx kc;
        Sha256Init(kc);
        Sha256Update(kc, key.data(), key.size());
        Sha256Final(kc, k);
    } else {
        memcpy(k, key.data(), key.size());
    }
    uint8_t ipad[64], opad[64];
    for (int i = 0; i < 64; i++) {
        ipad[i] = k[i] ^ 0x36;
        opad[i] = k[i] ^ 0x5C;
    }
    uint8_t inner[32];
    Sha256Ctx c;
    Sha256Init(c);
    Sha256Update(c, ipad, 64);
    if (!data.empty()) Sha256Update(c, data.data(), data.size());
    Sha256Final(c, inner);

    std::vector<uint8_t> digest(32);
    Sha256Init(c);
    Sha256Update(c, opad, 64);
    Sha256Update(c, inner, 32);
    Sha256Final(c, digest.data());
    return digest;
}

} // namespace PortableCrypto
