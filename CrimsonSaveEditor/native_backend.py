"""Native C++ save validation/write bridge.

The GUI deliberately talks to a small C ABI instead of C++ objects.  Every
operation returns JSON allocated by the DLL and released with ``parc_free``.
No network access is used by this module.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


class NativeBackendError(RuntimeError):
    """The native backend was missing or rejected an unsafe operation."""


def _application_dirs() -> list[Path]:
    dirs: list[Path] = []
    frozen_dir = getattr(sys, "_MEIPASS", None)
    if frozen_dir:
        dirs.append(Path(frozen_dir))
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    dirs.append(Path(__file__).resolve().parent)

    result: list[Path] = []
    for directory in dirs:
        if directory not in result:
            result.append(directory)
    return result


class NativeSaveBackend:
    """ctypes wrapper for ``parc_parser.dll`` backend API version 2.1+."""

    def __init__(self) -> None:
        self._dll: ctypes.CDLL | None = None
        self._dll_path: Path | None = None
        self._load_error = ""
        self._load()

    @property
    def available(self) -> bool:
        return self._dll is not None

    @property
    def load_error(self) -> str:
        return self._load_error

    @property
    def dll_path(self) -> str:
        return str(self._dll_path) if self._dll_path else ""

    @property
    def version(self) -> str:
        if not self._dll:
            return "unavailable"
        raw = self._dll.parc_version()
        return raw.decode("utf-8", errors="replace") if raw else "unknown"

    def _load(self) -> None:
        candidates = [directory / "parc_parser.dll" for directory in _application_dirs()]
        for dll_path in candidates:
            if not dll_path.is_file():
                continue
            try:
                dll = ctypes.CDLL(str(dll_path))
                required = (
                    "parc_version",
                    "parc_validate_file",
                    "parc_validate_blob",
                    "parc_write_validated_save",
                    "parc_free",
                )
                missing = [name for name in required if not hasattr(dll, name)]
                if missing:
                    self._load_error = (
                        f"{dll_path.name} is an older parser-only build; missing "
                        + ", ".join(missing)
                    )
                    continue

                dll.parc_version.restype = ctypes.c_char_p
                dll.parc_free.argtypes = [ctypes.c_void_p]
                dll.parc_free.restype = None

                dll.parc_validate_file.argtypes = [
                    ctypes.c_char_p,
                    ctypes.POINTER(ctypes.c_char_p),
                    ctypes.POINTER(ctypes.c_uint32),
                ]
                dll.parc_validate_file.restype = ctypes.c_int

                dll.parc_validate_blob.argtypes = [
                    ctypes.POINTER(ctypes.c_uint8),
                    ctypes.c_uint32,
                    ctypes.POINTER(ctypes.c_char_p),
                    ctypes.POINTER(ctypes.c_uint32),
                ]
                dll.parc_validate_blob.restype = ctypes.c_int

                dll.parc_write_validated_save.argtypes = [
                    ctypes.c_char_p,
                    ctypes.POINTER(ctypes.c_uint8),
                    ctypes.c_uint32,
                    ctypes.c_char_p,
                    ctypes.POINTER(ctypes.c_char_p),
                    ctypes.POINTER(ctypes.c_uint32),
                ]
                dll.parc_write_validated_save.restype = ctypes.c_int

                self._dll = dll
                self._dll_path = dll_path
                self._load_error = ""
                return
            except (OSError, AttributeError) as exc:
                self._load_error = f"Failed to load {dll_path}: {exc}"

        if not self._load_error:
            self._load_error = "parc_parser.dll was not found beside the application"

    def _require(self) -> ctypes.CDLL:
        if self._dll is None:
            raise NativeBackendError(
                "The C++ save-safety backend is unavailable. Saving is disabled.\n\n"
                + self._load_error
            )
        return self._dll

    @staticmethod
    def _blob_buffer(blob: bytes | bytearray) -> Any:
        if not blob:
            raise NativeBackendError("The decompressed save data is empty")
        return (ctypes.c_uint8 * len(blob)).from_buffer_copy(bytes(blob))

    def _decode_result(
        self,
        return_code: int,
        out_json: ctypes.c_char_p,
        out_size: ctypes.c_uint32,
    ) -> dict[str, Any]:
        dll = self._require()
        try:
            if not out_json:
                raise NativeBackendError(
                    f"Native backend returned code {return_code} without a result"
                )
            raw = ctypes.string_at(out_json, out_size.value)
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NativeBackendError(f"Invalid response from native backend: {exc}") from exc
        finally:
            if out_json:
                dll.parc_free(out_json)

        if return_code != 0 or not result.get("ok", False):
            error = result.get("error")
            if not error:
                errors = result.get("errors") or []
                error = "; ".join(str(item) for item in errors)
            raise NativeBackendError(error or f"Native backend rejected the operation ({return_code})")
        return result

    def validate_save(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        dll = self._require()
        out_json = ctypes.c_char_p()
        out_size = ctypes.c_uint32()
        code = dll.parc_validate_file(
            os.fsencode(os.fspath(path)), ctypes.byref(out_json), ctypes.byref(out_size)
        )
        return self._decode_result(code, out_json, out_size)

    def validate_blob(self, blob: bytes | bytearray) -> dict[str, Any]:
        dll = self._require()
        buffer = self._blob_buffer(blob)
        out_json = ctypes.c_char_p()
        out_size = ctypes.c_uint32()
        code = dll.parc_validate_blob(
            buffer, len(blob), ctypes.byref(out_json), ctypes.byref(out_size)
        )
        return self._decode_result(code, out_json, out_size)

    def write_validated_save(
        self,
        source_save_path: str | os.PathLike[str],
        blob: bytes | bytearray,
        output_save_path: str | os.PathLike[str],
    ) -> dict[str, Any]:
        """Validate, write, reload, then atomically install a SAVE container."""
        dll = self._require()
        source = os.path.abspath(os.fspath(source_save_path))
        output = os.path.abspath(os.fspath(output_save_path))
        output_dir = os.path.dirname(output) or os.getcwd()
        os.makedirs(output_dir, exist_ok=True)

        buffer = self._blob_buffer(blob)
        handle, temp_path = tempfile.mkstemp(
            prefix=".cse_validated_", suffix=".save", dir=output_dir
        )
        os.close(handle)
        os.unlink(temp_path)

        out_json = ctypes.c_char_p()
        out_size = ctypes.c_uint32()
        try:
            code = dll.parc_write_validated_save(
                os.fsencode(source),
                buffer,
                len(blob),
                os.fsencode(temp_path),
                ctypes.byref(out_json),
                ctypes.byref(out_size),
            )
            result = self._decode_result(code, out_json, out_size)
            os.replace(temp_path, output)
            result["output_path"] = output
            return result
        finally:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass
backend = NativeSaveBackend()
