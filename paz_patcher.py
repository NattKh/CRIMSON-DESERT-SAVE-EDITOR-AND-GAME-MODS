from __future__ import annotations

import io
import logging
import os
import shutil
import string
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import lz4.block
    HAS_LZ4 = True
except ImportError:
    HAS_LZ4 = False

log = logging.getLogger(__name__)


_M = 0xFFFFFFFF
_CHECKSUM_TRIPLE = struct.Struct("<III")


def _rot(x: int, k: int) -> int:
    return ((x << k) | (x >> (32 - k))) & _M


def _mix(a: int, b: int, c: int) -> tuple:
    a = ((a - c) ^ _rot(c, 4)) & _M;  c = (c + b) & _M
    b = ((b - a) ^ _rot(a, 6)) & _M;  a = (a + c) & _M
    c = ((c - b) ^ _rot(b, 8)) & _M;  b = (b + a) & _M
    a = ((a - c) ^ _rot(c, 16)) & _M; c = (c + b) & _M
    b = ((b - a) ^ _rot(a, 19)) & _M; a = (a + c) & _M
    c = ((c - b) ^ _rot(b, 4)) & _M;  b = (b + a) & _M
    return a, b, c


def _final(a: int, b: int, c: int) -> tuple:
    c = ((c ^ b) - _rot(b, 14)) & _M
    a = ((a ^ c) - _rot(c, 11)) & _M
    b = ((b ^ a) - _rot(a, 25)) & _M
    c = ((c ^ b) - _rot(b, 16)) & _M
    a = ((a ^ c) - _rot(c, 4)) & _M
    b = ((b ^ a) - _rot(a, 14)) & _M
    c = ((c ^ b) - _rot(b, 24)) & _M
    return a, b, c


def pa_checksum(data: bytes) -> int:
    length = len(data)
    if length == 0:
        return 0

    a = b = c = (length + 0xDEBA1DCD) & _M

    offset = 0
    remaining = length

    while remaining > 12:
        w0, w1, w2 = _CHECKSUM_TRIPLE.unpack_from(data, offset)
        a = (a + w0) & _M
        b = (b + w1) & _M
        c = (c + w2) & _M
        a, b, c = _mix(a, b, c)
        offset += 12
        remaining -= 12

    if remaining == 0:
        return c

    tail = data[offset:] + b"\x00" * (12 - remaining)
    w0, w1, w2 = struct.unpack("<III", tail)
    a = (a + w0) & _M
    b = (b + w1) & _M
    c = (c + w2) & _M

    _, _, c = _final(a, b, c)
    return c

SCAN_CHUNK_SIZE = 4 * 1024 * 1024


@dataclass
class PatchEntry:
    offset_from_sig: int
    old_bytes: bytes
    new_bytes: bytes


@dataclass
class PazPatch:
    name: str
    description: str
    paz_file: str
    signature: bytes
    patches: List[PatchEntry]
    vanilla_description: str = ""


@dataclass
class PatchStatus:
    name: str
    status: str
    detail: str = ""


VEHICLE_NAMES = [
    "BearWarMachine", "CarmabirdSaurusWarMachine", "WarMachine", "Dragon",
    "Horse", "GolemHorse", "Camel", "AlpineIbex", "1Horse4Wheels",
    "1Horse2Wheels", "2Horse4Wheels", "PlayerWagon", "Airballoon_I",
    "Airballoon_II", "Airballoon_III", "Cannon", "Boar", "Ballista",
    "Crossbow", "Canoe", "Boat", "Wolf", "Singijeon", "Wyvern", "Bear",
    "ReinDeer", "CuCubird", "Elephant", "Iguana", "CarmabirdSaurus",
    "Bull", "Train2",
]

VEHICLE_COOLDOWN_OFFSETS = {
    "BearWarMachine": 0x0000C6,
    "CarmabirdSaurusWarMachine": 0x0001A1,
    "WarMachine": 0x00026D,
    "Dragon": 0x000335,
    "Horse": 0x0003FD,
    "GolemHorse": 0x0004C9,
    "Camel": 0x000590,
    "AlpineIbex": 0x00065C,
    "1Horse4Wheels": 0x00072B,
    "1Horse2Wheels": 0x0007FA,
    "2Horse4Wheels": 0x0008C9,
    "PlayerWagon": 0x000996,
    "Airballoon_I": 0x000A64,
    "Airballoon_II": 0x000B33,
    "Airballoon_III": 0x000C03,
    "Cannon": 0x000CCB,
    "Boar": 0x000D91,
    "Ballista": 0x000E5B,
    "Crossbow": 0x000F25,
    "Canoe": 0x000FEC,
    "Boat": 0x0010B2,
    "Wolf": 0x001178,
    "Singijeon": 0x001243,
    "Wyvern": 0x00130B,
    "Bear": 0x0013D1,
    "ReinDeer": 0x00149B,
    "CuCubird": 0x001565,
    "Elephant": 0x00162F,
    "Iguana": 0x0016F7,
    "CarmabirdSaurus": 0x0017C8,
    "Bull": 0x00188E,
    "Train2": 0x001956,
}


INVENTORY_SIGNATURE = bytes([
    0x02, 0x00, 0x09, 0x00, 0x00, 0x00,
    0x43, 0x68, 0x61, 0x72, 0x61, 0x63, 0x74, 0x65, 0x72,
    0x00, 0x01,
])


def _build_inventory_patch() -> PazPatch:
    return PazPatch(
        name="Max Inventory Slots",
        description="Sets starting inventory to 200 slots (vanilla: 50). Max stays at 240.",
        paz_file="0008/0.paz",
        signature=INVENTORY_SIGNATURE,
        patches=[
            PatchEntry(
                offset_from_sig=17,
                old_bytes=struct.pack("<H", 50),
                new_bytes=struct.pack("<H", 200),
            ),
            PatchEntry(
                offset_from_sig=19,
                old_bytes=struct.pack("<H", 240),
                new_bytes=struct.pack("<H", 240),
            ),
        ],
        vanilla_description="Default 50, Max 240",
    )


def _build_mount_cooldown_patch() -> PazPatch:
    anchor_name = "BearWarMachine"
    anchor_sig = anchor_name.encode("ascii") + b"\x00"
    anchor_pabgb_offset = VEHICLE_COOLDOWN_OFFSETS[anchor_name]


    patches = []
    for vname, cd_offset in VEHICLE_COOLDOWN_OFFSETS.items():
        relative_offset = cd_offset - 6

        patches.append(PatchEntry(
            offset_from_sig=relative_offset,
            old_bytes=b"",
            new_bytes=struct.pack("<H", 0),
        ))

    return PazPatch(
        name="Mount Death Respawn (1s)",
        description="Sets all 32 mount/vehicle DEATH RESPAWN timers to 1 second (vanilla: ~85-92 min). Does NOT affect Dragon summon duration/cooldown (hardcoded).",
        paz_file="0008/0.paz",
        signature=anchor_sig,
        patches=patches,
        vanilla_description="Cooldowns range from 5026-5504 seconds (~84-92 minutes)",
    )


def get_all_patches() -> List[PazPatch]:
    return [
        _build_mount_cooldown_patch(),
    ]


