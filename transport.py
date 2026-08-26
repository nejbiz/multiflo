"""Narrow FTDI D2XX byte transport and a scripted test transport."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import ctypes
import os
from pathlib import Path
from typing import Deque, Iterable, Protocol, runtime_checkable

from .errors import TransportError, ValidationError


FT_OK = 0
FT_OPEN_BY_SERIAL_NUMBER = 1
FT_BITS_8 = 8
FT_STOP_BITS_2 = 2
FT_PARITY_NONE = 0
FT_FLOW_NONE = 0
FT_PURGE_RX = 1
FT_PURGE_TX = 2

_STATUS_NAMES = {
    0: "ok",
    1: "invalid handle",
    2: "device not found",
    3: "device not opened",
    4: "I/O error",
    5: "insufficient resources",
    6: "invalid parameter",
    9: "invalid arguments",
    18: "other error",
}


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    index: int
    flags: int
    device_type: int
    device_id: int
    location_id: int
    serial_number: str
    description: str

    @property
    def vendor_id(self) -> int:
        return (self.device_id >> 16) & 0xFFFF

    @property
    def product_id(self) -> int:
        return self.device_id & 0xFFFF

    @property
    def is_open(self) -> bool:
        return bool(self.flags & 1)


@dataclass(frozen=True, slots=True)
class D2xxConfig:
    baud_rate: int = 38_400
    read_timeout_ms: int = 1_000
    write_timeout_ms: int = 1_000
    assert_dtr: bool = True
    assert_rts: bool = True


@runtime_checkable
class ByteTransport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def read(self, size: int) -> bytes: ...
    def write(self, data: bytes) -> None: ...


class _D2xxLibrary:
    def __init__(self, library_path: str | os.PathLike[str] | None = None) -> None:
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise TransportError("FTDI D2XX transport is currently supported on Windows")
        path = str(library_path) if library_path else "ftd2xx.dll"
        try:
            self.dll = ctypes.WinDLL(path)
        except OSError as exc:
            raise TransportError(f"cannot load native D2XX library {path!r}: {exc}") from exc
        self._declare_functions()

    def _declare_functions(self) -> None:
        d = self.dll
        d.FT_CreateDeviceInfoList.argtypes = [ctypes.POINTER(ctypes.c_ulong)]
        d.FT_CreateDeviceInfoList.restype = ctypes.c_ulong
        d.FT_GetDeviceInfoDetail.argtypes = [
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        d.FT_GetDeviceInfoDetail.restype = ctypes.c_ulong
        d.FT_OpenEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
        d.FT_OpenEx.restype = ctypes.c_ulong
        d.FT_Close.argtypes = [ctypes.c_void_p]
        d.FT_Close.restype = ctypes.c_ulong
        d.FT_SetBaudRate.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        d.FT_SetBaudRate.restype = ctypes.c_ulong
        d.FT_SetDataCharacteristics.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            ctypes.c_ubyte,
            ctypes.c_ubyte,
        ]
        d.FT_SetDataCharacteristics.restype = ctypes.c_ulong
        d.FT_SetFlowControl.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ushort,
            ctypes.c_ubyte,
            ctypes.c_ubyte,
        ]
        d.FT_SetFlowControl.restype = ctypes.c_ulong
        for name in ("FT_SetDtr", "FT_SetRts"):
            function = getattr(d, name)
            function.argtypes = [ctypes.c_void_p]
            function.restype = ctypes.c_ulong
        d.FT_SetTimeouts.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        d.FT_SetTimeouts.restype = ctypes.c_ulong
        d.FT_Purge.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        d.FT_Purge.restype = ctypes.c_ulong
        d.FT_GetLatencyTimer.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte)]
        d.FT_GetLatencyTimer.restype = ctypes.c_ulong
        d.FT_Read.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        d.FT_Read.restype = ctypes.c_ulong
        d.FT_Write.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        d.FT_Write.restype = ctypes.c_ulong

    @staticmethod
    def check(status: int, operation: str) -> None:
        if status != FT_OK:
            name = _STATUS_NAMES.get(status, f"status {status}")
            raise TransportError(f"D2XX {operation} failed: {name}")

    def enumerate(self) -> list[DeviceInfo]:
        count = ctypes.c_ulong()
        self.check(self.dll.FT_CreateDeviceInfoList(ctypes.byref(count)), "enumeration")
        devices: list[DeviceInfo] = []
        for index in range(count.value):
            flags = ctypes.c_ulong()
            device_type = ctypes.c_ulong()
            device_id = ctypes.c_ulong()
            location_id = ctypes.c_ulong()
            serial = ctypes.create_string_buffer(16)
            description = ctypes.create_string_buffer(64)
            handle = ctypes.c_void_p()
            self.check(
                self.dll.FT_GetDeviceInfoDetail(
                    index,
                    ctypes.byref(flags),
                    ctypes.byref(device_type),
                    ctypes.byref(device_id),
                    ctypes.byref(location_id),
                    serial,
                    description,
                    ctypes.byref(handle),
                ),
                f"device detail for index {index}",
            )
            devices.append(
                DeviceInfo(
                    index,
                    flags.value,
                    device_type.value,
                    device_id.value,
                    location_id.value,
                    serial.value.decode("ascii", errors="replace"),
                    description.value.decode("ascii", errors="replace"),
                )
            )
        return devices


class D2xxTransport:
    """Own one FTDI handle and move bytes without MultiFlo semantics."""

    def __init__(
        self,
        *,
        expected_serial: str,
        expected_description: str = "MultiFlo",
        library_path: str | os.PathLike[str] | None = None,
        config: D2xxConfig = D2xxConfig(),
    ) -> None:
        if not expected_serial:
            raise ValidationError("expected_serial is required for guarded device selection")
        self.expected_serial = expected_serial
        self.expected_description = expected_description
        self.config = config
        self._library = _D2xxLibrary(library_path)
        self._handle: ctypes.c_void_p | None = None
        self.device_info: DeviceInfo | None = None
        self.latency_timer_ms: int | None = None

    @classmethod
    def enumerate(
        cls, library_path: str | os.PathLike[str] | None = None
    ) -> list[DeviceInfo]:
        return _D2xxLibrary(library_path).enumerate()

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def open(self) -> None:
        if self._handle is not None:
            return
        devices = self._library.enumerate()
        matches = [d for d in devices if d.serial_number == self.expected_serial]
        if len(matches) != 1:
            raise TransportError(
                f"expected exactly one FTDI device with serial {self.expected_serial!r}; "
                f"found {len(matches)}"
            )
        selected = matches[0]
        if selected.description != self.expected_description:
            raise TransportError(
                f"FTDI serial {selected.serial_number!r} has description "
                f"{selected.description!r}, expected {self.expected_description!r}"
            )
        if selected.is_open:
            raise TransportError("the selected FTDI device is already open by another process")

        handle = ctypes.c_void_p()
        serial = ctypes.c_char_p(self.expected_serial.encode("ascii"))
        self._library.check(
            self._library.dll.FT_OpenEx(
                ctypes.cast(serial, ctypes.c_void_p),
                FT_OPEN_BY_SERIAL_NUMBER,
                ctypes.byref(handle),
            ),
            "open",
        )
        self._handle = handle
        self.device_info = selected
        try:
            self._configure()
        except Exception:
            try:
                self.close()
            finally:
                raise

    def _configure(self) -> None:
        handle = self._require_handle()
        d = self._library.dll
        check = self._library.check
        check(d.FT_SetBaudRate(handle, self.config.baud_rate), "set baud rate")
        check(
            d.FT_SetDataCharacteristics(
                handle,
                FT_BITS_8,
                FT_STOP_BITS_2,
                FT_PARITY_NONE,
            ),
            "set 8-N-2 data characteristics",
        )
        check(d.FT_SetFlowControl(handle, FT_FLOW_NONE, 0x11, 0x13), "disable flow control")
        if self.config.assert_dtr:
            check(d.FT_SetDtr(handle), "assert DTR")
        if self.config.assert_rts:
            check(d.FT_SetRts(handle), "assert RTS")
        check(
            d.FT_SetTimeouts(
                handle,
                self.config.read_timeout_ms,
                self.config.write_timeout_ms,
            ),
            "set timeouts",
        )
        check(d.FT_Purge(handle, FT_PURGE_RX | FT_PURGE_TX), "purge RX/TX")
        latency = ctypes.c_ubyte()
        check(d.FT_GetLatencyTimer(handle, ctypes.byref(latency)), "get latency timer")
        self.latency_timer_ms = latency.value

    def purge(self) -> None:
        handle = self._require_handle()
        self._library.check(
            self._library.dll.FT_Purge(handle, FT_PURGE_RX | FT_PURGE_TX),
            "purge RX/TX",
        )

    def write(self, data: bytes) -> None:
        if not isinstance(data, bytes) or not data:
            raise ValidationError("write data must be non-empty bytes")
        handle = self._require_handle()
        buffer = ctypes.create_string_buffer(data, len(data))
        written = ctypes.c_ulong()
        self._library.check(
            self._library.dll.FT_Write(handle, buffer, len(data), ctypes.byref(written)),
            "write",
        )
        if written.value != len(data):
            raise TransportError(f"short D2XX write: {written.value} of {len(data)} bytes")

    def read(self, size: int) -> bytes:
        if size <= 0:
            raise ValidationError("read size must be positive")
        handle = self._require_handle()
        buffer = ctypes.create_string_buffer(size)
        read_count = ctypes.c_ulong()
        self._library.check(
            self._library.dll.FT_Read(handle, buffer, size, ctypes.byref(read_count)),
            "read",
        )
        if read_count.value == 0:
            raise TransportError(
                f"D2XX read timed out after {self.config.read_timeout_ms} ms"
            )
        return buffer.raw[: read_count.value]

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            self._library.check(self._library.dll.FT_Close(handle), "close")

    def _require_handle(self) -> ctypes.c_void_p:
        if self._handle is None:
            raise TransportError("D2XX device is not open")
        return self._handle

    def __enter__(self) -> "D2xxTransport":
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class ScriptedFakeTransport:
    """Return scripted fragments or failures; intentionally not a simulator."""

    def __init__(
        self,
        reads: Iterable[bytes | Exception] = (),
        *,
        expected_writes: Iterable[bytes] | None = None,
    ) -> None:
        self._reads: Deque[bytes | Exception] = deque(reads)
        self._current = b""
        self._expected_writes = deque(expected_writes or ())
        self.writes: list[bytes] = []
        self.is_open = False

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def write(self, data: bytes) -> None:
        if not self.is_open:
            raise TransportError("fake transport is not open")
        if self._expected_writes:
            expected = self._expected_writes.popleft()
            if data != expected:
                raise TransportError(
                    f"unexpected write {data.hex(' ')}, expected {expected.hex(' ')}"
                )
        self.writes.append(data)

    def read(self, size: int) -> bytes:
        if not self.is_open:
            raise TransportError("fake transport is not open")
        if not self._current:
            if not self._reads:
                raise TransportError("scripted read timed out")
            item = self._reads.popleft()
            if isinstance(item, Exception):
                raise item
            self._current = item
        result, self._current = self._current[:size], self._current[size:]
        if not result:
            raise TransportError("scripted transport disconnected")
        return result

    def assert_script_consumed(self) -> None:
        if self._expected_writes:
            raise AssertionError(f"{len(self._expected_writes)} expected writes remain")
        if self._reads or self._current:
            raise AssertionError("scripted reads remain")

