from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from multiflo.api import create_app
from multiflo.codec import (
    Frame,
    MessageClass,
    encode_batch_start,
    encode_peristaltic_dispense,
    encode_peristaltic_prime,
    encode_peristaltic_purge,
    encode_request,
    encode_shake,
    encode_soak,
)
from multiflo.driver import MultiFloDriver, ProgramStepState
from multiflo.models import (
    PeristalticDispense,
    PeristalticPrime,
    PeristalticPurge,
    Shake,
    Soak,
)
from multiflo.runner import ProtocolRunner
from multiflo.tests.fakes import ScriptedFakeTransport, write_marker


def response(command: int, body: bytes = b"\x00\x00") -> bytes:
    return bytes((0x06,)) + Frame(MessageClass.REQUEST, 0, command, 0, 0, body).encode()


def program_status_response(state: ProgramStepState) -> bytes:
    return response(
        0x0092,
        b"\x00\x00" + int(state).to_bytes(2, "little") + b"\x00" * 4 + b"\x02",
    )


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        # Every runner gets an isolated marker so tests never inherit or leave
        # an active-run marker in the working directory.
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.marker_path = Path(directory.name) / "active-run.json"

    @staticmethod
    def _inventory_reads() -> list[bytes]:
        version = b"7210200" + b"1.12    " + b"ABFB" + b"61FF" + b"103  " + b"002" + b"003"
        return [
            response(0x0073),
            program_status_response(ProgramStepState.READY),
            response(0x0100, b"\x00\x00" + b"14071419\x00"),
            response(0x00A0, b"\x00\x00" + version),
            response(0x0104, b"\x00\x00\x01"),
            response(0x0104, b"\x00\x00\x00"),
            response(0x0154, b"\x00\x00\x01"),
            response(0x0108, b"\x00\x00\x02"),
        ]

    @staticmethod
    def _inventory_writes() -> list[bytes]:
        return [
            encode_request(0x0073, 0),
            encode_request(0x0092, 1),
            encode_request(0x0100, 2),
            encode_request(0x00A0, 3),
            encode_request(0x0104, 4, b"\x01"),
            encode_request(0x0104, 5, b"\x02"),
            encode_request(0x0154, 6),
            encode_request(0x0108, 7, b"\x01"),
        ]

    def test_json_to_completed_dispense_and_polling(self) -> None:
        version = b"7210200" + b"1.12    " + b"ABFB" + b"61FF" + b"103  " + b"002" + b"003"
        step = PeristalticDispense(volume_ul=100, cassette_type="5ul")
        fake = ScriptedFakeTransport(
            [
                response(0x0073),
                program_status_response(ProgramStepState.READY),
                response(0x0100, b"\x00\x00" + b"14071419\x00"),
                response(0x00A0, b"\x00\x00" + version),
                response(0x0104, b"\x00\x00\x01"),
                response(0x0104, b"\x00\x00\x00"),
                response(0x0154, b"\x00\x00\x01"),
                response(0x0108, b"\x00\x00\x02"),
                response(0x008D),
                response(0x008F),
                program_status_response(ProgramStepState.READY),
                response(0x008C),
            ],
            expected_writes=[
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
                encode_request(0x008C, 11),
            ],
        )
        runner = ProtocolRunner(
            MultiFloDriver(
                fake,
                expected_product_serial="14071419",
                completion_poll_interval_seconds=0,
            ),
            crash_marker_path=self.marker_path,
        )
        try:
            client = TestClient(create_app(runner))
            payload = {
                "request_id": "calib1-slice-1",
                "protocol": {
                    "name": "calib1 vertical slice",
                    "steps": [step.model_dump(mode="json")],
                },
                "operator_confirmed_idle": True,
            }
            started = client.post("/v1/runs", json=payload)
            self.assertEqual(started.status_code, 202)
            run_id = started.json()["run_id"]
            for _ in range(100):
                polled = client.get(f"/v1/runs/{run_id}")
                self.assertEqual(polled.status_code, 200)
                if polled.json()["state"] in {
                    "completed",
                    "failed",
                    "unknown_execution_state",
                }:
                    break
                time.sleep(0.01)
            self.assertEqual(polled.json()["state"], "completed")
            self.assertEqual(polled.json()["completed_steps"], 1)
            self.assertTrue(polled.json()["abort_is_cooperative"])
            self.assertEqual(
                polled.json()["results"],
                [
                    {
                        "step_index": 0,
                        "operation": "peristaltic_dispense",
                        "device_status": 0,
                        "indication_count": 0,
                    }
                ],
            )
            fake.assert_script_consumed()
        finally:
            runner.shutdown()

    def test_validation_rejects_unsafe_shape_without_opening_transport(self) -> None:
        fake = ScriptedFakeTransport()
        runner = ProtocolRunner(
            MultiFloDriver(fake, expected_product_serial="14071419"),
            crash_marker_path=self.marker_path,
        )
        try:
            client = TestClient(create_app(runner))
            result = client.post(
                "/v1/protocols/validate",
                json={"name": "bad", "steps": [{"volume_ul": 0}]},
            )
            self.assertEqual(result.status_code, 422)
            self.assertFalse(fake.is_open)
            self.assertEqual(fake.writes, [])
        finally:
            runner.shutdown()

    def test_calib25_384_odd_rows_validates_without_opening_transport(self) -> None:
        fake = ScriptedFakeTransport()
        runner = ProtocolRunner(
            MultiFloDriver(fake, expected_product_serial="14071419"),
            crash_marker_path=self.marker_path,
        )
        try:
            client = TestClient(create_app(runner))
            result = client.post(
                "/v1/protocols/validate",
                json={
                    "name": "calib25",
                    "steps": [
                        {
                            "operation": "peristaltic_dispense",
                            "plate_type": "384_well",
                            "volume_ul": 10,
                            "flow_rate": "high",
                            "cassette_type": "1ul",
                            "row_sections": "odd",
                        }
                    ],
                },
            )
            self.assertEqual(result.status_code, 200)
            self.assertEqual(
                result.json()["protocol"]["steps"][0]["row_sections"],
                "odd",
            )
            self.assertFalse(fake.is_open)
            self.assertEqual(fake.writes, [])
        finally:
            runner.shutdown()

    def test_calib21_unsafe_volume_is_rejected_by_api(self) -> None:
        fake = ScriptedFakeTransport()
        runner = ProtocolRunner(
            MultiFloDriver(fake, expected_product_serial="14071419"),
            crash_marker_path=self.marker_path,
        )
        try:
            client = TestClient(create_app(runner))
            result = client.post(
                "/v1/protocols/validate",
                json={
                    "name": "calib21",
                    "steps": [
                        {
                            "operation": "peristaltic_dispense",
                            "plate_type": "96_well",
                            "volume_ul": 750,
                            "flow_rate": "high",
                            "cassette_type": "1ul",
                        }
                    ],
                },
            )
            self.assertEqual(result.status_code, 422)
            self.assertFalse(fake.is_open)
            self.assertEqual(fake.writes, [])
        finally:
            runner.shutdown()

    def test_run_requires_literal_operator_confirmation(self) -> None:
        runner = ProtocolRunner(
            MultiFloDriver(ScriptedFakeTransport(), expected_product_serial="14071419"),
            crash_marker_path=self.marker_path,
        )
        try:
            client = TestClient(create_app(runner))
            result = client.post(
                "/v1/runs",
                json={
                    "request_id": "unconfirmed-1",
                    "protocol": {"name": "test", "steps": [{"volume_ul": 100}]},
                    "operator_confirmed_idle": False,
                },
            )
            self.assertEqual(result.status_code, 422)
        finally:
            runner.shutdown()

    def test_retained_crash_marker_blocks_api_motion(self) -> None:
        with TemporaryDirectory() as directory:
            marker_path = Path(directory) / "active-run.json"
            write_marker(marker_path)
            fake = ScriptedFakeTransport()
            runner = ProtocolRunner(
                MultiFloDriver(fake, expected_product_serial="14071419"),
                crash_marker_path=marker_path,
            )
            try:
                client = TestClient(create_app(runner))
                result = client.post(
                    "/v1/runs",
                    json={
                        "request_id": "blocked-after-restart",
                        "protocol": {
                            "name": "blocked after restart",
                            "steps": [
                                {
                                    "operation": "peristaltic_dispense",
                                    "volume_ul": 100,
                                }
                            ],
                        },
                        "operator_confirmed_idle": True,
                    },
                )
                self.assertEqual(result.status_code, 409)
                self.assertIn("reconciliation", result.json()["detail"])
                self.assertFalse(fake.is_open)
                self.assertEqual(fake.writes, [])
            finally:
                runner.shutdown()

    def test_phase3_json_steps_run_through_fastapi(self) -> None:
        steps = [
            PeristalticPrime(volume_ul=3000),
            PeristalticPurge(volume_ul=2000),
            Shake(duration_seconds=5),
            Soak(duration_seconds=30),
        ]
        fake = ScriptedFakeTransport(
            self._inventory_reads()
            + [
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
                response(0x008D),
                response(0x00A3),
                program_status_response(ProgramStepState.READY),
                response(0x008C),
            ],
            expected_writes=self._inventory_writes()
            + [
                encode_request(0x008D, 8, encode_batch_start(steps[0].plate_type)),
                encode_request(0x0090, 9, encode_peristaltic_prime(steps[0])),
                encode_request(0x0092, 10),
                encode_request(0x008C, 11),
                encode_request(0x008D, 12, encode_batch_start(steps[1].plate_type)),
                encode_request(0x0091, 13, encode_peristaltic_purge(steps[1])),
                encode_request(0x0092, 14),
                encode_request(0x008C, 15),
                encode_request(0x008D, 16, encode_batch_start(steps[2].plate_type)),
                encode_request(0x00A3, 17, encode_shake(steps[2])),
                encode_request(0x0092, 18),
                encode_request(0x008C, 19),
                encode_request(0x008D, 20, encode_batch_start(steps[3].plate_type)),
                encode_request(0x00A3, 21, encode_soak(steps[3])),
                encode_request(0x0092, 22),
                encode_request(0x008C, 23),
            ],
        )
        runner = ProtocolRunner(
            MultiFloDriver(
                fake,
                expected_product_serial="14071419",
                completion_poll_interval_seconds=0,
            ),
            crash_marker_path=self.marker_path,
        )
        try:
            client = TestClient(create_app(runner))
            started = client.post(
                "/v1/runs",
                json={
                    "request_id": "phase3-mixed-1",
                    "protocol": {
                        "name": "phase 3 fake vertical slices",
                        "steps": [step.model_dump(mode="json") for step in steps],
                    },
                    "operator_confirmed_idle": True,
                },
            )
            self.assertEqual(started.status_code, 202)
            run_id = started.json()["run_id"]
            for _ in range(100):
                result = client.get(f"/v1/runs/{run_id}").json()
                if result["state"] in {
                    "completed",
                    "failed",
                    "unknown_execution_state",
                }:
                    break
                time.sleep(0.01)
            self.assertEqual(result["state"], "completed")
            self.assertEqual(
                [item["operation"] for item in result["results"]],
                [
                    "peristaltic_prime",
                    "peristaltic_purge",
                    "shake",
                    "soak",
                ],
            )
            fake.assert_script_consumed()
        finally:
            runner.shutdown()

    def test_mixed_protocol_inventory_conflict_fails_before_first_motion(self) -> None:
        fake = ScriptedFakeTransport(
            self._inventory_reads(),
            expected_writes=self._inventory_writes(),
        )
        runner = ProtocolRunner(
            MultiFloDriver(
                fake,
                expected_product_serial="14071419",
                completion_poll_interval_seconds=0,
            ),
            crash_marker_path=self.marker_path,
        )
        try:
            client = TestClient(create_app(runner))
            started = client.post(
                "/v1/runs",
                json={
                    "request_id": "mixed-cassette-conflict",
                    "protocol": {
                        "name": "incompatible mixed cassettes",
                        "steps": [
                            {
                                "operation": "peristaltic_dispense",
                                "volume_ul": 100,
                                "cassette_type": "5ul",
                                "plate_type": "96_well",
                            },
                            {
                                "operation": "peristaltic_prime",
                                "volume_ul": 100,
                                "cassette_type": "1ul",
                            },
                        ],
                    },
                    "operator_confirmed_idle": True,
                },
            )
            self.assertEqual(started.status_code, 202)
            run_id = started.json()["run_id"]
            for _ in range(100):
                result = client.get(f"/v1/runs/{run_id}").json()
                if result["state"] == "failed":
                    break
                time.sleep(0.01)
            self.assertEqual(result["state"], "failed")
            self.assertIn("step 1 requires 1ul", result["error"])
            self.assertEqual(result["completed_steps"], 0)
            fake.assert_script_consumed()
        finally:
            runner.shutdown()


if __name__ == "__main__":
    unittest.main()
