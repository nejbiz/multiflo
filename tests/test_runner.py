from __future__ import annotations

import time
import unittest
from threading import Event

from multiflo.codec import Frame, MessageClass
from multiflo.driver import ExchangeResult
from multiflo.models import PeristalticDispense, Protocol
from multiflo.runner import ProtocolRunner, RunState


class BlockingDriver:
    def __init__(self) -> None:
        self.motion_started = Event()
        self.release_motion = Event()

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.release_motion.set()

    def authorize_motion(self, *, operator_confirmed_idle: bool) -> None:
        if not operator_confirmed_idle:
            raise RuntimeError("confirmation missing")

    def prepare_motion(self) -> None:
        pass

    def peristaltic_dispense(self, step: PeristalticDispense) -> ExchangeResult:
        self.motion_started.set()
        self.release_motion.wait(timeout=2)
        return ExchangeResult(
            Frame(MessageClass.REQUEST, 0, 0x008F, 0, 0, b"\x00\x00"),
            (),
        )


class RunnerTests(unittest.TestCase):
    def test_abort_is_cooperative_and_does_not_cancel_in_flight_motion(self) -> None:
        driver = BlockingDriver()
        runner = ProtocolRunner(driver)  # type: ignore[arg-type]
        protocol = Protocol(name="one step", steps=[PeristalticDispense(volume_ul=100)])
        try:
            started = runner.start(protocol, operator_confirmed_idle=True)
            self.assertTrue(driver.motion_started.wait(timeout=1))
            aborting = runner.abort(started.run_id)
            self.assertIsNotNone(aborting)
            self.assertEqual(aborting.state, RunState.ABORTING)
            self.assertEqual(aborting.completed_steps, 0)
            driver.release_motion.set()
            for _ in range(100):
                final = runner.get(started.run_id)
                if final is not None and final.state is RunState.ABORTED:
                    break
                time.sleep(0.01)
            self.assertEqual(final.state, RunState.ABORTED)
            self.assertEqual(final.completed_steps, 1)
        finally:
            runner.shutdown()


if __name__ == "__main__":
    unittest.main()
