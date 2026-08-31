"""D2xxTransport against a stub ctypes library.

`transport.py` guards the physical connection and had no tests: every device
selection branch, the config-failure cleanup, short writes and read timeouts
were unexercised. None of this needs hardware - only a stand-in for the DLL.
"""

from __future__ import annotations

import unittest

from multiflo.errors import TransportError, ValidationError
from multiflo.transport import (
    FT_OK,
    D2xxConfig,
    D2xxTransport,
    NullTransport,
)


class FakeD2xxLibrary:
    """Minimal stand-in for the native D2XX DLL surface the transport binds."""

    def __init__(
        self,
        devices=(("14071419", "MultiFlo", 0),),
        *,
        fail_on: str | None = None,
        read_payload: bytes = b"",
        write_count: int | None = None,
        latency: int = 16,
    ):
        self.devices = list(devices)
        self.fail_on = fail_on
        self.read_payload = read_payload
        self.write_count = write_count
        self.latency = latency
        self.calls: list[str] = []
        self.dll = self
        self.closed = False
        self.purges = 0

    # The transport calls _D2xxLibrary.check on every native return code.
    def check(self, status: int, operation: str) -> None:
        if status != FT_OK:
            raise TransportError(f"D2XX {operation} failed: status {status}")

    def _status(self, name: str) -> int:
        self.calls.append(name)
        return 1 if self.fail_on == name else FT_OK

    def enumerate(self):
        from multiflo.transport import DeviceInfo

        self.calls.append("enumerate")
        return [
            DeviceInfo(index, flags, 0, 0x04036001, 0, serial, description)
            for index, (serial, description, flags) in enumerate(self.devices)
        ]

    def FT_OpenEx(self, name, flags, handle_ref):
        handle_ref._obj.value = 0x1234
        return self._status("open")

    def FT_Close(self, handle):
        self.closed = True
        return self._status("close")

    def FT_SetBaudRate(self, handle, baud):
        return self._status("baud")

    def FT_SetDataCharacteristics(self, handle, bits, stop, parity):
        self.data_characteristics = (bits, stop, parity)
        return self._status("data_characteristics")

    def FT_SetFlowControl(self, handle, flow, xon, xoff):
        return self._status("flow")

    def FT_SetDtr(self, handle):
        return self._status("dtr")

    def FT_SetRts(self, handle):
        return self._status("rts")

    def FT_SetTimeouts(self, handle, read_ms, write_ms):
        self.timeouts = (read_ms, write_ms)
        return self._status("timeouts")

    def FT_Purge(self, handle, mask):
        self.purges += 1
        return self._status("purge")

    def FT_GetLatencyTimer(self, handle, value_ref):
        value_ref._obj.value = self.latency
        return self._status("latency")

    def FT_Read(self, handle, buffer, size, count_ref):
        payload = self.read_payload[:size]
        buffer.raw = payload + b"\x00" * (size - len(payload))
        count_ref._obj.value = len(payload)
        return self._status("read")

    def FT_Write(self, handle, buffer, size, count_ref):
        count_ref._obj.value = size if self.write_count is None else self.write_count
        return self._status("write")


def build(library: FakeD2xxLibrary, **kwargs) -> D2xxTransport:
    transport = D2xxTransport.__new__(D2xxTransport)
    transport.expected_serial = kwargs.pop("expected_serial", "14071419")
    transport.expected_description = kwargs.pop("expected_description", "MultiFlo")
    transport.config = kwargs.pop("config", D2xxConfig())
    transport._library = library
    transport._handle = None
    transport.device_info = None
    transport.latency_timer_ms = None
    return transport


