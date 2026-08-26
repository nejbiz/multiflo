"""Phase 5 contract tests for the finalized FastAPI boundary."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from multiflo.api import create_app
from multiflo.codec import (
    Frame,
    MessageClass,
    encode_batch_start,
    encode_request,
    encode_shake,
)
from multiflo.driver import ExchangeResult, MultiFloDriver, ProgramStepState
from multiflo.errors import TransportError
from multiflo.models import Protocol, Shake
from multiflo.runner import ProtocolRunner
from multiflo.transport import ScriptedFakeTransport


BASECODE = b"7210200" + b"1.12    " + b"ABFB" + b"61FF" + b"103  " + b"002" + b"003"

EXPECTED_ROUTES = {
    ("/v1/health", "get"),
    ("/v1/device", "get"),
    ("/v1/protocols/validate", "post"),
    ("/v1/runs", "post"),
    ("/v1/runs/{run_id}", "get"),
    ("/v1/runs/{run_id}/abort", "post"),
}


def response(command: int, body: bytes = b"\x00\x00") -> bytes:
    return bytes((0x06,)) + Frame(MessageClass.REQUEST, 0, command, 0, 0, body).encode()


def program_status_response(state: ProgramStepState) -> bytes:
    return response(
        0x0092,
        b"\x00\x00" + int(state).to_bytes(2, "little") + b"\x00" * 4 + b"\x02",
    )


def inventory_reads() -> list[bytes]:
    """Reads for one communication test, ready check, and full inventory."""

    return [
        response(0x0073),
        program_status_response(ProgramStepState.READY),
        response(0x0100, b"\x00\x00" + b"14071419\x00"),
        response(0x00A0, b"\x00\x00" + BASECODE),
        response(0x0104, b"\x00\x00\x01"),
        response(0x0104, b"\x00\x00\x00"),
        response(0x0154, b"\x00\x00\x01"),
        response(0x0108, b"\x00\x00\x02"),
    ]


def inventory_writes(first_message_id: int = 0) -> list[bytes]:
    message_id = first_message_id
    writes = []
    for command, body in (
        (0x0073, b""),
        (0x0092, b""),
        (0x0100, b""),
        (0x00A0, b""),
        (0x0104, b"\x01"),
        (0x0104, b"\x02"),
        (0x0154, b""),
        (0x0108, b"\x01"),
    ):
        writes.append(encode_request(command, message_id, body))
        message_id += 1
    return writes


class _StubDriver:
    """Driver stand-in that blocks one motion step until released."""

    expected_product_serial = "14071419"

    def __init__(self) -> None:
        self.motion_started = Event()
        self.release_motion = Event()
        self.motion_calls = 0

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.release_motion.set()

    def authorize_motion(self, *, operator_confirmed_idle: bool) -> None:
        pass

    def prepare_motion(self, *, require_primary_peristaltic: bool = True) -> None:
        pass

    def validate_protocol(self, protocol: Protocol) -> None:
        pass

    def shake(self, step: Shake) -> ExchangeResult:
        self.motion_calls += 1
        self.motion_started.set()
        self.release_motion.wait(timeout=5)
        return ExchangeResult(Frame(MessageClass.REQUEST, 0, 0x00A3, 0, 0, b"\x00\x00"), ())


class _FailingTransport:
    """Transport whose device is absent; opening it always fails."""

    def __init__(self) -> None:
        self.open_attempts = 0
        self.is_open = False
        self.writes: list[bytes] = []

    def open(self) -> None:
        self.open_attempts += 1
        raise TransportError("device with serial 14071419 was not found")

    def close(self) -> None:
        self.is_open = False

    def read(self, size: int) -> bytes:  # pragma: no cover - never reached
        raise TransportError("device is not open")

    def write(self, data: bytes) -> None:  # pragma: no cover - never reached
        raise TransportError("device is not open")


def shake_run_payload(request_id: str, name: str = "phase 5 shake") -> dict:
    return {
        "request_id": request_id,
        "protocol": {
            "name": name,
            "steps": [{"operation": "shake", "duration_seconds": 5}],
        },
        "operator_confirmed_idle": True,
    }


class ApiContractTests(unittest.TestCase):
    def _runner(self, transport, **kwargs) -> ProtocolRunner:
        runner = ProtocolRunner(
            MultiFloDriver(
                transport,
                expected_product_serial="14071419",
                completion_poll_interval_seconds=0,
            ),
            crash_marker_path=self.marker_path,
            **kwargs,
        )
        self.addCleanup(runner.shutdown)
        return runner

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.marker_path = Path(directory.name) / "active-run.json"

    def test_openapi_documents_only_the_supported_surface(self) -> None:
        runner = self._runner(ScriptedFakeTransport())
        client = TestClient(create_app(runner))

        schema = client.get("/openapi.json")
        self.assertEqual(schema.status_code, 200)
        document = schema.json()
        routes = {
            (path, method)
            for path, methods in document["paths"].items()
            for method in methods
        }
        self.assertEqual(routes, EXPECTED_ROUTES)
        flattened = repr(document["paths"]).lower()
        for forbidden in ("raw", "packet", "command", "firmware", "pause", "resume"):
            self.assertNotIn(forbidden, flattened)
        for model in ("StartRunRequest", "Protocol", "RunStatus", "DeviceStatus"):
            self.assertFalse(
                document["components"]["schemas"][model].get("additionalProperties", True),
                f"{model} must reject unknown fields",
            )

    def test_health_reports_process_state_without_commanding_hardware(self) -> None:
        fake = ScriptedFakeTransport()
        runner = self._runner(fake)
        client = TestClient(create_app(runner))

        result = client.get("/v1/health")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(
            result.json(),
            {
                "status": "ok",
                "service": "multiflo",
                "api_version": "1.0.0",
                "controller_state": "disconnected",
                "reconciliation_required": False,
                "active_run_id": None,
            },
        )
        self.assertFalse(fake.is_open)
        self.assertEqual(fake.writes, [])

    def test_health_reports_a_retained_crash_marker(self) -> None:
        self.marker_path.write_text("{}\n", encoding="utf-8")
        runner = self._runner(ScriptedFakeTransport())
        client = TestClient(create_app(runner))

        body = client.get("/v1/health").json()

        self.assertEqual(body["controller_state"], "reconciliation_required")
        self.assertTrue(body["reconciliation_required"])

    def test_device_reports_verified_identity_modules_and_state(self) -> None:
        fake = ScriptedFakeTransport(
            [
                response(0x0100, b"\x00\x00" + b"14071419\x00"),
                response(0x00A0, b"\x00\x00" + BASECODE),
                response(0x0104, b"\x00\x00\x01"),
                response(0x0104, b"\x00\x00\x00"),
                response(0x0154, b"\x00\x00\x01"),
                response(0x0108, b"\x00\x00\x02"),
                program_status_response(ProgramStepState.READY),
            ],
            expected_writes=[
                encode_request(0x0100, 0),
                encode_request(0x00A0, 1),
                encode_request(0x0104, 2, b"\x01"),
                encode_request(0x0104, 3, b"\x02"),
                encode_request(0x0154, 4),
                encode_request(0x0108, 5, b"\x01"),
                encode_request(0x0092, 6),
            ],
        )
        runner = self._runner(fake)
        client = TestClient(create_app(runner))

        result = client.get("/v1/device")

        self.assertEqual(result.status_code, 200)
        body = result.json()
        self.assertTrue(body["connected"])
        self.assertTrue(body["serial_matches_expected"])
        self.assertEqual(body["expected_product_serial"], "14071419")
        self.assertEqual(body["identity"]["product_serial_number"], "14071419")
        self.assertEqual(body["identity"]["part_number"], "7210200")
        self.assertEqual(body["identity"]["software_version"], "1.12")
        self.assertEqual(
            body["modules"],
            {
                "primary_peristaltic": True,
                "secondary_peristaltic": False,
                "half_microliter_supported": True,
                "primary_cassette": "5ul",
            },
        )
        self.assertEqual(body["program_step_state"], "ready")
        self.assertEqual(body["controller_state"], "idle")
        self.assertIsNone(body["error"])
        fake.assert_script_consumed()

    def test_device_reports_a_missing_instrument_without_raising(self) -> None:
        transport = _FailingTransport()
        runner = self._runner(transport)
        client = TestClient(create_app(runner))

        result = client.get("/v1/device")

        self.assertEqual(result.status_code, 200)
        body = result.json()
        self.assertFalse(body["connected"])
        self.assertFalse(body["serial_matches_expected"])
        self.assertIsNone(body["identity"])
        self.assertIsNone(body["modules"])
        self.assertIn("TransportError", body["error"])
        self.assertEqual(body["controller_state"], "disconnected")
        self.assertEqual(transport.open_attempts, 1)

    def test_repeated_request_id_returns_the_same_run_without_new_motion(self) -> None:
        step = Shake(duration_seconds=5)
        fake = ScriptedFakeTransport(
            inventory_reads()
            + [
                response(0x008D),
                response(0x00A3),
                program_status_response(ProgramStepState.READY),
                response(0x008C),
            ],
            expected_writes=inventory_writes()
            + [
                encode_request(0x008D, 8, encode_batch_start(step.plate_type)),
                encode_request(0x00A3, 9, encode_shake(step)),
                encode_request(0x0092, 10),
                encode_request(0x008C, 11),
            ],
        )
        runner = self._runner(fake)
        client = TestClient(create_app(runner))
        payload = shake_run_payload("operator-ticket-4711")

        first = client.post("/v1/runs", json=payload)
        self.assertEqual(first.status_code, 202)
        run_id = first.json()["run_id"]
        self.assertEqual(first.json()["request_id"], "operator-ticket-4711")
        final = self._wait_for_terminal(client, run_id)
        self.assertEqual(final["state"], "completed")

        repeated = client.post("/v1/runs", json=payload)

        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()["run_id"], run_id)
        self.assertEqual(repeated.json()["state"], "completed")
        self.assertEqual(repeated.json()["completed_steps"], 1)
        fake.assert_script_consumed()

    def test_repeated_request_id_with_a_different_protocol_is_a_conflict(self) -> None:
        driver = _StubDriver()
        runner = ProtocolRunner(driver, crash_marker_path=self.marker_path)  # type: ignore[arg-type]
        self.addCleanup(runner.shutdown)
        client = TestClient(create_app(runner))
        driver.release_motion.set()

        first = client.post("/v1/runs", json=shake_run_payload("ticket-9"))
        self.assertEqual(first.status_code, 202)
        self._wait_for_terminal(client, first.json()["run_id"])

        changed = shake_run_payload("ticket-9", name="different protocol")
        conflict = client.post("/v1/runs", json=changed)

        self.assertEqual(conflict.status_code, 409)
        self.assertIn("different protocol", conflict.json()["detail"])
        self.assertEqual(driver.motion_calls, 1)

    def test_starting_while_a_run_is_active_is_a_conflict(self) -> None:
        driver = _StubDriver()
        runner = ProtocolRunner(driver, crash_marker_path=self.marker_path)  # type: ignore[arg-type]
        self.addCleanup(runner.shutdown)
        client = TestClient(create_app(runner))

        started = client.post("/v1/runs", json=shake_run_payload("ticket-first"))
        self.assertEqual(started.status_code, 202)
        self.assertTrue(driver.motion_started.wait(timeout=5))

        conflict = client.post("/v1/runs", json=shake_run_payload("ticket-second"))
        device = client.get("/v1/device")
        health = client.get("/v1/health").json()

        self.assertEqual(conflict.status_code, 409)
        self.assertIn("another protocol run is active", conflict.json()["detail"])
        self.assertEqual(device.status_code, 409)
        self.assertEqual(health["controller_state"], "running")
        self.assertEqual(health["active_run_id"], started.json()["run_id"])

        driver.release_motion.set()
        final = self._wait_for_terminal(client, started.json()["run_id"])
        self.assertEqual(final["state"], "completed")
        self.assertEqual(driver.motion_calls, 1)

    def test_client_disconnect_does_not_alter_execution(self) -> None:
        driver = _StubDriver()
        runner = ProtocolRunner(driver, crash_marker_path=self.marker_path)  # type: ignore[arg-type]
        self.addCleanup(runner.shutdown)
        client = TestClient(create_app(runner))

        started = client.post("/v1/runs", json=shake_run_payload("ticket-disconnect"))
        run_id = started.json()["run_id"]
        self.assertTrue(driver.motion_started.wait(timeout=5))

        # The HTTP client goes away while the step is still in flight.
        client.close()
        driver.release_motion.set()

        final = None
        for _ in range(500):
            final = runner.get(UUID(run_id))
            if final is not None and final.state.value == "completed":
                break
            time.sleep(0.01)
        self.assertIsNotNone(final)
        self.assertEqual(final.state.value, "completed")
        self.assertEqual(driver.motion_calls, 1)

        # A new client sees the same run and its confirmed progress.
        reconnected = TestClient(create_app(runner))
        polled = reconnected.get(f"/v1/runs/{run_id}")
        self.assertEqual(polled.status_code, 200)
        self.assertEqual(polled.json()["state"], "completed")
        self.assertEqual(polled.json()["completed_steps"], 1)

    def test_malformed_request_id_is_rejected_without_touching_hardware(self) -> None:
        fake = ScriptedFakeTransport()
        runner = self._runner(fake)
        client = TestClient(create_app(runner))

        for request_id in ("", "-leading-dash", "has space", "x" * 65):
            with self.subTest(request_id=request_id):
                result = client.post("/v1/runs", json=shake_run_payload(request_id))
                self.assertEqual(result.status_code, 422)
        self.assertFalse(fake.is_open)
        self.assertEqual(fake.writes, [])

    def test_unknown_run_ids_are_not_found(self) -> None:
        runner = self._runner(ScriptedFakeTransport())
        client = TestClient(create_app(runner))
        missing = uuid4()

        self.assertEqual(client.get(f"/v1/runs/{missing}").status_code, 404)
        self.assertEqual(client.post(f"/v1/runs/{missing}/abort").status_code, 404)
        self.assertEqual(client.get("/v1/runs/not-a-uuid").status_code, 422)

    def _wait_for_terminal(self, client: TestClient, run_id: str) -> dict:
        for _ in range(500):
            body = client.get(f"/v1/runs/{run_id}").json()
            if body["state"] in {
                "completed",
                "aborted",
                "failed",
                "unknown_execution_state",
            }:
                return body
            time.sleep(0.01)
        self.fail("run did not reach a terminal state")


if __name__ == "__main__":
    unittest.main()
