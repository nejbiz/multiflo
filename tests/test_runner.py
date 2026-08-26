from __future__ import annotations

from io import StringIO
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from threading import Event

from multiflo.codec import Frame, MessageClass
from multiflo.driver import ExchangeResult, ProgramStepState, ProgramStepStatus
from multiflo.errors import BusyError, UnknownExecutionState
from multiflo.models import PeristalticDispense, Protocol
from multiflo.runner import ControllerState, ProtocolRunner, RunState


class BlockingDriver:
    def __init__(self) -> None:
        self.motion_started = Event()
        self.release_motion = Event()
        self.motion_calls = 0

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.release_motion.set()

    def authorize_motion(self, *, operator_confirmed_idle: bool) -> None:
        if not operator_confirmed_idle:
            raise RuntimeError("confirmation missing")

    def prepare_motion(self, *, require_primary_peristaltic: bool = True) -> None:
        pass

    def validate_protocol(self, protocol: Protocol) -> None:
        pass

    def peristaltic_dispense(self, step: PeristalticDispense) -> ExchangeResult:
        self.motion_calls += 1
        self.motion_started.set()
        self.release_motion.wait(timeout=2)
        return ExchangeResult(
            Frame(MessageClass.REQUEST, 0, 0x008F, 0, 0, b"\x00\x00"),
            (),
        )


class ImmediateDriver(BlockingDriver):
    def peristaltic_dispense(self, step: PeristalticDispense) -> ExchangeResult:
        self.motion_calls += 1
        return ExchangeResult(
            Frame(MessageClass.REQUEST, 0, 0x008F, 0, 0, b"\x00\x00"),
            (),
        )


class SecondStepUnknownDriver(ImmediateDriver):
    def peristaltic_dispense(self, step: PeristalticDispense) -> ExchangeResult:
        self.motion_calls += 1
        if self.motion_calls == 2:
            raise UnknownExecutionState("simulated post-send disconnect")
        return ExchangeResult(
            Frame(MessageClass.REQUEST, 0, 0x008F, 0, 0, b"\x00\x00"),
            (),
        )


