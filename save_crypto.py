from __future__ import annotations

import hashlib
import hmac
import os
import struct
from typing import Tuple

import lz4.block

from models import SaveData


_SAVE_BASE_KEY = bytes.fromhex(
    "C41B8E730DF259A637CC04E9B12F9668DA107A853E61F9224DB80AD75C13EF90"
)[:31]

_VERSION_PREFIXES = {
    1: b'^Qgbrm/.#@`zsr]\\@rvfal#"',
    2: b"^Pearl--#Abyss__@!!",
}


def _generate_save_key(version: int) -> bytes:
    prefix = _VERSION_PREFIXES.get(version)
    if prefix is None:
        raise ValueError(f"Unsupported save version {version}")
    material = prefix + b"PRIVATE_HMAC_SECRET_CHECK"
    return bytes(x ^ y for x, y in zip(_SAVE_BASE_KEY, material)) + b"\x00"


KEY = _generate_save_key(2)

HEADER_SIZE = 0x80
MAGIC_OFFSET = 0x00
VERSION_OFFSET = 0x04
FLAGS_OFFSET = 0x06
UNCOMP_SIZE_OFFSET = 0x12
PAYLOAD_SIZE_OFFSET = 0x16
NONCE_OFFSET = 0x1A
HMAC_OFFSET = 0x2A
PAYLOAD_OFFSET = 0x80


def _rotl32(v: int, n: int) -> int:
    v &= 0xFFFFFFFF
    return ((v << n) | (v >> (32 - n))) & 0xFFFFFFFF


def _quarter_round(s: list, a: int, b: int, c: int, d: int) -> None:
    s[a] = (s[a] + s[b]) & 0xFFFFFFFF
    s[d] ^= s[a]
    s[d] = _rotl32(s[d], 16)

    s[c] = (s[c] + s[d]) & 0xFFFFFFFF
    s[b] ^= s[c]
    s[b] = _rotl32(s[b], 12)

    s[a] = (s[a] + s[b]) & 0xFFFFFFFF
    s[d] ^= s[a]
    s[d] = _rotl32(s[d], 8)

    s[c] = (s[c] + s[d]) & 0xFFFFFFFF
    s[b] ^= s[c]
    s[b] = _rotl32(s[b], 7)


def _chacha20_block(key_words: list, counter: int, nonce_words: list) -> bytes:
    s = [
        0x61707865, 0x3320646e, 0x79622d32, 0x6b206574,
        key_words[0], key_words[1], key_words[2], key_words[3],
        key_words[4], key_words[5], key_words[6], key_words[7],
        counter & 0xFFFFFFFF,
        nonce_words[0], nonce_words[1], nonce_words[2],
    ]

    w = list(s)

    for _ in range(10):
        _quarter_round(w, 0, 4, 8, 12)
        _quarter_round(w, 1, 5, 9, 13)
        _quarter_round(w, 2, 6, 10, 14)
        _quarter_round(w, 3, 7, 11, 15)
        _quarter_round(w, 0, 5, 10, 15)
        _quarter_round(w, 1, 6, 11, 12)
        _quarter_round(w, 2, 7, 8, 13)
        _quarter_round(w, 3, 4, 9, 14)

    result = bytearray(64)
    for i in range(16):
        v = (w[i] + s[i]) & 0xFFFFFFFF
        struct.pack_into("<I", result, i * 4, v)

    return bytes(result)


def chacha20_crypt(data: bytes, nonce16: bytes, key: bytes = None) -> bytes:
    if key is None:
        key = KEY
    init_counter = struct.unpack_from("<I", nonce16, 0)[0]
    nonce12 = nonce16[4:16]

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
        algo = algorithms.ChaCha20(key, nonce16)
        cipher = Cipher(algo, mode=None)
        enc = cipher.encryptor()
        return enc.update(data) + enc.finalize()
    except Exception:
        pass

    key_words = [struct.unpack_from("<I", key, i * 4)[0] for i in range(8)]
    nonce_words = [struct.unpack_from("<I", nonce12, i * 4)[0] for i in range(3)]

    output = bytearray(len(data))
    pos = 0
    counter = init_counter

    while pos < len(data):
        block = _chacha20_block(key_words, counter, nonce_words)
        end = min(pos + 64, len(data))
        for i in range(pos, end):
            output[i] = data[i] ^ block[i - pos]
        pos = end
        counter = (counter + 1) & 0xFFFFFFFF

    return bytes(output)


