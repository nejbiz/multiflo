from __future__ import annotations

import unittest

from multiflo.codec import (
    Endpoint,
    Frame,
    MessageClass,
    encode_batch_start,
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
    ProgramStepState,
    ReadOnlyDeviceInfo,
)
from multiflo.errors import (
    DeviceError,
    PreconditionError,
    ProtocolError,
    TransportError,
    UnknownExecutionState,
)
from multiflo.models import (
    CassetteType,
    PeristalticDispense,
    PeristalticPrime,
    PeristalticPurge,
    Protocol,
    Shake,
)
from tests.fakes import ScriptedFakeTransport


def response(command: int = 0x0073, message_id: int = 0, body: bytes = b"\x00\x00") -> bytes:
    return bytes((0x06,)) + Frame(
        MessageClass.REQUEST,
        0,
        command,
        0,
        0,
        body,
    ).encode()


def program_status_response(
    state: ProgramStepState,
    *,
    error_code: int = 0,
    error_source: int = 2,
) -> bytes:
    body = (
        b"\x00\x00"
        + int(state).to_bytes(2, "little")
        + error_code.to_bytes(4, "little")
        + bytes((error_source,))
    )
    return response(0x0092, body=body)


class DriverTests(unittest.TestCase):
    def test_whole_protocol_is_checked_against_inventory_before_motion(self) -> None:
        driver = MultiFloDriver(ScriptedFakeTransport())
        driver._motion_preflight = ReadOnlyDeviceInfo(
            "14071419",
            BasecodeVersionInfo("", "", "", "", "", "", "", b""),
            InstalledModules(True, False, True, CassetteType.FIVE_UL),
        )

        with self.assertRaisesRegex(PreconditionError, "step 1 requires 1ul"):
            driver.validate_protocol(
                Protocol(
                    name="cassette conflict",
                    steps=[
                        PeristalticDispense(
                            volume_ul=100,
                            cassette_type="5ul",
                            plate_type="96_well",
                        ),
                        PeristalticPrime(volume_ul=100, cassette_type="1ul"),
                    ],
                )
            )

        with self.assertRaisesRegex(PreconditionError, "step 0.*between 5 and 2500"):
            driver.validate_protocol(
                Protocol(
                    name="resolved any cassette",
                    steps=[PeristalticDispense(volume_ul=3000)],
                )
            )

        with self.assertRaisesRegex(
            PreconditionError,
            "step 0 uses a 384-well plate.*requires a 1ul cassette",
        ):
            driver.validate_protocol(
                Protocol(
                    name="384 cassette preflight",
                    steps=[
                        PeristalticDispense(
                            volume_ul=100,
                            plate_type="384_well",
                        )
                    ],
                )
            )

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

    def test_phase6_dispense_limits_are_resolved_against_inventory(self) -> None:
        driver = MultiFloDriver(ScriptedFakeTransport())

        def set_installed(cassette: CassetteType) -> None:
            driver._motion_preflight = ReadOnlyDeviceInfo(
                "14071419",
                BasecodeVersionInfo("", "", "", "", "", "", "", b""),
                InstalledModules(True, False, True, cassette),
            )

        set_installed(CassetteType.ONE_UL)
        driver.validate_protocol(
            Protocol(
                name="1ul upper boundary",
                steps=[PeristalticDispense(volume_ul=1200)],
            )
        )
        with self.assertRaisesRegex(PreconditionError, "between 1 and 1200"):
            driver.validate_protocol(
                Protocol(
                    name="1ul above boundary",
                    steps=[PeristalticDispense(volume_ul=1201)],
                )
            )

        for plate_type in ("384_well", "384_deep_well"):
            for cassette in (CassetteType.FIVE_UL, CassetteType.TEN_UL):
                with self.subTest(
                    plate_type=plate_type,
                    cassette=cassette.value,
                ):
                    set_installed(cassette)
                    with self.assertRaisesRegex(
                        PreconditionError,
                        "384-well plate.*requires a 1ul cassette",
                    ):
                        driver.validate_protocol(
                            Protocol(
                                name="384 cassette preflight",
                                steps=[
                                    PeristalticDispense(
                                        volume_ul=100,
                                        plate_type=plate_type,
                                    )
                                ],
                            )
                        )

    def test_motion_is_serial_and_cassette_guarded(self) -> None:
        version = b"7210200" + b"1.12    " + b"ABFB" + b"61FF" + b"103  " + b"002" + b"003"
        step = PeristalticDispense(volume_ul=100, cassette_type="5ul")
        reads = [
            response(0x0073),
            program_status_response(ProgramStepState.READY),
            response(0x0100, body=b"\x00\x00" + b"14071419\x00"),
            response(0x00A0, body=b"\x00\x00" + version),
            response(0x0104, body=b"\x00\x00\x01"),
            response(0x0104, body=b"\x00\x00\x00"),
            response(0x0154, body=b"\x00\x00\x01"),
            response(0x0108, body=b"\x00\x00\x02"),
            response(0x008D),
            response(0x008F),
            program_status_response(ProgramStepState.BUSY),
            program_status_response(ProgramStepState.READY),
            response(0x008C),
        ]
        writes = [
            encode_request(0x0073, 0),
            encode_request(0x0092, 1),
            encode_request(0x0100, 2),
            encode_request(0x00A0, 3),
            encode_request(0x0104, 4, b"\x01"),
            encode_request(0x0104, 5, b"\x02"),
            encode_request(0x0154, 6),
            encode_request(0x0108, 7, b"\x01"),
            encode_request(0x008D, 8, encode_batch_start(step.plate_type)),
            encode_request(0x008F, 9, encode_peristaltic_dispense(step)),
            encode_request(0x0092, 10),
            encode_request(0x0092, 11),
            encode_request(0x008C, 12),
        ]
        fake = ScriptedFakeTransport(reads, expected_writes=writes)
        with MultiFloDriver(
            fake,
            expected_product_serial="14071419",
            completion_poll_interval_seconds=0,
        ) as driver:
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
            [
                response(0x008D),
                response(0x0090),
                program_status_response(ProgramStepState.READY),
                response(0x008C),
                response(0x008D),
                response(0x0091),
                program_status_response(ProgramStepState.READY),
                response(0x008C),
                response(0x008D),
                response(0x00A3),
                program_status_response(ProgramStepState.READY),
                response(0x008C),
            ],
            expected_writes=[
                encode_request(0x008D, 0, encode_batch_start(prime.plate_type)),
                encode_request(0x0090, 1, encode_peristaltic_prime(prime)),
                encode_request(0x0092, 2),
                encode_request(0x008C, 3),
                encode_request(0x008D, 4, encode_batch_start(purge.plate_type)),
                encode_request(0x0091, 5, encode_peristaltic_purge(purge)),
                encode_request(0x0092, 6),
                encode_request(0x008C, 7),
                encode_request(0x008D, 8, encode_batch_start(shake.plate_type)),
                encode_request(0x00A3, 9, encode_shake(shake)),
                encode_request(0x0092, 10),
                encode_request(0x008C, 11),
            ],
        )
        with MultiFloDriver(fake, completion_poll_interval_seconds=0) as driver:
            driver._motion_preflight = ReadOnlyDeviceInfo(
                "14071419",
                BasecodeVersionInfo("", "", "", "", "", "", "", b""),
                InstalledModules(True, False, True, CassetteType.FIVE_UL),
            )
            driver.peristaltic_prime(prime)
            driver.peristaltic_purge(purge)
            driver.shake(shake)
        fake.assert_script_consumed()

    def test_calib25_384_odd_rows_uses_recovered_batch_and_body(self) -> None:
        step = PeristalticDispense(
            volume_ul=10,
            plate_type="384_well",
            flow_rate="high",
            cassette_type="1ul",
            row_sections="odd",
        )
        fake = ScriptedFakeTransport(
            [
                response(0x008D),
                response(0x008F),
                program_status_response(ProgramStepState.READY),
                response(0x008C),
            ],
            expected_writes=[
                encode_request(0x008D, 0, b"\x01"),
                encode_request(0x008F, 1, encode_peristaltic_dispense(step)),
                encode_request(0x0092, 2),
                encode_request(0x008C, 3),
            ],
        )
        with MultiFloDriver(fake, completion_poll_interval_seconds=0) as driver:
            driver._motion_preflight = ReadOnlyDeviceInfo(
                "14071419",
                BasecodeVersionInfo("", "", "", "", "", "", "", b""),
                InstalledModules(True, False, True, CassetteType.ONE_UL),
            )
            driver.peristaltic_dispense(step)
        fake.assert_script_consumed()

    def test_program_step_status_decoding_and_error_state(self) -> None:
        fake = ScriptedFakeTransport(
            [
                program_status_response(ProgramStepState.PAUSED),
                program_status_response(
                    ProgramStepState.ERROR,
                    error_code=6087,
                    error_source=3,
                ),
            ],
            expected_writes=[encode_request(0x0092, 0), encode_request(0x0092, 1)],
        )
        with MultiFloDriver(fake) as driver:
            paused = driver.query_program_step_status()
            error = driver.query_program_step_status()
        self.assertEqual(paused.state, ProgramStepState.PAUSED)
        self.assertEqual(error.state, ProgramStepState.ERROR)
        self.assertEqual(error.error_code, 6087)
        self.assertEqual(error.error_source, 3)
        fake.assert_script_consumed()

    def test_preflight_rejects_device_busy_state(self) -> None:
        version = b"7210200" + b"1.12    " + b"ABFB" + b"61FF" + b"103  " + b"002" + b"003"
        reads = [
            response(0x0073),
            program_status_response(ProgramStepState.BUSY),
        ]
        fake = ScriptedFakeTransport(reads)
        with MultiFloDriver(fake, expected_product_serial="14071419") as driver:
            driver.authorize_motion(operator_confirmed_idle=True)
            with self.assertRaisesRegex(PreconditionError, "busy, not ready"):
                driver.prepare_motion()

    def test_end_batch_recovery_is_busy_only_and_sent_once(self) -> None:
        fake = ScriptedFakeTransport(
            [
                program_status_response(ProgramStepState.BUSY),
                response(0x008C),
                program_status_response(ProgramStepState.READY, error_source=0),
            ],
            expected_writes=[
                encode_request(0x0092, 0),
                encode_request(0x008C, 1),
                encode_request(0x0092, 2),
            ],
        )
        with MultiFloDriver(fake) as driver:
            recovered = driver.recover_end_batch(operator_confirmed_stationary=True)
        self.assertEqual(recovered.before.state, ProgramStepState.BUSY)
        self.assertEqual(recovered.after.state, ProgramStepState.READY)
        fake.assert_script_consumed()

        ready_fake = ScriptedFakeTransport(
            [program_status_response(ProgramStepState.READY)],
            expected_writes=[encode_request(0x0092, 0)],
        )
        with MultiFloDriver(ready_fake) as driver:
            with self.assertRaisesRegex(PreconditionError, "requires busy state"):
                driver.recover_end_batch(operator_confirmed_stationary=True)
        ready_fake.assert_script_consumed()

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
        with MultiFloDriver(fake, read_only_attempts=1) as driver:
            with self.assertRaisesRegex(TransportError, "timed out"):
                driver.communication_test()

    def test_nak_is_a_device_error(self) -> None:
        fake = ScriptedFakeTransport([b"\x15"])
        with MultiFloDriver(fake, read_only_attempts=1) as driver:
            with self.assertRaisesRegex(DeviceError, "NAK"):
                driver.communication_test()

    def test_nonzero_device_status(self) -> None:
        fake = ScriptedFakeTransport([response(body=b"\x07\x81")])
        with MultiFloDriver(fake, read_only_attempts=1) as driver:
            with self.assertRaisesRegex(DeviceError, "0x8107"):
                driver.communication_test()

    def test_disconnect(self) -> None:
        fake = ScriptedFakeTransport(
            [response()[:5], TransportError("device disconnected")],
            expected_writes=[encode_request(0x0073)],
        )
        with MultiFloDriver(fake, read_only_attempts=1) as driver:
            with self.assertRaisesRegex(TransportError, "disconnected"):
                driver.communication_test()

    def test_bad_response_checksum(self) -> None:
        packet = bytearray(response())
        packet[10] ^= 1
        fake = ScriptedFakeTransport([bytes(packet)])
        with MultiFloDriver(fake, read_only_attempts=1) as driver:
            with self.assertRaisesRegex(ProtocolError, "checksum"):
                driver.communication_test()

    def test_mismatched_command(self) -> None:
        fake = ScriptedFakeTransport([response(command=0x0074)])
        with MultiFloDriver(fake, read_only_attempts=1) as driver:
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
        with MultiFloDriver(fake, read_only_attempts=1) as driver:
            result = driver.communication_test()
        self.assertEqual(len(result.indications), 1)


if __name__ == "__main__":
    unittest.main()