class PazPatchManager:

    def __init__(self, game_path: str = ""):
        self.game_path = game_path


    @staticmethod
    def find_game_path() -> str:
        candidates = []

        for letter in string.ascii_uppercase:
            candidates.append(
                f"{letter}:\\SteamLibrary\\steamapps\\common\\Crimson Desert"
            )

        candidates.extend([
            r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert",
            r"C:\Program Files\Steam\steamapps\common\Crimson Desert",
        ])

        candidates.append(r"C:\Program Files\Epic Games\CrimsonDesert")

        for path in candidates:
            paz = os.path.join(path, "0008", "0.paz")
            if os.path.isfile(paz):
                return path

        return ""

    def get_paz_path(self, relative: str) -> str:
        return os.path.join(self.game_path, relative.replace("/", os.sep))

    def get_backup_path(self, paz_path: str) -> str:
        return paz_path + ".backup"


    @staticmethod
    def scan_for_signature(paz_path: str, signature: bytes) -> int:
        sig_len = len(signature)
        overlap = sig_len - 1

        with open(paz_path, "rb") as f:
            file_offset = 0
            carry = b""

            while True:
                chunk = f.read(SCAN_CHUNK_SIZE)
                if not chunk:
                    break

                search_buf = carry + chunk
                pos = search_buf.find(signature)

                if pos >= 0:
                    return file_offset - len(carry) + pos

                carry = search_buf[-overlap:] if len(search_buf) > overlap else search_buf
                file_offset += len(chunk)

        return -1


    def check_status(self, patch: PazPatch) -> PatchStatus:
        paz_path = self.get_paz_path(patch.paz_file)

        if not os.path.isfile(paz_path):
            return PatchStatus(patch.name, "File Not Found",
                               f"Cannot find {paz_path}")

        sig_offset = self.scan_for_signature(paz_path, patch.signature)
        if sig_offset < 0:
            return PatchStatus(patch.name, "Signature Not Found",
                               "Game version may not be supported")

        applied_count = 0
        vanilla_count = 0
        total_checkable = 0

        with open(paz_path, "rb") as f:
            for pe in patch.patches:
                if len(pe.new_bytes) == 0:
                    continue

                abs_offset = sig_offset + pe.offset_from_sig
                f.seek(abs_offset)
                current = f.read(len(pe.new_bytes))

                if current == pe.new_bytes:
                    applied_count += 1
                elif pe.old_bytes and current == pe.old_bytes:
                    vanilla_count += 1

                total_checkable += 1

        if total_checkable == 0:
            return PatchStatus(patch.name, "Unknown", "No checkable patch entries")

        if applied_count == total_checkable:
            return PatchStatus(patch.name, "Applied",
                               f"All {total_checkable} patch points verified")
        elif applied_count == 0:
            return PatchStatus(patch.name, "Not Applied",
                               f"0/{total_checkable} patches active")
        else:
            return PatchStatus(patch.name, "Partial",
                               f"{applied_count}/{total_checkable} patches active")

    def check_mount_cooldown_status(self, patch: PazPatch) -> PatchStatus:
        paz_path = self.get_paz_path(patch.paz_file)

        if not os.path.isfile(paz_path):
            return PatchStatus(patch.name, "File Not Found",
                               f"Cannot find {paz_path}")

        sig_offset = self.scan_for_signature(paz_path, patch.signature)
        if sig_offset < 0:
            return PatchStatus(patch.name, "Signature Not Found",
                               "Game version may not be supported")

        zeroed = 0
        total = 0

        with open(paz_path, "rb") as f:
            for pe in patch.patches:
                abs_offset = sig_offset + pe.offset_from_sig
                f.seek(abs_offset)
                current = f.read(2)
                if len(current) == 2:
                    val = struct.unpack("<H", current)[0]
                    total += 1
                    if val == 0:
                        zeroed += 1

        if total == 0:
            return PatchStatus(patch.name, "Unknown", "Could not read cooldown values")

        if zeroed == total:
            return PatchStatus(patch.name, "Applied",
                               f"All {total} cooldowns are 0")
        elif zeroed == 0:
            return PatchStatus(patch.name, "Not Applied",
                               f"All {total} cooldowns have vanilla values")
        else:
            return PatchStatus(patch.name, "Partial",
                               f"{zeroed}/{total} cooldowns zeroed")

    def check_inventory_status(self, patch: PazPatch) -> PatchStatus:
        paz_path = self.get_paz_path(patch.paz_file)

        if not os.path.isfile(paz_path):
            return PatchStatus(patch.name, "File Not Found",
                               f"Cannot find {paz_path}")

        sig_offset = self.scan_for_signature(paz_path, patch.signature)
        if sig_offset < 0:
            return PatchStatus(patch.name, "Signature Not Found",
                               "Game version may not be supported")

        with open(paz_path, "rb") as f:
            f.seek(sig_offset + 17)
            data = f.read(4)
            if len(data) < 4:
                return PatchStatus(patch.name, "Unknown", "Could not read slot values")

            default_slots = struct.unpack_from("<H", data, 0)[0]
            max_slots = struct.unpack_from("<H", data, 2)[0]

        if default_slots == 200:
            return PatchStatus(patch.name, "Applied",
                               f"Default={default_slots}, Max={max_slots}")
        elif default_slots == 50:
            return PatchStatus(patch.name, "Not Applied",
                               f"Default={default_slots}, Max={max_slots} (vanilla)")
        else:
            return PatchStatus(patch.name, "Partial",
                               f"Default={default_slots}, Max={max_slots} (custom)")

    def get_detailed_status(self, patch: PazPatch) -> PatchStatus:
        if patch.name == "Mount Death Respawn (1s)":
            return self.check_mount_cooldown_status(patch)
        elif patch.name == "Max Inventory Slots":
            return self.check_inventory_status(patch)
        else:
            return self.check_status(patch)


    def _get_pamt_path(self, paz_file: str) -> str:
        folder = os.path.dirname(paz_file)
        return os.path.join(self.game_path, folder, "0.pamt")

    def _get_papgt_path(self) -> str:
        return os.path.join(self.game_path, "meta", "0.papgt")

    def _get_related_paths(self, paz_file: str) -> List[str]:
        paz_path = self.get_paz_path(paz_file)
        pamt_path = self._get_pamt_path(paz_file)
        papgt_path = self._get_papgt_path()
        paths = [paz_path]
        if os.path.isfile(pamt_path):
            paths.append(pamt_path)
        if os.path.isfile(papgt_path):
            paths.append(papgt_path)
        return paths


    def _backup_single_file(self, file_path: str) -> Tuple[bool, str]:
        backup_path = self.get_backup_path(file_path)

        if os.path.isfile(backup_path):
            return True, f"Backup already exists: {backup_path}"

        if not os.path.isfile(file_path):
            return True, f"File does not exist (skip): {file_path}"

        try:
            log.info("Creating backup: %s -> %s", file_path, backup_path)
            shutil.copy2(file_path, backup_path)
            return True, f"Backup created: {backup_path}"
        except Exception as e:
            return False, f"Failed to create backup of {file_path}: {e}"

    def create_backup(self, paz_path: str) -> Tuple[bool, str]:
        messages = []
        ok, msg = self._backup_single_file(paz_path)
        if not ok:
            return False, msg
        messages.append(msg)

        paz_dir = os.path.dirname(paz_path)
        pamt_path = os.path.join(paz_dir, "0.pamt")
        ok, msg = self._backup_single_file(pamt_path)
        if not ok:
            log.warning("PAMT backup failed: %s", msg)
        else:
            messages.append(msg)

        papgt_path = self._get_papgt_path()
        ok, msg = self._backup_single_file(papgt_path)
        if not ok:
            log.warning("PAPGT backup failed: %s", msg)
        else:
            messages.append(msg)

        return True, "; ".join(messages)

    def _restore_single_file(self, file_path: str) -> Tuple[bool, str]:
        backup_path = self.get_backup_path(file_path)
        if not os.path.isfile(backup_path):
            return False, f"No backup found at {backup_path}"
        try:
            shutil.copy2(backup_path, file_path)
            return True, f"Restored: {file_path}"
        except Exception as e:
            return False, f"Failed to restore {file_path}: {e}"

    def restore_backup(self, patch: PazPatch) -> Tuple[bool, str]:
        paz_path = self.get_paz_path(patch.paz_file)
        restored = []
        errors = []

        ok, msg = self._restore_single_file(paz_path)
        if ok:
            restored.append(msg)
        else:
            errors.append(msg)

        pamt_path = self._get_pamt_path(patch.paz_file)
        ok, msg = self._restore_single_file(pamt_path)
        if ok:
            restored.append(msg)

        papgt_path = self._get_papgt_path()
        ok, msg = self._restore_single_file(papgt_path)
        if ok:
            restored.append(msg)

        if not restored and errors:
            return False, errors[0]

        self.update_papgt()

        detail = f"Restored {len(restored)} file(s)"
        if errors:
            detail += f" ({len(errors)} could not be restored)"
        return True, detail

    def restore_all_backups(self) -> Tuple[bool, str]:
        patches = get_all_patches()
        restored = []
        errors = []

        paz_files = set()
        for p in patches:
            paz_files.add(p.paz_file)

        for pf in paz_files:
            paz_path = self.get_paz_path(pf)
            backup_path = self.get_backup_path(paz_path)
            if os.path.isfile(backup_path):
                try:
                    shutil.copy2(backup_path, paz_path)
                    restored.append(pf)
                except Exception as e:
                    errors.append(f"{pf}: {e}")

            pamt_path = self._get_pamt_path(pf)
            pamt_backup = self.get_backup_path(pamt_path)
            if os.path.isfile(pamt_backup):
                try:
                    shutil.copy2(pamt_backup, pamt_path)
                except Exception as e:
                    errors.append(f"PAMT {pf}: {e}")

        papgt_path = self._get_papgt_path()
        papgt_backup = self.get_backup_path(papgt_path)
        if os.path.isfile(papgt_backup):
            try:
                shutil.copy2(papgt_backup, papgt_path)
            except Exception as e:
                errors.append(f"PAPGT: {e}")

        if errors:
            return False, f"Restored {len(restored)}, errors: {'; '.join(errors)}"
        elif restored:
            return True, f"Restored {len(restored)} file(s): {', '.join(restored)}"
        else:
            return False, "No backups found to restore"

    def has_backup(self, patch: PazPatch) -> bool:
        paz_path = self.get_paz_path(patch.paz_file)
        return os.path.isfile(self.get_backup_path(paz_path))


    def update_pamt_checksum(self, pamt_path: str) -> Tuple[bool, str]:
        if not os.path.isfile(pamt_path):
            return False, f"PAMT not found: {pamt_path}"

        try:
            with open(pamt_path, "r+b") as f:
                f.seek(0)
                header = f.read(12)
                if len(header) < 12:
                    return False, "PAMT file too small"

                old_checksum = struct.unpack_from("<I", header, 0)[0]

                f.seek(12)
                payload = f.read()
                new_checksum = pa_checksum(payload)

                if new_checksum != old_checksum:
                    f.seek(0)
                    f.write(struct.pack("<I", new_checksum))
                    log.info("PAMT checksum updated: 0x%08X -> 0x%08X in %s",
                             old_checksum, new_checksum, pamt_path)
                    return True, (
                        f"PAMT checksum updated: 0x{old_checksum:08X} -> "
                        f"0x{new_checksum:08X}"
                    )
                else:
                    return True, f"PAMT checksum unchanged: 0x{new_checksum:08X}"

        except Exception as e:
            return False, f"Failed to update PAMT checksum: {e}"

    def update_papgt(self) -> Tuple[bool, str]:
        papgt_path = self._get_papgt_path()

        if not os.path.isfile(papgt_path):
            return False, f"PAPGT not found: {papgt_path}"

        try:
            with open(papgt_path, "rb") as f:
                papgt = bytearray(f.read())

            if len(papgt) < 12:
                return False, "PAPGT file too small"

            entry_count = papgt[8]
            log.info("PAPGT has %d entries", entry_count)

            folders = sorted([
                d for d in os.listdir(self.game_path)
                if os.path.isdir(os.path.join(self.game_path, d))
                and d.isdigit()
            ])

            changed = False
            updated_entries = []

            for entry_index, folder_name in enumerate(folders):
                if entry_index >= entry_count:
                    break

                pamt_path = os.path.join(self.game_path, folder_name, "0.pamt")
                if not os.path.isfile(pamt_path):
                    continue

                with open(pamt_path, "rb") as f:
                    pamt_data = f.read()

                if len(pamt_data) <= 12:
                    continue

                real_crc = pa_checksum(pamt_data[12:])
                crc_offset = 12 + (entry_index * 12) + 8
                old_crc = struct.unpack_from("<I", papgt, crc_offset)[0]

                if old_crc != real_crc:
                    struct.pack_into("<I", papgt, crc_offset, real_crc)
                    changed = True
                    updated_entries.append(
                        f"{folder_name}: 0x{old_crc:08X} -> 0x{real_crc:08X}"
                    )
                    log.info("PAPGT entry %s: 0x%08X -> 0x%08X",
                             folder_name, old_crc, real_crc)

            old_papgt_checksum = struct.unpack_from("<I", papgt, 4)[0]
            new_papgt_checksum = pa_checksum(bytes(papgt[12:]))
            if new_papgt_checksum != old_papgt_checksum:
                struct.pack_into("<I", papgt, 4, new_papgt_checksum)
                changed = True
                log.info("PAPGT self-checksum: 0x%08X -> 0x%08X",
                         old_papgt_checksum, new_papgt_checksum)

            if changed:
                with open(papgt_path, "wb") as f:
                    f.write(papgt)

                detail = f"Updated {len(updated_entries)} PAMT CRC(s) in PAPGT"
                if updated_entries:
                    detail += ": " + ", ".join(updated_entries)
                detail += (
                    f". PAPGT checksum: 0x{new_papgt_checksum:08X}"
                )
                return True, detail
            else:
                return True, (
                    f"PAPGT already consistent "
                    f"(checksum: 0x{old_papgt_checksum:08X})"
                )

        except Exception as e:
            return False, f"Failed to update PAPGT: {e}"


    def apply_patch(self, patch: PazPatch) -> Tuple[bool, str]:
        paz_path = self.get_paz_path(patch.paz_file)

        if not os.path.isfile(paz_path):
            return False, f"PAZ file not found: {paz_path}"

        original_size = os.path.getsize(paz_path)

        sig_offset = self.scan_for_signature(paz_path, patch.signature)
        if sig_offset < 0:
            return False, (
                "Signature not found in PAZ file. "
                "The game may have been updated to an unsupported version."
            )

        ok, msg = self.create_backup(paz_path)
        if not ok:
            return False, f"Backup failed: {msg}"

        saved_bytes: list = []
        try:
            with open(paz_path, "rb") as f:
                for pe in patch.patches:
                    abs_offset = sig_offset + pe.offset_from_sig
                    if len(pe.new_bytes) == 0:
                        continue
                    f.seek(abs_offset)
                    original = f.read(len(pe.new_bytes))
                    saved_bytes.append((abs_offset, original))
        except Exception as e:
            return False, f"Failed to read original bytes: {e}"

        patched_count = 0
        try:
            with open(paz_path, "r+b") as f:
                for i, pe in enumerate(patch.patches):
                    abs_offset = sig_offset + pe.offset_from_sig
                    if len(pe.new_bytes) == 0:
                        continue
                    f.seek(abs_offset)
                    f.write(pe.new_bytes)
                    patched_count += 1
                f.flush()

        except Exception as e:
            log.error("Patch write failed, rolling back: %s", e)
            try:
                with open(paz_path, "r+b") as f:
                    for abs_offset, orig in saved_bytes:
                        f.seek(abs_offset)
                        f.write(orig)
                    f.flush()
            except Exception:
                pass
            return False, f"Patch failed after {patched_count} writes (ROLLED BACK): {e}"

        new_size = os.path.getsize(paz_path)
        if new_size != original_size:
            log.error("PAZ size changed! %d -> %d, rolling back", original_size, new_size)
            try:
                with open(paz_path, "r+b") as f:
                    for abs_offset, orig in saved_bytes:
                        f.seek(abs_offset)
                        f.write(orig)
                    f.flush()
                    f.truncate(original_size)
            except Exception:
                pass
            return False, (
                f"SAFETY ROLLBACK: File size changed from {original_size:,} to {new_size:,} bytes. "
                f"Original bytes restored. This should not happen with in-place patches."
            )

        status = self.get_detailed_status(patch)

        result_msg = f"Applied {patched_count} patch(es) successfully.\n"
        result_msg += f"Status: {status.status} - {status.detail}\n"
        result_msg += f"File size verified: {new_size:,} bytes (unchanged)"

        return True, result_msg

    def verify_signatures(self) -> List[PatchStatus]:
        results = []
        for patch in get_all_patches():
            paz_path = self.get_paz_path(patch.paz_file)
            if not os.path.isfile(paz_path):
                results.append(PatchStatus(
                    patch.name, "File Not Found", f"Cannot find {paz_path}"
                ))
                continue

            sig_offset = self.scan_for_signature(paz_path, patch.signature)
            if sig_offset >= 0:
                results.append(PatchStatus(
                    patch.name, "OK",
                    f"Signature found at offset 0x{sig_offset:X}"
                ))
            else:
                results.append(PatchStatus(
                    patch.name, "Not Found",
                    "Signature not found - game version may not be supported"
                ))

        return results


