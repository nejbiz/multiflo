"""Retry policy: what may be re-sent, and what must never be.

The whole point of the policy is asymmetry. Opening a connection and reading
status move no liquid, so a transient fault there is worth absorbing. A motion
command that may already have been executed is not.
"""

from __future__ import annotations

import unittest

from multiflo.codec import encode_batch_start, encode_request, encode_shake
from multiflo.driver import MultiFloDriver, ProgramStepState
from multiflo.errors import DeviceError, TransportError, UnknownExecutionState
from multiflo.models import Shake
from multiflo.tests.fakes import (
    FailingTransport,
    ScriptedFakeTransport,
    device_info,
    inventory_reads,
    program_status_response,
    response,
)


class ConnectionRetryTests(unittest.TestCase):
    def test_open_retries_a_transient_connection_failure(self) -> None:
        transport = FailingTransport(fail_times=2)
        driver = MultiFloDriver(transport, retry_backoff_seconds=0)

        driver.open()

        self.assertEqual(transport.open_attempts, 3)

    def test_open_gives_up_after_the_attempt_budget(self) -> None:
        transport = FailingTransport()
        driver = MultiFloDriver(transport, open_attempts=3, retry_backoff_seconds=0)

        with self.assertRaisesRegex(TransportError, "was not found"):
            driver.open()

        self.assertEqual(transport.open_attempts, 3)


class ReadOnlyRetryTests(unittest.TestCase):
    def test_read_only_command_is_resent_after_a_wire_fault(self) -> None:
        fake = ScriptedFakeTransport(
            [
                TransportError("USB hiccup"),
                response(0x0073),
            ]
        )
        driver = MultiFloDriver(fake, read_only_attempts=3)
        driver.open()

        driver.communication_test()

        self.assertEqual(len(fake.writes), 2)
        # The stream is purged before the retry so a partial frame cannot
        # shift every subsequent read.
        self.assertEqual(fake.purge_count, 1)

    def test_read_only_command_fails_after_the_attempt_budget(self) -> None:
        fake = ScriptedFakeTransport([TransportError("gone")] * 3)
        driver = MultiFloDriver(fake, read_only_attempts=3)
        driver.open()

        with self.assertRaises(TransportError):
            driver.communication_test()

        self.assertEqual(len(fake.writes), 3)


class MotionIsNeverRetriedTests(unittest.TestCase):
    """The safety-critical negative test."""

    def _prepared_driver(self, fake: ScriptedFakeTransport, **kwargs) -> MultiFloDriver:
        driver = MultiFloDriver(
            fake,
            expected_product_serial="14071419",
            completion_poll_interval_seconds=0,
            **kwargs,
        )
        driver.open()
        driver._operator_confirmed_idle = True
        driver._motion_preflight = device_info()
        return driver

    def test_a_failed_motion_command_is_not_resent(self) -> None:
        step = Shake(duration_seconds=5)
        fake = ScriptedFakeTransport(
            [
                response(0x008D),
                TransportError("USB hiccup during the shake command"),
            ]
        )
        driver = self._prepared_driver(fake)

        with self.assertRaises(UnknownExecutionState):
            driver.shake(step)

        # Start Batch and exactly one shake. A second shake frame would be a
        # second physical operation.
        self.assertEqual(
            fake.writes,
            [
                encode_request(0x008D, 0, encode_batch_start(step.plate_type)),
                encode_request(0x00A3, 1, encode_shake(step)),
            ],
        )

    def test_a_failed_start_batch_is_not_resent(self) -> None:
        fake = ScriptedFakeTransport([TransportError("gone")])
        driver = self._prepared_driver(fake)

        with self.assertRaises(UnknownExecutionState):
            driver.shake(Shake(duration_seconds=5))

        self.assertEqual(len(fake.writes), 1)

    def test_uncertain_motion_clears_authorization(self) -> None:
        fake = ScriptedFakeTransport([TransportError("gone")])
        driver = self._prepared_driver(fake)

        with self.assertRaises(UnknownExecutionState):
            driver.shake(Shake(duration_seconds=5))

        # The driver must refuse further motion until a fresh preflight runs.
        with self.assertRaises(Exception):
            driver.shake(Shake(duration_seconds=5))
        self.assertIsNone(driver._motion_preflight)
        self.assertFalse(driver._operator_confirmed_idle)


