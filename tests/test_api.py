from __future__ import annotations

import time
import unittest

from fastapi.testclient import TestClient

from multiflo.api import create_app
from multiflo.codec import (
    Frame,
    MessageClass,
    encode_peristaltic_dispense,
    encode_peristaltic_prime,
    encode_peristaltic_purge,
    encode_request,
    encode_shake,
    encode_soak,
)
from multiflo.driver import MultiFloDriver
from multiflo.models import (
    PeristalticDispense,
    PeristalticPrime,
    PeristalticPurge,
    Shake,
    Soak,
)
from multiflo.runner import ProtocolRunner
from multiflo.transport import ScriptedFakeTransport


def response(command: int, body: bytes = b"\x00\x00") -> bytes:
    return bytes((0x06,)) + Frame(MessageClass.REQUEST, 0, command, 0, 0, body).encode()


class ApiTests(unittest.TestCase):
    @staticmethod
    def _inventory_reads() -> list[bytes]:
        version = b"7210200" + b"1.12    " + b"ABFB" + b"61FF" + b"103  " + b"002" + b"003"
        return [
            response(0x0073),
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
            encode_request(0x0100, 1),
            encode_request(0x00A0, 2),
            encode_request(0x0104, 3, b"\x01"),
            encode_request(0x0104, 4, b"\x02"),
            encode_request(0x0154, 5),
            encode_request(0x0108, 6, b"\x01"),
        ]

    def test_json_to_completed_dispense_and_polling(self) -> None:
        version = b"7210200" + b"1.12    " + b"ABFB" + b"61FF" + b"103  " + b"002" + b"003"
        step = PeristalticDispense(volume_ul=100, cassette_type="5ul")
        fake = ScriptedFakeTransport(
            [
                response(0x0073),
                response(0x0100, b"\x00\x00" + b"14071419\x00"),
                response(0x00A0, b"\x00\x00" + version),
                response(0x0104, b"\x00\x00\x01"),
                response(0x0104, b"\x00\x00\x00"),
                response(0x0154, b"\x00\x00\x01"),
                response(0x0108, b"\x00\x00\x02"),
                response(0x008F),
            ],
            expected_writes=[
                encode_request(0x0073, 0),
                encode_request(0x0100, 1),
                encode_request(0x00A0, 2),
                encode_request(0x0104, 3, b"\x01"),
                encode_request(0x0104, 4, b"\x02"),
                encode_request(0x0154, 5),
                encode_request(0x0108, 6, b"\x01"),
                encode_request(0x008F, 7, encode_peristaltic_dispense(step)),
            ],
        )
        runner = ProtocolRunner(
            MultiFloDriver(fake, expected_product_serial="14071419")
        )
        try:
            client = TestClient(create_app(runner))
            payload = {
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
        runner = ProtocolRunner(MultiFloDriver(fake, expected_product_serial="14071419"))
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

    def test_run_requires_literal_operator_confirmation(self) -> None:
        runner = ProtocolRunner(
            MultiFloDriver(ScriptedFakeTransport(), expected_product_serial="14071419")
        )
        try:
            client = TestClient(create_app(runner))
            result = client.post(
                "/v1/runs",
                json={
                    "protocol": {"name": "test", "steps": [{"volume_ul": 100}]},
                    "operator_confirmed_idle": False,
                },
            )
            self.assertEqual(result.status_code, 422)
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
                response(0x0090),
                response(0x0091),
                response(0x00A3),
                response(0x00A3),
            ],
            expected_writes=self._inventory_writes()
            + [
                encode_request(0x0090, 7, encode_peristaltic_prime(steps[0])),
                encode_request(0x0091, 8, encode_peristaltic_purge(steps[1])),
                encode_request(0x00A3, 9, encode_shake(steps[2])),
                encode_request(0x00A3, 10, encode_soak(steps[3])),
            ],
        )
        runner = ProtocolRunner(
            MultiFloDriver(fake, expected_product_serial="14071419")
        )
        try:
            client = TestClient(create_app(runner))
            started = client.post(
                "/v1/runs",
                json={
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


if __name__ == "__main__":
    unittest.main()