BUFF_HASHES: Dict[str, int] = {
    "Max HP (Hp)":                          0x000F4240,
    "Damage Dealt (DDD)":                   0x000F4242,
    "Defense (DPV)":                        0x000F4243,
    "Invincible":                           0x00011552,
    "Critical Damage (CriticalDamage)":     0x000F4246,
    "Damage Taken Rate (AttackedDmgRate)":  0x000F4248,
    "Damage Reduction (AttackedDmgReduc)":  0x000F4249,
    "Critical Rate (CriticalRate)":         0x000F4247,
    "Attack Speed (AttackSpeedRate)":       0x000F424A,
    "Move Speed (MoveSpeedRate)":           0x000F424B,
    "Climb Speed (ClimbSpeedRate)":         0x000F42A2,
    "Swim Speed (SwimSpeedRate)":           0x000F42A6,
    "Stamina Regen (StaminaRegen)":         0x000F42A3,
    "HP Regen (HpRegen)":                   0x000F42A4,
    "Spirit Regen (MpRegen)":               0x000F42A5,
    "Fire Resistance (FireResistance)":     0x000F42A7,
    "Ice Resistance (IceResistance)":       0x000F42A8,
    "Lightning Resistance (ElectricRes)":   0x000F42C7,
    "Guard Efficiency (GuardPVRate)":       0x000F42A0,
    "Craft Material Save (ReduceCraft)":    0x000F429B,
    "Bonus Ore Drop (MoreOreDrop)":         0x000F4287,
    "Bonus Lumber Drop (MoreLumberDrop)":   0x000F4288,
    "Disarm on Hit (EquipDropRate)":        0x000F4282,
    "Silver Drop Rate (MoneyDropRate)":     0x000F42B5,
    "Bonus Mining Drop (CollectDrop_Ore)":  0x000F42DC,
    "Bonus Gather Drop (CollectDrop_Plant)": 0x000F4289,
    "Bonus Skinning Drop (CollectDrop_Animal)": 0x000F42DB,
    "Bonus Logging Drop (CollectDrop_Log)": 0x000F429D,
}

BUFF_NAMES: Dict[int, str] = {v: k for k, v in BUFF_HASHES.items()}

_FLAT2_HASHES: Set[int] = {
    BUFF_HASHES["Damage Dealt (DDD)"],
    BUFF_HASHES["Defense (DPV)"],
    BUFF_HASHES["Invincible"],
    BUFF_HASHES["Critical Damage (CriticalDamage)"],
    BUFF_HASHES["Damage Taken Rate (AttackedDmgRate)"],
    BUFF_HASHES["Damage Reduction (AttackedDmgReduc)"],
}
_FLAT1_HASHES: Set[int] = {
    BUFF_HASHES["Max HP (Hp)"],
}
_RATE_HASHES: Set[int] = set(BUFF_HASHES.values()) - _FLAT2_HASHES - _FLAT1_HASHES


def _stat_entry_size(hash_val: int) -> int:
    if hash_val in _FLAT2_HASHES:
        return 12
    if hash_val in _FLAT1_HASHES:
        return 8
    return 5


def _stat_size_class(hash_val: int) -> str:
    if hash_val in _FLAT2_HASHES:
        return "flat2"
    if hash_val in _FLAT1_HASHES:
        return "flat1"
    return "rate"

_PABGB_TERM = bytes([0x0B, 0x73, 0xE1, 0xC5, 0xEA, 0x00])

_ITEMINFO_PAZ_OFFSET   = 0x786000
_ITEMINFO_COMPRESSED   = 753344
_ITEMINFO_UNCOMPRESSED = 4775790
_ITEMINFO_PAMT_OFFSET  = 0x00096731


