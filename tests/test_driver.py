from __future__ import annotations

import unittest

from multiflo.codec import (
    Endpoint,
    Frame,
    MessageClass,
    encode_peristaltic_dispense,
    encode_peristaltic_prime,
    encode_peristaltic_purge,
    encode_request,
    encode_shake,
)
from multiflo.driver import (
    BasecodeVersionInfo,
    InstalledModules,
    MultiFloDriver,
    ReadOnlyDeviceInfo,
)
from multiflo.errors import DeviceError, ProtocolError, TransportError, UnknownExecutionState
from multiflo.models import (
    CassetteType,
    PeristalticDispense,
    PeristalticPrime,
    PeristalticPurge,
    Shake,
)
from multiflo.transport import ScriptedFakeTransport


def response(command: int = 0x0073, message_id: int = 0, body: bytes = b"\x00\x00") -> bytes:
    return bytes((0x06,)) + Frame(
        MessageClass.REQUEST,
        0,
        command,
        0,
        0,
        body,
    ).encode()


class DriverTests(unittest.TestCase):
    def test_inventory_uses_peristaltic_only_machine_profile(self) -> None:
        version = b"7210200" + b"1.12    " + b"ABFB" + b"61FF" + b"103  " + b"002" + b"003"
        packets = [
            response(0x0100, body=b"\x00\x00" + b"14071419   \x00"),
            response(0x00A0, body=b"\x00\x00" + version),
            response(0x0104, body=b"\x00\x00\x01"),
            response(0x0104, body=b"\x00\x00\x00"),
            response(0x0154, body=b"\x00\x00\x01"),
            response(0x0108, body=b"\x00\x00\x02"),
        ]
        expected_writes = [
            encode_request(0x0100, 0),
            encode_request(0x00A0, 1),
            encode_request(0x0104, 2, b"\x01"),
            encode_request(0x0104, 3, b"\x02"),
            encode_request(0x0154, 4),
            encode_request(0x0108, 5, b"\x01"),
        ]
        fake = ScriptedFakeTransport(packets, expected_writes=expected_writes)
        with MultiFloDriver(fake) as driver:
            inventory = driver.inspect_device()
        self.assertTrue(inventory.modules.primary_peristaltic)
        self.assertFalse(inventory.modules.secondary_peristaltic)
        self.assertTrue(inventory.modules.half_microliter_supported)
        self.assertEqual(inventory.modules.primary_cassette, CassetteType.FIVE_UL)
        self.assertFalse(hasattr(inventory.modules, "syringe_manifold"))
        fake.assert_script_consumed()

    def test_motion_is_serial_and_cassette_guarded(self) -> None:
        version = b"7210200" + b"1.12    " + b"ABFB" + b"61FF" + b"103  " + b"002" + b"003"
        step = PeristalticDispense(volume_ul=100, cassette_type="5ul")
        reads = [
            response(0x0073),
            response(0x0100, body=b"\x00\x00" + b"14071419\x00"),
            response(0x00A0, body=b"\x00\x00" + version),
            response(0x0104, body=b"\x00\x00\x01"),
            response(0x0104, body=b"\x00\x00\x00"),
            response(0x0154, body=b"\x00\x00\x01"),
            response(0x0108, body=b"\x00\x00\x02"),
            response(0x008F),
        ]
        writes = [
            encode_request(0x0073, 0),
            encode_request(0x0100, 1),
            encode_request(0x00A0, 2),
            encode_request(0x0104, 3, b"\x01"),
            encode_request(0x0104, 4, b"\x02"),
            encode_request(0x0154, 5),
            encode_request(0x0108, 6, b"\x01"),
            encode_request(0x008F, 7, encode_peristaltic_dispense(step)),
        ]
        fake = ScriptedFakeTransport(reads, expected_writes=writes)
        with MultiFloDriver(fake, expected_product_serial="14071419") as driver:
            driver.authorize_motion(operator_confirmed_idle=True)
            driver.prepare_motion()
            driver.peristaltic_dispense(step)
        fake.assert_script_consumed()

    def test_motion_timeout_has_unknown_execution_state(self) -> None:
        driver = MultiFloDriver(ScriptedFakeTransport())
        driver._is_open = True
        driver._operator_confirmed_idle = True
        driver._motion_preflight = ReadOnlyDeviceInfo(
            "14071419",
            BasecodeVersionInfo("", "", "", "", "", "", "", b""),
            InstalledModules(True, False, True, CassetteType.FIVE_UL),
        )
        with self.assertRaises(UnknownExecutionState):
            driver.peristaltic_dispense(PeristalticDispense(volume_ul=100))

    def test_phase3_motion_commands_use_recovered_bodies(self) -> None:
        prime = PeristalticPrime(volume_ul=3000)
        purge = PeristalticPurge(volume_ul=2000)
        shake = Shake(duration_seconds=5)
        fake = ScriptedFakeTransport(
            [response(0x0090), response(0x0091), response(0x00A3)],
            expected_writes=[
                encode_request(0x0090, 0, encode_peristaltic_prime(prime)),
                encode_request(0x0091, 1, encode_peristaltic_purge(purge)),
                encode_request(0x00A3, 2, encode_shake(shake)),
            ],
        )
        with MultiFloDriver(fake) as driver:
            driver._motion_preflight = ReadOnlyDeviceInfo(
                "14071419",
                BasecodeVersionInfo("", "", "", "", "", "", "", b""),
                InstalledModules(True, False, True, CassetteType.FIVE_UL),
            )
            driver.peristaltic_prime(prime)
            driver.peristaltic_purge(purge)
            driver.shake(shake)
        fake.assert_script_consumed()

    def test_fragmented_success(self) -> None:
        packet = response(body=b"\x00\x00")
        fake = ScriptedFakeTransport(
            [packet[:2], packet[2:7], packet[7:]],
            expected_writes=[encode_request(0x0073)],
        )
        with MultiFloDriver(fake) as driver:
            result = driver.communication_test()
        self.assertEqual(result.response.body, b"\x00\x00")
        fake.assert_script_consumed()

    def test_timeout(self) -> None:
        fake = ScriptedFakeTransport([], expected_writes=[encode_request(0x0073)])
        with MultiFloDriver(fake) as driver:
            with self.assertRaisesRegex(TransportError, "timed out"):
                driver.communication_test()

    def test_nak_is_a_device_error(self) -> None:
        fake = ScriptedFakeTransport([b"\x15"])
        with MultiFloDriver(fake) as driver:
            with self.assertRaisesRegex(DeviceError, "NAK"):
                driver.communication_test()

    def test_nonzero_device_status(self) -> None:
        fake = ScriptedFakeTransport([response(body=b"\x07\x81")])
        with MultiFloDriver(fake) as driver:
            with self.assertRaisesRegex(DeviceError, "0x8107"):
                driver.communication_test()

    def test_disconnect(self) -> None:
        fake = ScriptedFakeTransport(
            [response()[:5], TransportError("device disconnected")],
            expected_writes=[encode_request(0x0073)],
        )
        with MultiFloDriver(fake) as driver:
            with self.assertRaisesRegex(TransportError, "disconnected"):
                driver.communication_test()

    def test_bad_response_checksum(self) -> None:
        packet = bytearray(response())
        packet[10] ^= 1
        fake = ScriptedFakeTransport([bytes(packet)])
        with MultiFloDriver(fake) as driver:
            with self.assertRaisesRegex(ProtocolError, "checksum"):
                driver.communication_test()

    def test_mismatched_command(self) -> None:
        fake = ScriptedFakeTransport([response(command=0x0074)])
        with MultiFloDriver(fake) as driver:
            with self.assertRaisesRegex(ProtocolError, "does not match"):
                driver.communication_test()

    def test_indication_before_response(self) -> None:
        indication = Frame(
            MessageClass.INDICATION,
            Endpoint.PC,
            0x0101,
            Endpoint.INSTRUMENT,
            0,
            b"status",
        ).encode()
        planned_response = Frame(
            MessageClass.RESPONSE,
            Endpoint.PC,
            0x0073,
            Endpoint.INSTRUMENT,
            0,
            b"\x00\x00",
        ).encode()
        fake = ScriptedFakeTransport([bytes((0x06,)), indication, planned_response])
        with MultiFloDriver(fake) as driver:
            result = driver.communication_test()
        self.assertEqual(len(result.indications), 1)


if __name__ == "__main__":
    unittest.main()