def compute_hmac(data: bytes, key: bytes = None) -> bytes:
    if key is None:
        key = KEY
    return hmac.new(key, data, hashlib.sha256).digest()


def verify_hmac(data: bytes, expected: bytes, key: bytes = None) -> bool:
    return hmac.compare_digest(compute_hmac(data, key), expected)


def load_save_file(path: str) -> SaveData:
    with open(path, "rb") as f:
        file_data = f.read()

    if len(file_data) < HEADER_SIZE + 16:
        raise ValueError("File too small to be a save file.")

    magic = file_data[MAGIC_OFFSET:MAGIC_OFFSET + 4]
    if magic != b"SAVE":
        raise ValueError(f"Bad magic: expected 'SAVE', got {magic!r}")

    version = struct.unpack_from("<H", file_data, VERSION_OFFSET)[0]
    uncomp_size = struct.unpack_from("<I", file_data, UNCOMP_SIZE_OFFSET)[0]
    payload_size = struct.unpack_from("<I", file_data, PAYLOAD_SIZE_OFFSET)[0]
    nonce = file_data[NONCE_OFFSET:NONCE_OFFSET + 16]
    stored_hmac = file_data[HMAC_OFFSET:HMAC_OFFSET + 32]

    if PAYLOAD_OFFSET + payload_size > len(file_data):
        raise ValueError(
            f"Payload size {payload_size} exceeds file size {len(file_data)}"
        )

    key = _generate_save_key(version)

    ciphertext = file_data[PAYLOAD_OFFSET:PAYLOAD_OFFSET + payload_size]

    compressed = chacha20_crypt(ciphertext, nonce, key)

    hmac_ok = verify_hmac(compressed, stored_hmac, key)

    decompressed = lz4.block.decompress(
        compressed, uncompressed_size=uncomp_size
    )

    if len(decompressed) != uncomp_size:
        raise ValueError(
            f"LZ4 decompressed {len(decompressed)} bytes, expected {uncomp_size}"
        )

    header = file_data[:HEADER_SIZE]

    save_data = SaveData(
        raw_header=header,
        decompressed_blob=bytearray(decompressed),
        original_compressed_size=payload_size,
        original_decompressed_size=uncomp_size,
        file_path=path,
        is_raw_stream=False,
    )

    if not hmac_ok:
        raise Warning("HMAC mismatch - save may be corrupted but was loaded anyway.")

    return save_data


def load_raw_stream(path: str) -> SaveData:
    with open(path, "rb") as f:
        blob = f.read()
    return SaveData(
        raw_header=b"",
        decompressed_blob=bytearray(blob),
        original_compressed_size=0,
        original_decompressed_size=len(blob),
        file_path=path,
        is_raw_stream=True,
    )


def write_save_file(
    path: str,
    edited_blob: bytes,
    original_header: bytes | None = None,
) -> None:
    version = 2
    if original_header and len(original_header) >= 6:
        version = struct.unpack_from("<H", original_header, VERSION_OFFSET)[0]
    key = _generate_save_key(version)

    compressed = lz4.block.compress(
        bytes(edited_blob),
        store_size=False,
        mode="high_compression",
        compression=9,
    )

    nonce = os.urandom(16)

    hmac_digest = compute_hmac(compressed, key)

    encrypted = chacha20_crypt(compressed, nonce, key)

    header = bytearray(HEADER_SIZE)

    if original_header and len(original_header) >= 0x12:
        header[:0x12] = original_header[:0x12]

    header[0:4] = b"SAVE"
    struct.pack_into("<H", header, VERSION_OFFSET, 2)
    struct.pack_into("<H", header, FLAGS_OFFSET, 0x0080)

    struct.pack_into("<I", header, UNCOMP_SIZE_OFFSET, len(edited_blob))
    struct.pack_into("<I", header, PAYLOAD_SIZE_OFFSET, len(compressed))

    header[NONCE_OFFSET:NONCE_OFFSET + 16] = nonce
    header[HMAC_OFFSET:HMAC_OFFSET + 32] = hmac_digest

    with open(path, "wb") as f:
        f.write(bytes(header))
        f.write(encrypted)