class PollBudgetTests(unittest.TestCase):
    def _prepared_driver(self, fake: ScriptedFakeTransport, **kwargs) -> MultiFloDriver:
        driver = MultiFloDriver(
            fake,
            expected_product_serial="14071419",
            completion_poll_interval_seconds=0,
            **kwargs,
        )
        driver.open()
        driver._operator_confirmed_idle = True
        driver._motion_preflight = device_info()
        return driver

    def test_a_transient_poll_failure_does_not_fail_the_run(self) -> None:
        """The headline fix: a hiccup while polling used to brick the shift."""

        step = Shake(duration_seconds=5)
        fake = ScriptedFakeTransport(
            [
                response(0x008D),
                response(0x00A3),
                TransportError("USB hiccup while polling"),
                program_status_response(ProgramStepState.BUSY),
                TransportError("another hiccup"),
                program_status_response(ProgramStepState.READY),
                response(0x008C),
            ]
        )
        driver = self._prepared_driver(fake, poll_failure_budget=5)

        result = driver.shake(step)

        self.assertEqual(result.response.command_id, 0x00A3)
        self.assertEqual(driver.consecutive_poll_failures, 0)
        self.assertIs(driver.last_status.state, ProgramStepState.READY)

    def test_the_poll_budget_is_bounded(self) -> None:
        fake = ScriptedFakeTransport(
            [
                response(0x008D),
                response(0x00A3),
            ]
            + [TransportError("still gone")] * 10
        )
        driver = self._prepared_driver(fake, poll_failure_budget=2)

        with self.assertRaises(UnknownExecutionState):
            driver.shake(Shake(duration_seconds=5))


class PollLoopOutcomeTests(unittest.TestCase):
    def _run_shake_with(self, *status_reads, **kwargs):
        fake = ScriptedFakeTransport(
            [response(0x008D), response(0x00A3), *status_reads, response(0x008C)]
        )
        driver = MultiFloDriver(
            fake,
            expected_product_serial="14071419",
            completion_poll_interval_seconds=0,
            **kwargs,
        )
        driver.open()
        driver._operator_confirmed_idle = True
        driver._motion_preflight = device_info()
        return driver.shake(Shake(duration_seconds=5))

    def test_device_error_during_polling_is_a_device_error(self) -> None:
        with self.assertRaisesRegex(DeviceError, "entered error state"):
            self._run_shake_with(
                program_status_response(ProgramStepState.ERROR, error_code=0x1234)
            )

    def test_device_stopped_during_polling_is_a_device_error(self) -> None:
        with self.assertRaisesRegex(DeviceError, "stopped by the instrument"):
            self._run_shake_with(program_status_response(ProgramStepState.STOPPED))

    def test_a_paused_instrument_fails_instead_of_spinning(self) -> None:
        """Paused used to fall through and spin for the full 600 s timeout."""

        with self.assertRaisesRegex(DeviceError, "stayed paused"):
            self._run_shake_with(
                *[program_status_response(ProgramStepState.PAUSED)] * 2,
                paused_timeout_seconds=0,
            )

    def test_polling_times_out(self) -> None:
        with self.assertRaisesRegex(UnknownExecutionState, "0x00a3"):
            self._run_shake_with(
                *[program_status_response(ProgramStepState.BUSY)] * 20,
                completion_timeout_seconds=0.001,
            )

    def test_a_stop_request_ends_the_wait_as_unknown(self) -> None:
        """Shutdown must not block for the completion timeout."""

        fake = ScriptedFakeTransport(
            [response(0x008D), response(0x00A3), response(0x008C)]
        )
        driver = MultiFloDriver(
            fake,
            expected_product_serial="14071419",
            completion_poll_interval_seconds=0,
        )
        driver.open()
        driver._operator_confirmed_idle = True
        driver._motion_preflight = device_info()
        driver.stop_requested.set()

        with self.assertRaisesRegex(UnknownExecutionState, "unknown"):
            driver.shake(Shake(duration_seconds=5))


class LastStatusTests(unittest.TestCase):
    def test_inventory_records_the_last_observed_device_state(self) -> None:
        fake = ScriptedFakeTransport(inventory_reads())
        driver = MultiFloDriver(fake, expected_product_serial="14071419")
        driver.open()
        driver.authorize_motion(operator_confirmed_idle=True)

        driver.prepare_motion()

        self.assertIsNotNone(driver.last_status)
        self.assertIs(driver.last_status.state, ProgramStepState.READY)
        self.assertIsNotNone(driver.last_status_at)


if __name__ == "__main__":
    unittest.main()