class DeviceSelectionTests(unittest.TestCase):
    def test_opens_the_one_matching_device_and_configures_it(self) -> None:
        library = FakeD2xxLibrary()
        transport = build(library, config=D2xxConfig(read_timeout_ms=2500))

        transport.open()

        self.assertTrue(transport.is_open)
        self.assertEqual(transport.device_info.serial_number, "14071419")
        self.assertEqual(transport.latency_timer_ms, 16)
        self.assertEqual(library.timeouts, (2500, 1000))
        # 8 data bits, 2 stop bits, no parity.
        self.assertEqual(library.data_characteristics, (8, 2, 0))
        for expected in ("baud", "data_characteristics", "flow", "dtr", "rts", "purge"):
            self.assertIn(expected, library.calls)

    def test_rejects_when_no_device_matches(self) -> None:
        transport = build(FakeD2xxLibrary(devices=[("99999999", "MultiFlo", 0)]))

        with self.assertRaisesRegex(TransportError, "found 0"):
            transport.open()

    def test_rejects_ambiguous_duplicate_serials(self) -> None:
        transport = build(
            FakeD2xxLibrary(
                devices=[("14071419", "MultiFlo", 0), ("14071419", "MultiFlo", 0)]
            )
        )

        with self.assertRaisesRegex(TransportError, "found 2"):
            transport.open()

    def test_rejects_a_mismatched_description(self) -> None:
        transport = build(FakeD2xxLibrary(devices=[("14071419", "Something Else", 0)]))

        with self.assertRaisesRegex(TransportError, "expected 'MultiFlo'"):
            transport.open()

    def test_rejects_a_device_already_open_elsewhere(self) -> None:
        transport = build(FakeD2xxLibrary(devices=[("14071419", "MultiFlo", 1)]))

        with self.assertRaisesRegex(TransportError, "already open"):
            transport.open()

    def test_open_is_idempotent(self) -> None:
        library = FakeD2xxLibrary()
        transport = build(library)
        transport.open()
        opens = library.calls.count("open")

        transport.open()

        self.assertEqual(library.calls.count("open"), opens)

    def test_a_configuration_failure_closes_the_handle(self) -> None:
        """A leaked handle would keep the instrument locked from every process."""

        library = FakeD2xxLibrary(fail_on="rts")
        transport = build(library)

        with self.assertRaisesRegex(TransportError, "assert RTS"):
            transport.open()

        self.assertTrue(library.closed)
        self.assertFalse(transport.is_open)


class ReadWriteTests(unittest.TestCase):
    def _open(self, **kwargs) -> tuple[D2xxTransport, FakeD2xxLibrary]:
        library = FakeD2xxLibrary(**kwargs)
        transport = build(library)
        transport.open()
        return transport, library

    def test_read_returns_only_the_bytes_the_device_supplied(self) -> None:
        transport, _ = self._open(read_payload=b"\x06\x01\x02")

        self.assertEqual(transport.read(16), b"\x06\x01\x02")

    def test_a_zero_byte_read_is_a_timeout(self) -> None:
        transport, _ = self._open(read_payload=b"")

        with self.assertRaisesRegex(TransportError, "timed out"):
            transport.read(4)

    def test_read_rejects_a_non_positive_size(self) -> None:
        transport, _ = self._open()

        with self.assertRaises(ValidationError):
            transport.read(0)

    def test_a_short_write_is_an_error(self) -> None:
        transport, _ = self._open(write_count=2)

        with self.assertRaisesRegex(TransportError, "short D2XX write: 2 of 5"):
            transport.write(b"hello")

    def test_write_rejects_empty_data(self) -> None:
        transport, _ = self._open()

        with self.assertRaises(ValidationError):
            transport.write(b"")

    def test_purge_discards_both_directions(self) -> None:
        transport, library = self._open()
        before = library.purges

        transport.purge()

        self.assertEqual(library.purges, before + 1)

    def test_operations_require_an_open_handle(self) -> None:
        transport = build(FakeD2xxLibrary())

        for operation in (
            lambda: transport.read(4),
            lambda: transport.write(b"x"),
            lambda: transport.purge(),
        ):
            with self.assertRaisesRegex(TransportError, "not open"):
                operation()

    def test_close_releases_the_handle(self) -> None:
        transport, library = self._open()

        transport.close()

        self.assertTrue(library.closed)
        self.assertFalse(transport.is_open)
        # Closing twice must not call into the DLL with a dead handle.
        transport.close()


class NullTransportTests(unittest.TestCase):
    def test_every_io_path_fails_loudly(self) -> None:
        transport = NullTransport()

        for operation in (
            transport.open,
            lambda: transport.read(1),
            lambda: transport.write(b"x"),
        ):
            with self.assertRaisesRegex(TransportError, "no instrument"):
                operation()

        transport.close()
        transport.purge()


class SerialConfigTests(unittest.TestCase):
    def test_expected_serial_is_required(self) -> None:
        with self.assertRaises(ValidationError):
            D2xxTransport(expected_serial="")


if __name__ == "__main__":
    unittest.main()