def _find_pabgb_in_pamt(game_path: str, filename: str):
    pamt_path = os.path.join(game_path, "0008", "0.pamt")
    if not os.path.isfile(pamt_path):
        return None

    try:
        import sys as _sys
        my_dir = os.path.dirname(os.path.abspath(__file__))
        for d in (os.path.join(my_dir, 'Communitydump', 'source'),
                  os.path.join(my_dir, 'Communitydump', 'BestCrypto'),
                  os.path.join(my_dir, 'tools')):
            if os.path.isdir(d) and d not in _sys.path:
                _sys.path.insert(0, d)
        from paz_parse import parse_pamt

        paz_dir = os.path.join(game_path, "0008")
        with open(pamt_path, 'rb') as f:
            pamt_data = f.read()
        entries = parse_pamt(pamt_path, paz_dir=paz_dir)

        for e in entries:
            if filename in e.path.lower():
                target = struct.pack('<III', e.offset, e.comp_size, e.orig_size)
                pamt_pos = pamt_data.find(target)
                pamt_comp_offset = pamt_pos + 4 if pamt_pos >= 0 else -1

                log.info("Found %s via PAMT: paz=%s offset=0x%X comp=%d orig=%d pamt_comp_off=0x%X",
                         filename, e.paz_file, e.offset, e.comp_size, e.orig_size, pamt_comp_offset)
                return (e.paz_file, e.offset, e.comp_size, e.orig_size, pamt_comp_offset)

        log.warning("%s not found in PAMT entries", filename)
    except Exception as ex:
        log.warning("Dynamic PAMT lookup failed for %s: %s — falling back to hardcoded offsets", filename, ex)

    return None


def _find_iteminfo_in_pamt(game_path: str):
    return _find_pabgb_in_pamt(game_path, 'iteminfo.pabgb')


@dataclass
class StatEntry:
    offset: int
    hash_val: int
    value: int
    param2: int = 0
    size_class: str = "flat2"

    @property
    def name(self) -> str:
        return BUFF_NAMES.get(self.hash_val, f"0x{self.hash_val:08X}")

    @property
    def entry_size(self) -> int:
        return _stat_entry_size(self.hash_val)


@dataclass
class StatArray:
    offset: int
    count: int
    entries: List[StatEntry]
    total_size: int

    @property
    def end_offset(self) -> int:
        return self.offset + self.total_size


StatTriplet = StatEntry


@dataclass
class ItemRecord:
    name: str
    name_offset: int
    data_offset: int
    data_end: int
    item_key: int = 0
    stat_triplets: List[StatTriplet] = field(default_factory=list)