class ReconciliationDriver:
    def __init__(self, state: ProgramStepState) -> None:
        self.state = state
        self.query_count = 0

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def query_program_step_status(self) -> ProgramStepStatus:
        self.query_count += 1
        return ProgramStepStatus(self.state, 0, 0)


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.marker_path = Path(self.temporary_directory.name) / "active-run.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def wait_for_terminal(self, runner: ProtocolRunner, run_id) -> object:
        final = None
        for _ in range(100):
            final = runner.get(run_id)
            if final is not None and final.state in {
                RunState.COMPLETED,
                RunState.ABORTED,
                RunState.FAILED,
                RunState.UNKNOWN_EXECUTION_STATE,
            }:
                return final
            time.sleep(0.01)
        self.fail("run did not reach a terminal state")

    def test_abort_is_cooperative_and_does_not_cancel_in_flight_motion(self) -> None:
        driver = BlockingDriver()
        runner = ProtocolRunner(  # type: ignore[arg-type]
            driver,
            crash_marker_path=self.marker_path,
        )
        protocol = Protocol(
            name="two steps",
            steps=[
                PeristalticDispense(volume_ul=100),
                PeristalticDispense(volume_ul=100),
            ],
        )
        try:
            started = runner.start(protocol, operator_confirmed_idle=True)
            self.assertTrue(driver.motion_started.wait(timeout=1))
            self.assertTrue(self.marker_path.exists())
            aborting = runner.abort(started.run_id)
            self.assertIsNotNone(aborting)
            self.assertEqual(aborting.state, RunState.ABORTING)
            self.assertEqual(aborting.completed_steps, 0)
            driver.release_motion.set()
            final = self.wait_for_terminal(runner, started.run_id)
            self.assertEqual(final.state, RunState.ABORTED)
            self.assertEqual(final.completed_steps, 1)
            self.assertEqual(driver.motion_calls, 1)
            self.assertFalse(self.marker_path.exists())
            self.assertEqual(runner.state, ControllerState.IDLE)
        finally:
            runner.shutdown()

    def test_unknown_execution_preserves_marker_and_blocks_restart(self) -> None:
        driver = SecondStepUnknownDriver()
        runner = ProtocolRunner(  # type: ignore[arg-type]
            driver,
            crash_marker_path=self.marker_path,
        )
        protocol = Protocol(
            name="uncertain",
            steps=[
                PeristalticDispense(volume_ul=100),
                PeristalticDispense(volume_ul=100),
            ],
        )
        try:
            started = runner.start(protocol, operator_confirmed_idle=True)
            final = self.wait_for_terminal(runner, started.run_id)
            self.assertEqual(final.state, RunState.UNKNOWN_EXECUTION_STATE)
            self.assertEqual(final.completed_steps, 1)
            self.assertEqual(final.current_step, 1)
            self.assertEqual(runner.state, ControllerState.RECONCILIATION_REQUIRED)
            marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["run_id"], str(started.run_id))
            self.assertEqual(marker["current_step"], 1)
            self.assertEqual(marker["last_confirmed_step"], 0)
        finally:
            runner.shutdown()

        reconciliation_driver = ReconciliationDriver(ProgramStepState.READY)
        restarted = ProtocolRunner(  # type: ignore[arg-type]
            reconciliation_driver,
            crash_marker_path=self.marker_path,
        )
        try:
            self.assertEqual(
                restarted.state,
                ControllerState.RECONCILIATION_REQUIRED,
            )
            with self.assertRaisesRegex(BusyError, "reconciliation"):
                restarted.start(protocol, operator_confirmed_idle=True)
            self.assertEqual(
                restarted.reconcile_startup(),
                ControllerState.IDLE,
            )
            self.assertFalse(self.marker_path.exists())
            self.assertEqual(reconciliation_driver.query_count, 1)
        finally:
            restarted.shutdown()

    def test_non_ready_reconciliation_retains_marker(self) -> None:
        self.marker_path.write_text("{}\n", encoding="utf-8")
        driver = ReconciliationDriver(ProgramStepState.BUSY)
        runner = ProtocolRunner(  # type: ignore[arg-type]
            driver,
            crash_marker_path=self.marker_path,
        )
        try:
            with self.assertRaisesRegex(BusyError, "busy, not ready"):
                runner.reconcile_startup()
            self.assertTrue(self.marker_path.exists())
            self.assertTrue(runner.reconciliation_required)
        finally:
            runner.shutdown()

    def test_operator_acknowledgement_can_clear_startup_marker(self) -> None:
        self.marker_path.write_text("{}\n", encoding="utf-8")
        driver = ReconciliationDriver(ProgramStepState.BUSY)
        runner = ProtocolRunner(  # type: ignore[arg-type]
            driver,
            crash_marker_path=self.marker_path,
        )
        try:
            self.assertEqual(
                runner.reconcile_startup(operator_acknowledged=True),
                ControllerState.DISCONNECTED,
            )
            self.assertEqual(driver.query_count, 0)
            self.assertFalse(self.marker_path.exists())
        finally:
            runner.shutdown()

    def test_structured_logs_include_request_response_and_timestamps(self) -> None:
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger(f"multiflo.runner.test.{id(self)}")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        driver = ImmediateDriver()
        runner = ProtocolRunner(  # type: ignore[arg-type]
            driver,
            crash_marker_path=self.marker_path,
            logger=logger,
        )
        try:
            started = runner.start(
                Protocol(
                    name="logged",
                    steps=[PeristalticDispense(volume_ul=100)],
                ),
                operator_confirmed_idle=True,
            )
            final = self.wait_for_terminal(runner, started.run_id)
            self.assertEqual(final.state, RunState.COMPLETED)
        finally:
            runner.shutdown()
            handler.close()

        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(
            [event["event"] for event in events],
            [
                "run_queued",
                "run_started",
                "step_started",
                "step_completed",
                "run_terminal",
            ],
        )
        by_name = {event["event"]: event for event in events}
        self.assertEqual(by_name["step_started"]["request"]["volume_ul"], 100)
        self.assertEqual(by_name["step_completed"]["response"]["device_status"], 0)
        self.assertEqual(
            by_name["step_completed"]["response"]["completion_status"],
            "ready",
        )
        self.assertEqual(by_name["run_terminal"]["state"], "completed")
        self.assertTrue(all("timestamp" in event for event in events))


if __name__ == "__main__":
    unittest.main()
