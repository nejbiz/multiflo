"""Narrow FTDI D2XX and serial byte transports.

These move bytes and know nothing about MultiFlo commands. Test doubles live
in tests/fakes.py, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import logging
import os
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

from .errors import TransportError, ValidationError
from .logs import log_event


_LOGGER = logging.getLogger(__name__)


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
    def purge(self) -> None:
        """Discard buffered bytes so a partial frame cannot shift the next read."""


def _default_d2xx_library_name() -> str:
    """Return the platform's native D2XX shared-library file name.

    The D2XX C API is identical across platforms; only the loader and the
    library file name differ. Windows ships `ftd2xx.dll`; FTDI's Linux release
    ships `libftd2xx.so` (RevPi/ARM included). macOS is `libftd2xx.dylib`.
    """

    if os.name == "nt":
        return "ftd2xx.dll"
    if sys.platform == "darwin":
        return "libftd2xx.dylib"
    return "libftd2xx.so"


class _D2xxLibrary:
    def __init__(self, library_path: str | os.PathLike[str] | None = None) -> None:
        path = str(library_path) if library_path else _default_d2xx_library_name()
        try:
            if os.name == "nt":
                if not hasattr(ctypes, "WinDLL"):
                    raise TransportError("WinDLL is unavailable on this interpreter")
                self.dll = ctypes.WinDLL(path)
            else:
                # Linux/macOS D2XX exports the same cdecl FT_* API via CDLL.
                self.dll = ctypes.CDLL(path)
        except OSError as exc:
            raise TransportError(
                f"cannot load native D2XX library {path!r}: {exc}. On Linux install "
                "FTDI's libftd2xx and ensure the ftdi_sio kernel module is not "
                "holding the device."
            ) from exc
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
        log_event(
            _LOGGER,
            "d2xx_opened",
            serial=selected.serial_number,
            description=selected.description,
            vendor_id=f"0x{selected.vendor_id:04x}",
            product_id=f"0x{selected.product_id:04x}",
            latency_timer_ms=self.latency_timer_ms,
            read_timeout_ms=self.config.read_timeout_ms,
            write_timeout_ms=self.config.write_timeout_ms,
            baud_rate=self.config.baud_rate,
        )

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
            log_event(
                _LOGGER,
                "d2xx_short_write",
                level=logging.WARNING,
                requested=len(data),
                written=written.value,
            )
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
            log_event(
                _LOGGER,
                "d2xx_read_timeout",
                level=logging.WARNING,
                requested=size,
                timeout_ms=self.config.read_timeout_ms,
            )
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


class NullTransport:
    """A transport with no instrument behind it.

    Lets the driver and API be constructed offline - OpenAPI export and
    dry-run encoding checks - while guaranteeing that nothing can reach a
    machine. Every I/O call fails loudly rather than silently succeeding.
    """

    is_open = False

    def open(self) -> None:
        raise TransportError("no instrument is configured for this transport")

    def close(self) -> None:
        pass

    def purge(self) -> None:
        pass

    def read(self, size: int) -> bytes:
        raise TransportError("no instrument is configured for this transport")

    def write(self, data: bytes) -> None:
        raise TransportError("no instrument is configured for this transport")


@dataclass(frozen=True, slots=True)
class SerialConfig:
    """8-N-2, no flow control, DTR+RTS asserted, matching the D2XX defaults."""

    baud_rate: int = 38_400
    read_timeout_seconds: float = 3.0
    write_timeout_seconds: float = 3.0
    assert_dtr: bool = True
    assert_rts: bool = True


class SerialByteTransport:
    """Raw byte transport over an FTDI virtual COM port (Linux `/dev/ttyUSB*`).

    The base MultiFlo framing is transport-agnostic, so the driver runs
    unchanged over either D2XX or a raw serial port. This path is used on the
    RevPi, where the FT232 is exposed by the `ftdi_sio` kernel driver. Device
    selection still relies on the in-band product-serial check the driver
    performs before any motion; this class only moves bytes.

    Timeout parity with D2XX is deliberate: a zero-byte (timed-out) read raises
    `TransportError` rather than returning an empty buffer, so the driver's
    disconnect/uncertain-state logic behaves the same as over D2XX.
    """

    def __init__(
        self,
        port: str,
        *,
        config: SerialConfig = SerialConfig(),
    ) -> None:
        if not port:
            raise ValidationError("a serial port path is required")
        self.port = port
        self.config = config
        self._serial: object | None = None

    @property
    def is_open(self) -> bool:
        serial = self._serial
        return bool(serial is not None and getattr(serial, "is_open", False))

    def open(self) -> None:
        if self.is_open:
            return
        try:
            import serial  # Imported lazily so non-serial hosts need no pyserial.
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise TransportError(
                "pyserial is required for SerialByteTransport; install pyserial"
            ) from exc
        try:
            handle = serial.Serial(
                port=self.port,
                baudrate=self.config.baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_TWO,
                timeout=self.config.read_timeout_seconds,
                write_timeout=self.config.write_timeout_seconds,
                rtscts=False,
                dsrdtr=False,
                xonxoff=False,
            )
        except Exception as exc:  # serial.SerialException and friends
            raise TransportError(f"cannot open serial port {self.port!r}: {exc}") from exc
        handle.dtr = self.config.assert_dtr
        handle.rts = self.config.assert_rts
        handle.reset_input_buffer()
        handle.reset_output_buffer()
        self._serial = handle

    def close(self) -> None:
        serial_handle, self._serial = self._serial, None
        if serial_handle is not None:
            try:
                serial_handle.close()
            except Exception as exc:  # pragma: no cover - close errors are not useful
                raise TransportError(f"error closing serial port: {exc}") from exc

    def write(self, data: bytes) -> None:
        if not isinstance(data, bytes) or not data:
            raise ValidationError("write data must be non-empty bytes")
        serial_handle = self._require_handle()
        try:
            written = serial_handle.write(data)
            serial_handle.flush()
        except Exception as exc:
            raise TransportError(f"serial write failed: {exc}") from exc
        if written != len(data):
            raise TransportError(f"short serial write: {written} of {len(data)} bytes")

    def read(self, size: int) -> bytes:
        if size <= 0:
            raise ValidationError("read size must be positive")
        serial_handle = self._require_handle()
        try:
            chunk = serial_handle.read(size)
        except Exception as exc:
            raise TransportError(f"serial read failed: {exc}") from exc
        if not chunk:
            raise TransportError(
                f"serial read timed out after {self.config.read_timeout_seconds} s"
            )
        return bytes(chunk)

    def purge(self) -> None:
        serial_handle = self._require_handle()
        serial_handle.reset_input_buffer()
        serial_handle.reset_output_buffer()

    def _require_handle(self) -> object:
        if not self.is_open:
            raise TransportError("serial port is not open")
        return self._serial

    def __enter__(self) -> "SerialByteTransport":
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