class ItemBuffPatcher:

    def __init__(self, game_path: str):
        self.game_path = game_path
        self.paz_path = os.path.join(game_path, "0008", "0.paz")
        self.pamt_path = os.path.join(game_path, "0008", "0.pamt")
        self._paz_manager = PazPatchManager(game_path)
        self._original_data: Optional[bytes] = None

        self._dynamic_info = _find_iteminfo_in_pamt(game_path)
        if self._dynamic_info:
            self.paz_path = self._dynamic_info[0]
            self._paz_offset = self._dynamic_info[1]
            self._comp_size = self._dynamic_info[2]
            self._orig_size = self._dynamic_info[3]
            self._pamt_comp_offset = self._dynamic_info[4]
        else:
            self._paz_offset = _ITEMINFO_PAZ_OFFSET
            self._comp_size = _ITEMINFO_COMPRESSED
            self._orig_size = _ITEMINFO_UNCOMPRESSED
            self._pamt_comp_offset = _ITEMINFO_PAMT_OFFSET

        self._paz_slot_capacity = self._detect_paz_slot_capacity()

    def _detect_paz_slot_capacity(self) -> int:
        KNOWN_SLOT = 753344

        try:
            with open(self.paz_path, "rb") as f:
                f.seek(self._paz_offset)
                slot_data = f.read(KNOWN_SLOT)

            if len(slot_data) == KNOWN_SLOT:
                log.info("PAZ slot capacity: %d bytes (known)", KNOWN_SLOT)
                return KNOWN_SLOT
        except Exception as e:
            log.debug("Slot capacity detection failed: %s", e)

        return max(self._comp_size, KNOWN_SLOT)


    def _read_pamt_compressed_size(self) -> int:
        try:
            if self._pamt_comp_offset >= 0:
                with open(self.pamt_path, "rb") as f:
                    f.seek(self._pamt_comp_offset)
                    raw = f.read(4)
                    if len(raw) == 4:
                        return struct.unpack_from("<I", raw, 0)[0]
        except Exception:
            pass
        return self._comp_size

    def extract_iteminfo(self) -> bytes:
        try:
            import crimson_rs
            pamt = crimson_rs.parse_pamt_file(self.pamt_path)
            for d in pamt['directories']:
                for f in d.get('files', []):
                    if f.get('name', '').lower() == 'iteminfo.pabgb':
                        paz_path = os.path.join(os.path.dirname(self.pamt_path),
                                                f"{f['chunk_id']}.paz")
                        with open(paz_path, "rb") as fh:
                            fh.seek(f['chunk_offset'])
                            compressed = fh.read(f['compressed_size'])
                        decompressed = crimson_rs.decompress_data(
                            compressed, f['compression'], f['uncompressed_size'])
                        self.paz_path = paz_path
                        self._paz_offset = f['chunk_offset']
                        self._comp_size = f['compressed_size']
                        self._orig_size = f['uncompressed_size']
                        self._original_data = decompressed
                        log.info(
                            "Extracted iteminfo.pabgb via crimson_rs: %d -> %d bytes",
                            f['compressed_size'], f['uncompressed_size'],
                        )
                        return decompressed
            log.warning("iteminfo.pabgb not found in PAMT via crimson_rs, trying legacy path")
        except Exception as ex:
            log.warning("crimson_rs extraction failed: %s — trying legacy path", ex)

        if not HAS_LZ4:
            raise RuntimeError(
                "lz4 package is not installed. Install with: pip install lz4"
            )

        if not os.path.isfile(self.paz_path):
            raise RuntimeError(f"PAZ file not found: {self.paz_path}")

        actual_compressed = self._read_pamt_compressed_size()

        with open(self.paz_path, "rb") as f:
            f.seek(self._paz_offset)
            compressed = f.read(actual_compressed)

        if len(compressed) != actual_compressed:
            raise RuntimeError(
                f"Short read: expected {actual_compressed} bytes, "
                f"got {len(compressed)}"
            )

        try:
            decompressed = lz4.block.decompress(
                compressed, uncompressed_size=self._orig_size
            )
        except Exception as e:
            raise RuntimeError(f"LZ4 decompression failed: {e}") from e

        self._original_data = decompressed
        log.info(
            "Extracted iteminfo.pabgb: %d bytes compressed -> %d bytes",
            len(compressed), len(decompressed),
        )
        return decompressed


    def find_items(self, data: bytes) -> List[ItemRecord]:
        items: List[ItemRecord] = []
        if len(data) < 4:
            return items

        try:
            from iteminfo_parser import find_all_items, parse_item as _parse_structural

            raw_items = find_all_items(data)
            log.info("Structural parser found %d items", len(raw_items))

            for idx in range(len(raw_items)):
                off, item_id, name = raw_items[idx]
                next_off = raw_items[idx + 1][0] if idx + 1 < len(raw_items) else len(data)

                name_len = struct.unpack_from("<I", data, off + 4)[0]
                name_offset = off + 4
                data_offset = off + 8 + name_len + 1

                items.append(ItemRecord(
                    name=name,
                    name_offset=name_offset,
                    data_offset=data_offset,
                    data_end=next_off,
                    item_key=item_id,
                ))

            log.info("Parsed %d items from iteminfo.pabgb (structural)", len(items))
            return items

        except Exception as exc:
            log.warning("Structural parser failed (%s), using fallback scanner", exc)

        record_count = struct.unpack_from("<I", data, 0)[0]
        log.info("iteminfo record_count header: %d", record_count)

        all_entries: List[tuple] = []
        offset = 4
        while offset < len(data) - 8:
            name_len = struct.unpack_from("<I", data, offset)[0]
            if name_len < 1 or name_len > 120:
                offset += 1
                continue
            name_end = offset + 4 + name_len
            if name_end >= len(data):
                break
            candidate = data[offset + 4 : name_end]
            if (all(32 <= b < 127 for b in candidate)
                    and data[name_end] == 0):
                name = candidate.decode("ascii")
                data_start = name_end + 1
                all_entries.append((offset, name, data_start))
                offset = name_end + 1
                continue
            offset += 1

        alpha_indices: List[int] = []
        for i, (_, name, _) in enumerate(all_entries):
            if name[0:1].isalpha():
                alpha_indices.append(i)

        for j, idx in enumerate(alpha_indices):
            name_off, name, data_start = all_entries[idx]
            if j + 1 < len(alpha_indices):
                data_end = all_entries[alpha_indices[j + 1]][0]
            else:
                data_end = len(data)
            item_key = 0
            if name_off >= 4:
                item_key = struct.unpack_from("<I", data, name_off - 4)[0]
            items.append(ItemRecord(
                name=name,
                name_offset=name_off,
                data_offset=data_start,
                data_end=data_end,
                item_key=item_key,
            ))

        log.info("Parsed %d items from iteminfo.pabgb (fallback)", len(items))
        return items

    def find_item_by_name(self, data: bytes, search: str) -> List[ItemRecord]:
        all_items = self.find_items(data)
        search_lower = search.lower()
        return [
            item for item in all_items
            if search_lower in item.name.lower()
        ]


    @staticmethod
    def find_stat_arrays(data: bytes, item: ItemRecord) -> List[StatArray]:
        arrays: List[StatArray] = []
        region_start = item.data_offset
        region_end = item.data_end
        known_hashes = set(BUFF_NAMES.keys())
        seen_offsets: set = set()

        offset = region_start
        while offset < region_end - 8:
            count_val = struct.unpack_from("<I", data, offset)[0]
            if count_val < 1 or count_val > 20:
                offset += 1
                continue

            first_hash_off = offset + 4
            if first_hash_off + 4 > region_end:
                offset += 1
                continue

            first_hash = struct.unpack_from("<I", data, first_hash_off)[0]
            if first_hash not in known_hashes:
                offset += 1
                continue

            entry_sz = _stat_entry_size(first_hash)
            first_class = _stat_size_class(first_hash)

            entries: List[StatEntry] = []
            valid = True
            pos = first_hash_off

            for i in range(count_val):
                if pos + entry_sz > region_end:
                    valid = False
                    break
                h = struct.unpack_from("<I", data, pos)[0]
                if h not in known_hashes:
                    valid = False
                    break
                if _stat_size_class(h) != first_class:
                    valid = False
                    break

                if first_class == "flat2":
                    p1 = struct.unpack_from("<I", data, pos + 4)[0]
                    p2 = struct.unpack_from("<I", data, pos + 8)[0]
                    entries.append(StatEntry(
                        offset=pos, hash_val=h, value=p1,
                        param2=p2, size_class="flat2",
                    ))
                elif first_class == "flat1":
                    val = struct.unpack_from("<I", data, pos + 4)[0]
                    entries.append(StatEntry(
                        offset=pos, hash_val=h, value=val,
                        param2=0, size_class="flat1",
                    ))
                else:
                    level = data[pos + 4]
                    entries.append(StatEntry(
                        offset=pos, hash_val=h, value=level,
                        param2=0, size_class="rate",
                    ))
                pos += entry_sz

            if valid and entries and offset not in seen_offsets:
                total = 4 + count_val * entry_sz
                arrays.append(StatArray(
                    offset=offset, count=count_val,
                    entries=entries, total_size=total,
                ))
                seen_offsets.add(offset)
                offset = pos
            else:
                offset += 1

        arrays.sort(key=lambda a: a.offset)
        return arrays

    @staticmethod
    def find_stat_blocks(data: bytes, item: ItemRecord) -> List[StatEntry]:
        arrays = ItemBuffPatcher.find_stat_arrays(data, item)
        entries: List[StatEntry] = []
        for arr in arrays:
            entries.extend(arr.entries)
        return entries


    @staticmethod
    def inject_buff(
        data: bytearray,
        inject_offset: int,
        buff_hash: int,
        value: int = 1,
    ) -> bytearray:
        payload = struct.pack("<III", 1, buff_hash, value)
        result = data[:inject_offset] + payload + data[inject_offset:]
        return bytearray(result)

    @staticmethod
    def modify_stat_value(
        data: bytearray,
        triplet: StatTriplet,
        new_value: int,
    ) -> bytearray:
        value_offset = triplet.offset + 8
        struct.pack_into("<I", data, value_offset, new_value)
        return data

    _FLAT_HASHES = _FLAT2_HASHES | _FLAT1_HASHES
    _RATE_HASHES = _RATE_HASHES

    @staticmethod
    def overwrite_stat_value(
        data: bytearray,
        entry: StatEntry,
        new_value: int,
    ) -> bytearray:
        if entry.size_class == "rate":
            data[entry.offset + 4] = min(new_value, 15) & 0xFF
        else:
            struct.pack_into("<I", data, entry.offset + 4, new_value)
        return data

    @staticmethod
    def swap_stat_hash(
        data: bytearray,
        entry: StatEntry,
        new_hash: int,
    ) -> bool:
        old_class = _stat_size_class(entry.hash_val)
        new_class = _stat_size_class(new_hash)
        if old_class != new_class:
            return False
        struct.pack_into("<I", data, entry.offset, new_hash)
        return True

    @staticmethod
    def overwrite_stat(
        data: bytearray,
        triplet: StatEntry,
        new_hash: int,
        new_value: int,
    ) -> bytearray:
        old_class = _stat_size_class(triplet.hash_val)
        new_class = _stat_size_class(new_hash)
        if old_class != new_class:
            log.warning("overwrite_stat: size class mismatch %s->%s, skipping hash change",
                        old_class, new_class)
            ItemBuffPatcher.overwrite_stat_value(data, triplet, new_value)
            return data

        struct.pack_into("<I", data, triplet.offset, new_hash)
        if new_class == "rate":
            data[triplet.offset + 4] = min(new_value, 15) & 0xFF
        else:
            struct.pack_into("<I", data, triplet.offset + 4, new_value)
        return data

    @staticmethod
    def remove_stat(data: bytearray, triplet: StatEntry) -> bytearray:
        start = triplet.offset
        end = start + triplet.entry_size
        result = data[:start] + data[end:]
        return bytearray(result)


    def patch_stack_sizes(
        self,
        data: bytearray,
        target_stack: int = 9999,
        min_original: int = 2,
    ) -> Tuple[int, List[str]]:
        items = self.find_items(bytes(data))
        patched = 0
        descriptions = []

        for it in items:
            name_len = struct.unpack_from('<I', data, it.name_offset)[0]
            stack_off = it.name_offset + 4 + name_len + 1
            if stack_off + 4 > len(data):
                continue

            current = struct.unpack_from('<I', data, stack_off)[0]
            if current < min_original or current >= target_stack:
                continue
            if current > 100000:
                continue

            struct.pack_into('<I', data, stack_off, target_stack)
            patched += 1
            descriptions.append(f"{it.name}: {current} -> {target_stack}")

        return patched, descriptions


    def repack_iteminfo(self, modified_data: bytes) -> Tuple[bool, str]:
        if not HAS_LZ4:
            return False, "lz4 package not installed"

        if not os.path.isfile(self.paz_path):
            return False, f"PAZ file not found: {self.paz_path}"

        try:
            compressed = lz4.block.compress(
                modified_data,
                mode="high_compression",
                compression=9,
                store_size=False,
            )
        except Exception as e:
            return False, f"LZ4 compression failed: {e}"

        compressed_size = len(compressed)
        max_slot = self._paz_slot_capacity
        size_diff = compressed_size - max_slot

        if size_diff > 0:
            for level in (6, 3, 1):
                try:
                    compressed = lz4.block.compress(
                        modified_data,
                        mode="high_compression",
                        compression=level,
                        store_size=False,
                    )
                except Exception:
                    continue
                compressed_size = len(compressed)
                size_diff = compressed_size - max_slot
                if size_diff <= 0:
                    log.info("Compression level %d achieved fit: %d bytes", level, compressed_size)
                    break

            if size_diff > 0:
                return False, (
                    f"Compressed data is {size_diff} bytes too large "
                    f"({compressed_size} > {max_slot}). "
                    f"Reduce the number of modifications."
                )

        padded = compressed + b"\x00" * (-size_diff)
        assert len(padded) == max_slot

        log.info(
            "Recompressed: %d -> %d bytes (padded %d null bytes)",
            len(modified_data), compressed_size, -size_diff,
        )

        ok, msg = self._paz_manager.create_backup(self.paz_path)
        if not ok:
            return False, f"Backup failed: {msg}"

        try:
            with open(self.paz_path, "r+b") as f:
                f.seek(self._paz_offset)
                f.write(padded)
        except Exception as e:
            return False, f"Failed to write PAZ: {e}"

        new_uncompressed = len(modified_data)
        try:
            if self._pamt_comp_offset >= 0:
                with open(self.pamt_path, "r+b") as f:
                    f.seek(self._pamt_comp_offset)
                    f.write(struct.pack("<I", compressed_size))
                    if new_uncompressed != self._orig_size:
                        f.write(struct.pack("<I", new_uncompressed))
        except Exception as e:
            return False, f"Failed to update PAMT sizes: {e}"

        pamt_ok, pamt_msg = self._paz_manager.update_pamt_checksum(self.pamt_path)
        if not pamt_ok:
            log.warning("PAMT checksum issue: %s", pamt_msg)

        papgt_ok, papgt_msg = self._paz_manager.update_papgt()
        if not papgt_ok:
            log.warning("PAPGT update issue (retrying): %s", papgt_msg)
            try:
                papgt_ok, papgt_msg = self._paz_manager.update_papgt()
            except Exception as ex:
                papgt_msg = f"PAPGT retry failed: {ex}"
                papgt_ok = False

        result = (
            f"Repacked iteminfo.pabgb: {compressed_size:,} bytes compressed "
            f"({-size_diff:,} bytes padding). "
            f"Uncompressed: {new_uncompressed:,} bytes."
        )
        if pamt_ok:
            result += f"\n{pamt_msg}"
        if papgt_ok:
            result += f"\n{papgt_msg}"
        elif not papgt_ok:
            result += f"\nWARNING: {papgt_msg}"

        return True, result

    def restore_iteminfo(self) -> Tuple[bool, str]:
        return self._paz_manager.restore_all_backups()


_VEHICLEINFO_PAZ_OFFSET   = 0x0100E830
_VEHICLEINFO_COMPRESSED   = 1376
_VEHICLEINFO_UNCOMPRESSED = 6496
_VEHICLEINFO_PAMT_OFFSET  = 0x00096F79

_VEHICLE_SENTINEL = bytes.fromhex('73e1c5ea')


@dataclass
class VehicleRecord:
    name: str
    key: int
    rec_offset: int
    cooldown: int
    cooldown_offset: int


class VehiclePatcher:

    def __init__(self, game_path: str):
        self.game_path = game_path
        self.paz_path = os.path.join(game_path, "0008", "0.paz")
        self.pamt_path = os.path.join(game_path, "0008", "0.pamt")
        self._paz_manager = PazPatchManager(game_path)
        self._original_data: Optional[bytes] = None

        self._dynamic_info = _find_pabgb_in_pamt(game_path, 'vehicleinfo.pabgb')
        if self._dynamic_info:
            self.paz_path = self._dynamic_info[0]
            self._paz_offset = self._dynamic_info[1]
            self._comp_size = self._dynamic_info[2]
            self._orig_size = self._dynamic_info[3]
            self._pamt_comp_offset = self._dynamic_info[4]
        else:
            self._paz_offset = _VEHICLEINFO_PAZ_OFFSET
            self._comp_size = _VEHICLEINFO_COMPRESSED
            self._orig_size = _VEHICLEINFO_UNCOMPRESSED
            self._pamt_comp_offset = _VEHICLEINFO_PAMT_OFFSET


    def _read_pamt_compressed_size(self) -> int:
        if self._pamt_comp_offset >= 0:
            try:
                with open(self.pamt_path, "rb") as f:
                    f.seek(self._pamt_comp_offset)
                    raw = f.read(4)
                    if len(raw) == 4:
                        return struct.unpack_from("<I", raw, 0)[0]
            except Exception:
                pass
        return self._comp_size

    def extract_vehicleinfo(self) -> bytes:
        if not HAS_LZ4:
            raise RuntimeError(
                "lz4 package is not installed. Install with: pip install lz4"
            )

        if not os.path.isfile(self.paz_path):
            raise RuntimeError(f"PAZ file not found: {self.paz_path}")

        actual_compressed = self._read_pamt_compressed_size()

        with open(self.paz_path, "rb") as f:
            f.seek(self._paz_offset)
            compressed = f.read(actual_compressed)

        if len(compressed) != actual_compressed:
            raise RuntimeError(
                f"Short read: expected {actual_compressed} bytes, "
                f"got {len(compressed)}"
            )

        try:
            decompressed = lz4.block.decompress(
                compressed, uncompressed_size=self._orig_size
            )
        except Exception as e:
            raise RuntimeError(f"LZ4 decompression failed: {e}") from e

        self._original_data = decompressed
        log.info(
            "Extracted vehicleinfo.pabgb: %d bytes compressed -> %d bytes",
            len(compressed), len(decompressed),
        )
        return decompressed


    @staticmethod
    def parse_records(data: bytes) -> List[VehicleRecord]:
        records: List[VehicleRecord] = []
        offset = 0
        length = len(data)

        while offset < length - 8:
            key = struct.unpack_from('<H', data, offset)[0]
            name_len = struct.unpack_from('<I', data, offset + 2)[0]

            if name_len < 1 or name_len > 60:
                offset += 1
                continue

            name_end = offset + 6 + name_len
            if name_end >= length:
                break

            candidate = data[offset + 6 : name_end]
            if not all(32 <= b < 127 for b in candidate):
                offset += 1
                continue

            if data[name_end] != 0:
                offset += 1
                continue

            name = candidate.decode('ascii')

            next_offset = name_end + 1
            rec_end = length

            scan = next_offset + 10
            while scan < length - 8:
                nk = struct.unpack_from('<H', data, scan)[0]
                nl = struct.unpack_from('<I', data, scan + 2)[0]
                if 1 <= nl <= 60 and scan + 6 + nl < length:
                    nc = data[scan + 6 : scan + 6 + nl]
                    if all(32 <= b < 127 for b in nc) and data[scan + 6 + nl] == 0:
                        rec_end = scan
                        break
                scan += 1

            field_data_len = rec_end - (name_end + 1)
            if field_data_len >= 33:
                tail_start = rec_end - 33
                cooldown_off = tail_start + 23
                cooldown = struct.unpack_from('<H', data, cooldown_off)[0]
            else:
                cooldown_off = 0
                cooldown = 0

            records.append(VehicleRecord(
                name=name,
                key=key,
                rec_offset=offset,
                cooldown=cooldown,
                cooldown_offset=cooldown_off,
            ))

            offset = rec_end

        return records


    @staticmethod
    def patch_cooldowns(
        data: bytes,
        cooldown_value: int = 0,
        vehicle_filter: Optional[List[str]] = None,
    ) -> Tuple[bytearray, List[str]]:
        records = VehiclePatcher.parse_records(data)
        buf = bytearray(data)
        patched: List[str] = []

        for rec in records:
            if vehicle_filter and rec.name not in vehicle_filter:
                continue

            if rec.cooldown == cooldown_value:
                continue

            if rec.cooldown_offset == 0:
                continue

            old = rec.cooldown
            struct.pack_into('<H', buf, rec.cooldown_offset, cooldown_value)
            patched.append(
                f"{rec.name}: {old}s -> {cooldown_value}s"
            )

        return buf, patched


    def check_cooldown_status(self) -> PatchStatus:
        try:
            data = self.extract_vehicleinfo()
        except RuntimeError as e:
            return PatchStatus("Mount Death Respawn (1s)", "Error", str(e))

        records = self.parse_records(data)
        zeroed = sum(1 for r in records if r.cooldown == 0)
        total = len(records)

        if zeroed == total:
            return PatchStatus("Mount Death Respawn (1s)", "Applied",
                               f"All {total} cooldowns are 0")
        elif zeroed == 0:
            return PatchStatus("Mount Death Respawn (1s)", "Not Applied",
                               f"All {total} cooldowns have vanilla values")
        else:
            return PatchStatus("Mount Death Respawn (1s)", "Partial",
                               f"{zeroed}/{total} cooldowns zeroed")


    def apply_no_cooldown(
        self,
        cooldown_value: int = 0,
        vehicle_filter: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        try:
            data = self.extract_vehicleinfo()
        except RuntimeError as e:
            return False, str(e)

        modified, patch_log = self.patch_cooldowns(data, cooldown_value, vehicle_filter)

        if not patch_log:
            return True, "No changes needed — all cooldowns already at target value."

        try:
            compressed = lz4.block.compress(
                bytes(modified),
                mode="high_compression",
                compression=9,
                store_size=False,
            )
        except Exception as e:
            return False, f"LZ4 compression failed: {e}"

        compressed_size = len(compressed)
        size_diff = compressed_size - self._comp_size

        if size_diff > 0:
            return False, (
                f"Compressed data is {size_diff} bytes too large "
                f"({compressed_size} > {self._comp_size}). "
                f"This should not happen for cooldown-only patches."
            )

        padded = compressed + b"\x00" * (-size_diff)
        assert len(padded) == self._comp_size

        log.info(
            "Recompressed vehicleinfo: %d -> %d bytes (padded %d null bytes)",
            len(modified), compressed_size, -size_diff,
        )

        ok, msg = self._paz_manager.create_backup(self.paz_path)
        if not ok:
            return False, f"Backup failed: {msg}"

        original_paz_size = os.path.getsize(self.paz_path)
        try:
            with open(self.paz_path, "r+b") as f:
                f.seek(self._paz_offset)
                f.write(padded)
        except Exception as e:
            return False, f"Failed to write PAZ: {e}"

        new_paz_size = os.path.getsize(self.paz_path)
        if new_paz_size != original_paz_size:
            log.error("PAZ size changed! %d -> %d", original_paz_size, new_paz_size)
            return False, (
                f"SAFETY: PAZ file size changed ({original_paz_size} -> {new_paz_size}). "
                f"Restore from backup immediately."
            )

        new_uncompressed = len(modified)
        try:
            with open(self.pamt_path, "r+b") as f:
                f.seek(self._pamt_comp_offset)
                f.write(struct.pack("<I", compressed_size))
                if new_uncompressed != self._orig_size:
                    f.write(struct.pack("<I", new_uncompressed))
        except Exception as e:
            return False, f"Failed to update PAMT sizes: {e}"

        pamt_ok, pamt_msg = self._paz_manager.update_pamt_checksum(self.pamt_path)
        if not pamt_ok:
            log.warning("PAMT checksum issue: %s", pamt_msg)

        papgt_ok, papgt_msg = self._paz_manager.update_papgt()
        if not papgt_ok:
            log.warning("PAPGT update issue: %s", papgt_msg)

        result = (
            f"Patched {len(patch_log)} vehicle cooldown(s):\n"
            + "\n".join(f"  {p}" for p in patch_log)
            + f"\n\nRepacked vehicleinfo.pabgb: {compressed_size:,} compressed "
            f"({-size_diff:,} padding)."
        )
        if pamt_ok:
            result += f"\n{pamt_msg}"
        if papgt_ok:
            result += f"\n{papgt_msg}"

        return True, result

    def restore_vehicleinfo(self) -> Tuple[bool, str]:
        return self._paz_manager.restore_all_backups()


_STORAGE_TARGETS = ["CampWareHouse", "WareHouse", "Bank", "Recovery", "Kuku"]


class StoragePatcher:

    def __init__(self, game_path: str):
        self.game_path = game_path
        self._paz_manager = PazPatchManager(game_path)
        self._entry = None

    def _find_entry(self):
        if self._entry:
            return self._entry
        try:
            import sys as _sys
            my_dir = os.path.dirname(os.path.abspath(__file__))
            for d in [os.path.join(my_dir, 'Communitydump', 'source'),
                      os.path.join(my_dir, 'Communitydump', 'BestCrypto')]:
                if os.path.isdir(d) and d not in _sys.path:
                    _sys.path.insert(0, d)
            from paz_parse import parse_pamt

            pamt_path = os.path.join(self.game_path, "0008", "0.pamt")
            entries = parse_pamt(pamt_path, paz_dir=os.path.join(self.game_path, "0008"))
            for e in entries:
                if 'inventory.pabgb' in e.path.lower():
                    self._entry = e
                    return e
        except Exception as ex:
            log.warning("Failed to find inventory.pabgb in PAMT: %s", ex)
        return None

    def extract(self) -> bytes:
        entry = self._find_entry()
        if not entry:
            raise RuntimeError("inventory.pabgb not found in PAMT index")

        with open(entry.paz_file, 'rb') as f:
            f.seek(entry.offset)
            raw = f.read(entry.comp_size)

        if entry.compressed and HAS_LZ4:
            return lz4.block.decompress(raw, uncompressed_size=entry.orig_size)
        return raw

    def parse_records(self, data: bytes) -> List[dict]:
        records = []
        offset = 0
        while offset < len(data) - 8:
            key = struct.unpack_from('<H', data, offset)[0]
            name_len = struct.unpack_from('<I', data, offset + 2)[0]
            if name_len < 1 or name_len > 100:
                offset += 1
                continue
            name_end = offset + 6 + name_len
            if name_end >= len(data):
                break
            candidate = data[offset + 6:name_end]
            if all(32 <= b < 127 for b in candidate) and data[name_end] == 0:
                name = candidate.decode('ascii')
                if name[0:1].isalpha():
                    records.append({"name": name, "key": key, "data_start": name_end + 1, "rec_offset": offset})
                    offset = name_end + 1
                    continue
            offset += 1

        for i in range(len(records)):
            records[i]["data_end"] = records[i + 1]["rec_offset"] if i + 1 < len(records) else len(data)

        return records

    def patch_storage(self, data: bytearray, target_slots: int = 900,
                      char_default: int = 100, char_max: int = 240) -> Tuple[int, List[str]]:
        records = self.parse_records(bytes(data))
        patched = 0
        descriptions = []

        for rec in records:
            ds = rec["data_start"]
            if ds + 5 > len(data):
                continue

            old_default = struct.unpack_from('<H', data, ds + 1)[0]
            old_max = struct.unpack_from('<H', data, ds + 3)[0]

            if rec["name"] == "Character":
                if old_default == char_default and old_max == char_max:
                    continue
                struct.pack_into('<H', data, ds + 1, char_default)
                struct.pack_into('<H', data, ds + 3, char_max)
                descriptions.append(f"Character: {old_default}/{old_max} -> {char_default}/{char_max}")
                patched += 1
            elif rec["name"] in _STORAGE_TARGETS:
                if old_default == target_slots and old_max == target_slots:
                    continue
                struct.pack_into('<H', data, ds + 1, target_slots)
                struct.pack_into('<H', data, ds + 3, target_slots)
                descriptions.append(f"{rec['name']}: {old_default}/{old_max} -> {target_slots}/{target_slots}")
                patched += 1

        return patched, descriptions

    def check_status(self) -> Tuple[str, List[str]]:
        try:
            data = self.extract()
        except Exception as e:
            return "Error", [str(e)]

        records = self.parse_records(data)
        details = []
        all_patched = True

        for rec in records:
            if rec["name"] not in _STORAGE_TARGETS and rec["name"] != "Character":
                continue
            ds = rec["data_start"]
            if ds + 5 > len(data):
                continue
            default = struct.unpack_from('<H', data, ds + 1)[0]
            max_s = struct.unpack_from('<H', data, ds + 3)[0]
            details.append(f"{rec['name']}: {default}/{max_s}")
            if rec["name"] == "Character":
                if default < 100:
                    all_patched = False
            elif default != 900 or max_s != 900:
                all_patched = False

        return "Applied" if all_patched else "Not Applied", details

    def apply(self, target_slots: int = 900) -> Tuple[bool, str]:
        entry = self._find_entry()
        if not entry:
            return False, "inventory.pabgb not found in PAMT"

        if not HAS_LZ4:
            return False, "lz4 package not installed"

        try:
            data = bytearray(self.extract())
            count, descriptions = self.patch_storage(data, target_slots)

            if count == 0:
                return True, "All storage already at target value."

            compressed = lz4.block.compress(bytes(data), mode="high_compression", compression=9,
                                             store_size=False)

            slot_capacity = entry.comp_size if not entry.compressed else entry.comp_size
            if not entry.compressed:
                write_data = bytes(data)
                new_comp_size = len(write_data)
            else:
                if len(compressed) > entry.comp_size:
                    return False, f"Compressed size {len(compressed)} exceeds slot {entry.comp_size}"
                write_data = compressed + b'\x00' * (entry.comp_size - len(compressed))
                new_comp_size = len(compressed)

            self._paz_manager.create_backup(entry.paz_file)

            with open(entry.paz_file, 'r+b') as f:
                f.seek(entry.offset)
                f.write(write_data)

            if entry.compressed and new_comp_size != entry.comp_size:
                pamt_path = os.path.join(self.game_path, "0008", "0.pamt")
                with open(pamt_path, 'rb') as f:
                    pamt_data = bytearray(f.read())
                target = struct.pack('<III', entry.offset, entry.comp_size, entry.orig_size)
                pos = pamt_data.find(target)
                if pos >= 0:
                    struct.pack_into('<I', pamt_data, pos + 4, new_comp_size)
                    self._paz_manager.create_backup(pamt_path)
                    with open(pamt_path, 'wb') as f:
                        f.write(pamt_data)
                    self._paz_manager.update_pamt_checksum(pamt_path)

            self._paz_manager.update_papgt()

            return True, f"Patched {count} storage records to {target_slots} slots:\n" + "\n".join(f"  {d}" for d in descriptions)

        except Exception as e:
            log.exception("Storage patch failed")
            return False, f"Storage patch failed: {e}"


class MountPatcher:

    def __init__(self, game_path: str):
        self.game_path = game_path
        self._paz_manager = PazPatchManager(game_path)

    def _find_pamt_entry(self, filename: str):
        try:
            import sys as _sys
            my_dir = os.path.dirname(os.path.abspath(__file__))
            for d in [os.path.join(my_dir, 'Communitydump', 'source'),
                      os.path.join(my_dir, 'Communitydump', 'BestCrypto')]:
                if os.path.isdir(d) and d not in _sys.path:
                    _sys.path.insert(0, d)
            from paz_parse import parse_pamt
            pamt_path = os.path.join(self.game_path, "0008", "0.pamt")
            entries = parse_pamt(pamt_path, paz_dir=os.path.join(self.game_path, "0008"))
            for e in entries:
                if filename in e.path.lower():
                    return e
        except Exception as ex:
            log.warning("PAMT lookup for %s failed: %s", filename, ex)
        return None

    def _extract(self, entry) -> bytes:
        with open(entry.paz_file, 'rb') as f:
            f.seek(entry.offset)
            raw = f.read(entry.comp_size)
        if entry.compressed and HAS_LZ4:
            return lz4.block.decompress(raw, uncompressed_size=entry.orig_size)
        return raw

    def _repack(self, entry, data: bytes) -> Tuple[bool, str]:
        if not HAS_LZ4:
            return False, "lz4 not installed"

        if entry.compressed:
            compressed = None
            for mode, lvl in [("high_compression", 12), ("high_compression", 9),
                              ("high_compression", 6), ("default", 0)]:
                try:
                    if mode == "default":
                        c = lz4.block.compress(data, store_size=False)
                    else:
                        c = lz4.block.compress(data, mode=mode, compression=lvl, store_size=False)
                    if len(c) <= entry.comp_size:
                        compressed = c
                        break
                except Exception:
                    continue
            if compressed is None:
                return False, f"All compression levels exceed slot {entry.comp_size}"
            write_data = compressed + b'\x00' * (entry.comp_size - len(compressed))
            new_comp_size = len(compressed)
        else:
            write_data = data
            new_comp_size = len(data)

        self._paz_manager.create_backup(entry.paz_file)

        with open(entry.paz_file, 'r+b') as f:
            f.seek(entry.offset)
            f.write(write_data)

        if entry.compressed and new_comp_size != entry.comp_size:
            pamt_path = os.path.join(self.game_path, "0008", "0.pamt")
            with open(pamt_path, 'rb') as f:
                pamt_data = bytearray(f.read())
            target = struct.pack('<III', entry.offset, entry.comp_size, entry.orig_size)
            pos = pamt_data.find(target)
            if pos >= 0:
                struct.pack_into('<I', pamt_data, pos + 4, new_comp_size)
                self._paz_manager.create_backup(pamt_path)
                with open(pamt_path, 'wb') as f:
                    f.write(pamt_data)
                self._paz_manager.update_pamt_checksum(pamt_path)

        self._paz_manager.update_papgt()
        return True, f"Written {len(write_data):,}B"

    @staticmethod
    def _find_record_data(data: bytes, record_name: str) -> Optional[int]:
        sig = record_name.encode('ascii') + b'\x00'
        off = data.find(sig)
        if off < 0:
            return None
        return off + len(record_name) + 1

    def check_status(self) -> List[str]:
        results = []

        entry = self._find_pamt_entry('skill.pabgb')
        if entry:
            data = self._extract(entry)

            ds = self._find_record_data(data, 'Active_RideDragon_Long')
            if ds:
                val = struct.unpack_from('<I', data, ds + 24)[0]
                results.append(f"Dragon Ride Cooldown: {val}ms ({val/1000:.0f}s)")

            ds = self._find_record_data(data, 'Gimmick_RideLimit')
            if ds:
                val = struct.unpack_from('<I', data, ds + 128)[0]
                results.append(f"Ride Limit: {val}")

            ds = self._find_record_data(data, 'Active_DecreaseMercenaryCooltime_Reduce_Dragon')
            if ds:
                val = struct.unpack_from('<I', data, ds + 163)[0]
                results.append(f"Dragon Horn Cooldown Reduction: {val}s ({val/60:.0f}min)")

        entry2 = self._find_pamt_entry('conditioninfo.pabgb')
        if entry2:
            data2 = self._extract(entry2)

            ds_generic = self._find_record_data(data2, 'Interaction_Riding')
            ds_dragon = self._find_record_data(data2, 'Interaction_Riding_Dragon')
            if ds_generic and ds_dragon:
                generic = data2[ds_generic:ds_generic + 18]
                dragon = data2[ds_dragon:ds_dragon + 18]
                if generic == dragon:
                    results.append("Dragon Interaction: PATCHED (matches generic)")
                else:
                    results.append("Dragon Interaction: Original (differs from generic)")

        return results

    def apply_all(self, ride_cooldown_ms: int = 1000,
                  ride_limit: int = 999999,
                  cooldown_reduction: int = 99999,
                  patch_interaction: bool = True) -> Tuple[bool, str]:
        messages = []

        entry = self._find_pamt_entry('skill.pabgb')
        if not entry:
            return False, "skill.pabgb not found in PAMT"

        data = bytearray(self._extract(entry))
        patched = 0

        ds = self._find_record_data(bytes(data), 'Active_RideDragon_Long')
        if ds:
            old = struct.unpack_from('<I', data, ds + 24)[0]
            struct.pack_into('<I', data, ds + 24, ride_cooldown_ms)
            messages.append(f"Dragon Ride Cooldown: {old}ms -> {ride_cooldown_ms}ms")
            patched += 1

        ds = self._find_record_data(bytes(data), 'Gimmick_RideLimit')
        if ds:
            old = struct.unpack_from('<I', data, ds + 128)[0]
            struct.pack_into('<I', data, ds + 128, ride_limit)
            messages.append(f"Ride Limit: {old} -> {ride_limit}")
            patched += 1

        ds = self._find_record_data(bytes(data), 'Active_DecreaseMercenaryCooltime_Reduce_Dragon')
        if ds:
            old = struct.unpack_from('<I', data, ds + 163)[0]
            struct.pack_into('<I', data, ds + 163, cooldown_reduction)
            messages.append(f"Dragon Horn Cooldown Reduction: {old}s -> {cooldown_reduction}s")
            patched += 1

        if patched > 0:
            ok, msg = self._repack(entry, bytes(data))
            if not ok:
                return False, f"skill.pabgb repack failed: {msg}"
            messages.append(f"skill.pabgb: {msg}")

        if patch_interaction:
            entry2 = self._find_pamt_entry('conditioninfo.pabgb')
            if entry2:
                data2 = bytearray(self._extract(entry2))

                ds_generic = self._find_record_data(bytes(data2), 'Interaction_Riding')
                ds_dragon = self._find_record_data(bytes(data2), 'Interaction_Riding_Dragon')

                if ds_generic and ds_dragon:
                    generic_bytes = bytes(data2[ds_generic:ds_generic + 18])
                    old_dragon = bytes(data2[ds_dragon:ds_dragon + 18])

                    if generic_bytes != old_dragon:
                        data2[ds_dragon:ds_dragon + 18] = generic_bytes
                        messages.append(f"Dragon Interaction: patched to match generic riding")

                        ok2, msg2 = self._repack(entry2, bytes(data2))
                        if not ok2:
                            return False, f"conditioninfo.pabgb repack failed: {msg2}"
                        messages.append(f"conditioninfo.pabgb: {msg2}")
                    else:
                        messages.append("Dragon Interaction: already matches generic")

        return True, "\n".join(messages)


class ItemEffectPatcher:

    EFFECT_HASH_DISTANCE = 41

    KNOWN_EFFECTS = {
        0xB0A8256B: "Instant Dragon CD Reset (Narima's Horn)",
        0x5F7B936D: "Dragon CD -10min (Dragon Claw Horn)",
        0x5985B2D4: "Food Effect (Blackberry)",
    }

    def __init__(self, game_path: str):
        self.game_path = game_path
        self._paz_manager = PazPatchManager(game_path)

    def _find_pamt_entry(self, filename: str):
        try:
            import sys as _sys
            my_dir = os.path.dirname(os.path.abspath(__file__))
            for d in [os.path.join(my_dir, 'Communitydump', 'source'),
                      os.path.join(my_dir, 'Communitydump', 'BestCrypto')]:
                if os.path.isdir(d) and d not in _sys.path:
                    _sys.path.insert(0, d)
            from paz_parse import parse_pamt
            pamt_path = os.path.join(self.game_path, "0008", "0.pamt")
            entries = parse_pamt(pamt_path, paz_dir=os.path.join(self.game_path, "0008"))
            for e in entries:
                if filename in e.path.lower():
                    return e
        except Exception as ex:
            log.warning("PAMT lookup for %s failed: %s", filename, ex)
        return None

    def _extract(self, entry) -> bytes:
        with open(entry.paz_file, 'rb') as f:
            f.seek(entry.offset)
            raw = f.read(entry.comp_size)
        if entry.compressed and HAS_LZ4:
            return lz4.block.decompress(raw, uncompressed_size=entry.orig_size)
        return raw

    def _repack(self, entry, data: bytes) -> Tuple[bool, str]:
        if not HAS_LZ4:
            return False, "lz4 not installed"
        if entry.compressed:
            compressed = None
            for mode, lvl in [("high_compression", 12), ("high_compression", 9),
                              ("high_compression", 6), ("default", 0)]:
                try:
                    if mode == "default":
                        c = lz4.block.compress(data, store_size=False)
                    else:
                        c = lz4.block.compress(data, mode=mode, compression=lvl, store_size=False)
                    if len(c) <= entry.comp_size:
                        compressed = c
                        break
                except Exception:
                    continue
            if compressed is None:
                return False, f"All compression levels exceed slot {entry.comp_size}"
            write_data = compressed + b'\x00' * (entry.comp_size - len(compressed))
            new_comp_size = len(compressed)
        else:
            write_data = data
            new_comp_size = len(data)

        self._paz_manager.create_backup(entry.paz_file)
        with open(entry.paz_file, 'r+b') as f:
            f.seek(entry.offset)
            f.write(write_data)

        if entry.compressed and new_comp_size != entry.comp_size:
            pamt_path = os.path.join(self.game_path, "0008", "0.pamt")
            with open(pamt_path, 'rb') as f:
                pamt_data = bytearray(f.read())
            target = struct.pack('<III', entry.offset, entry.comp_size, entry.orig_size)
            pos = pamt_data.find(target)
            if pos >= 0:
                struct.pack_into('<I', pamt_data, pos + 4, new_comp_size)
                self._paz_manager.create_backup(pamt_path)
                with open(pamt_path, 'wb') as f:
                    f.write(pamt_data)
                self._paz_manager.update_pamt_checksum(pamt_path)

        self._paz_manager.update_papgt()
        return True, f"Written {len(write_data):,}B"

    @staticmethod
    def find_effect_hash(record: bytes) -> Optional[Tuple[int, int, int]]:
        start = max(len(record) // 3, 200)
        for i in range(start, len(record) - ItemEffectPatcher.EFFECT_HASH_DISTANCE - 4):
            b = record[i:i+4]
            if 0 in b:
                continue
            v = struct.unpack_from('<I', record, i)[0]
            if v == 0xFFFFFFFF:
                continue
            j = i + ItemEffectPatcher.EFFECT_HASH_DISTANCE
            if j + 4 <= len(record):
                v2 = struct.unpack_from('<I', record, j)[0]
                if v == v2:
                    return (v, i, j)
        return None

    @staticmethod
    def _find_record_by_name(data: bytes, name: str) -> Optional[Tuple[int, int]]:
        sig = name.encode('ascii') + b'\x00'
        pos = data.find(sig)
        if pos < 0:
            return None
        rec_start = pos - 8
        return rec_start, pos + len(sig)

    def swap_effect(self, target_item_name: str, source_effect_hash: int) -> Tuple[bool, str]:
        entry = self._find_pamt_entry('iteminfo.pabgb')
        if not entry:
            return False, "iteminfo.pabgb not found in PAMT"

        data = bytearray(self._extract(entry))

        sig = target_item_name.encode('ascii') + b'\x00'
        name_pos = data.find(sig)
        if name_pos < 0:
            return False, f"Item '{target_item_name}' not found in iteminfo.pabgb"

        rec_start = name_pos - 8

        rec_end = len(data)
        search_from = name_pos + len(sig) + 100
        for probe in [b'Item_', b'Blackberry\x00', b'Elderberry\x00', b'Rasberry\x00']:
            nxt = data.find(probe, search_from)
            if nxt > 0 and nxt - 8 < rec_end:
                rec_end = nxt - 8

        record = bytes(data[rec_start:rec_end])
        result = self.find_effect_hash(record)
        if not result:
            return False, f"Could not find effect hash in '{target_item_name}' record"

        old_hash, pos1_rel, pos2_rel = result
        pos1 = rec_start + pos1_rel
        pos2 = rec_start + pos2_rel

        old_name = self.KNOWN_EFFECTS.get(old_hash, f"0x{old_hash:08X}")
        new_name = self.KNOWN_EFFECTS.get(source_effect_hash, f"0x{source_effect_hash:08X}")

        struct.pack_into('<I', data, pos1, source_effect_hash)
        struct.pack_into('<I', data, pos2, source_effect_hash)

        ok, msg = self._repack(entry, bytes(data))
        if not ok:
            return False, f"Repack failed: {msg}"

        return True, (
            f"Swapped effect on '{target_item_name}':\n"
            f"  Old: {old_name}\n"
            f"  New: {new_name}\n"
            f"  Patched at offsets 0x{pos1:06X} and 0x{pos2:06X}\n"
            f"  {msg}"
        )

    def check_effect(self, item_name: str) -> Optional[Tuple[int, str]]:
        entry = self._find_pamt_entry('iteminfo.pabgb')
        if not entry:
            return None

        data = self._extract(entry)
        sig = item_name.encode('ascii') + b'\x00'
        name_pos = data.find(sig)
        if name_pos < 0:
            return None

        rec_start = name_pos - 8
        record = data[rec_start:rec_start + 800]
        result = self.find_effect_hash(record)
        if not result:
            return None

        h = result[0]
        return (h, self.KNOWN_EFFECTS.get(h, f"Unknown (0x{h:08X})"))
