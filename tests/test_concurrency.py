"""Concurrency, shutdown, and crash-marker handling.

`_exchange_lock` is the only thing keeping two threads from interleaving frames
on one serial line, and `shutdown()` used to block for the driver's full
completion timeout. Neither had a test.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from multiflo.driver import MultiFloDriver
from multiflo.errors import BusyError
from multiflo.models import PeristalticDispense, Protocol
from multiflo.runner import ControllerState, ProtocolRunner
from multiflo.tests.fakes import FakeDriver, response, write_marker


class SerializingTransport:
    """Records overlapping exchanges so an unlocked driver would be caught."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.completed = 0
        self._guard = threading.Lock()
        self.is_open = True

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def purge(self) -> None:
        pass

    def write(self, data: bytes) -> None:
        with self._guard:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        # Widen the window an unlocked driver would interleave in.
        time.sleep(0.002)

    def read(self, size: int) -> bytes:
        payload = response(0x0073)
        with self._guard:
            self.in_flight = max(0, self.in_flight - 1) if size == 1 else self.in_flight
        if not hasattr(self, "_buffer") or not self._buffer:
            self._buffer = payload
        chunk, self._buffer = self._buffer[:size], self._buffer[size:]
        if not self._buffer:
            with self._guard:
                self.completed += 1
        return chunk


class ExchangeLockTests(unittest.TestCase):
    def test_concurrent_exchanges_never_interleave(self) -> None:
        transport = SerializingTransport()
        driver = MultiFloDriver(transport)
        driver.open()

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(driver.communication_test) for _ in range(8)]
            for future in futures:
                future.result(timeout=10)

        # One request on the wire at a time is the entire contract.
        self.assertEqual(transport.max_in_flight, 1)

    def test_message_ids_are_unique_under_concurrency(self) -> None:
        transport = SerializingTransport()
        driver = MultiFloDriver(transport)
        driver.open()

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = [
                future.result(timeout=10)
                for future in [pool.submit(driver.communication_test) for _ in range(8)]
            ]

        self.assertEqual(len(results), 8)


class ShutdownTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.marker_path = Path(directory.name) / "active-run.json"

    def test_shutdown_signals_the_driver_to_stop_polling(self) -> None:
        """Ctrl+C must not wait out the 600 s completion timeout."""

        driver = FakeDriver(block=True)
        runner = ProtocolRunner(driver, crash_marker_path=self.marker_path)  # type: ignore[arg-type]
        runner.start(
            Protocol(name="long", steps=[PeristalticDispense(volume_ul=100)]),
            operator_confirmed_idle=True,
            request_id="shutdown-1",
        )
        self.assertTrue(driver.motion_started.wait(timeout=2))

        stopped = threading.Event()

        def shutdown() -> None:
            runner.shutdown()
            stopped.set()

        worker = threading.Thread(target=shutdown)
        worker.start()
        # The driver is told to stop even while the step is still in flight.
        self.assertTrue(
            self._wait_for(lambda: driver.stop_requested.is_set(), timeout=2),
            "shutdown did not signal the driver to stop",
        )
        driver.release_motion.set()
        worker.join(timeout=5)
        self.assertTrue(stopped.is_set(), "shutdown did not return")
        self.assertEqual(runner.state, ControllerState.CLOSED)

    def test_a_second_run_is_refused_after_shutdown(self) -> None:
        runner = ProtocolRunner(FakeDriver(), crash_marker_path=self.marker_path)  # type: ignore[arg-type]
        runner.shutdown()

        with self.assertRaisesRegex(BusyError, "closed"):
            runner.start(
                Protocol(name="late", steps=[PeristalticDispense(volume_ul=100)]),
                operator_confirmed_idle=True,
                request_id="after-shutdown",
            )

    @staticmethod
    def _wait_for(predicate, *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False


class CrashMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.marker_path = Path(directory.name) / "active-run.json"

    def _runner(self) -> ProtocolRunner:
        runner = ProtocolRunner(FakeDriver(), crash_marker_path=self.marker_path)  # type: ignore[arg-type]
        self.addCleanup(runner.shutdown)
        return runner

    def test_a_retained_marker_is_parsed_and_reported(self) -> None:
        write_marker(
            self.marker_path,
            run_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            protocol_name="interrupted dispense",
            current_step=3,
            last_confirmed_step=2,
        )

        runner = self._runner()

        self.assertTrue(runner.reconciliation_required)
        retained = runner.retained_marker
        self.assertIsNotNone(retained)
        self.assertEqual(str(retained.run_id), "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(retained.protocol_name, "interrupted dispense")
        self.assertEqual(retained.last_confirmed_step, 2)
        self.assertFalse(runner.marker_unreadable)

    def test_a_corrupt_marker_still_blocks_and_is_flagged(self) -> None:
        """An unparseable file is not evidence that the last run finished."""

        self.marker_path.write_text("this is not json", encoding="utf-8")

        runner = self._runner()

        self.assertTrue(runner.reconciliation_required)
        self.assertTrue(runner.marker_unreadable)
        self.assertIsNone(runner.retained_marker)
        self.assertEqual(runner.state, ControllerState.RECONCILIATION_REQUIRED)
        with self.assertRaisesRegex(BusyError, "reconciliation"):
            runner.start(
                Protocol(name="blocked", steps=[PeristalticDispense(volume_ul=100)]),
                operator_confirmed_idle=True,
                request_id="blocked-1",
            )

    def test_a_marker_with_the_wrong_shape_is_treated_as_corrupt(self) -> None:
        self.marker_path.write_text('{"unexpected": true}', encoding="utf-8")

        runner = self._runner()

        self.assertTrue(runner.marker_unreadable)
        self.assertTrue(runner.reconciliation_required)

    def test_no_marker_means_no_reconciliation(self) -> None:
        runner = self._runner()

        self.assertFalse(runner.reconciliation_required)
        self.assertIsNone(runner.retained_marker)
        self.assertFalse(runner.marker_unreadable)


class GeometryValidationTests(unittest.TestCase):
    def test_a_protocol_may_not_mix_plate_geometries(self) -> None:
        """Step defaults differ, so this is easy to do by accident."""

        from pydantic import ValidationError as PydanticValidationError
        from multiflo.models import Shake

        with self.assertRaisesRegex(PydanticValidationError, "same plate type"):
            Protocol(
                name="mixed",
                steps=[
                    PeristalticDispense(volume_ul=100),  # 96_deep_well default
                    Shake(duration_seconds=5),  # 96_well default
                ],
            )

    def test_an_explicit_shared_geometry_is_accepted(self) -> None:
        from multiflo.models import Shake

        protocol = Protocol(
            name="consistent",
            steps=[
                PeristalticDispense(volume_ul=100, plate_type="96_well"),
                Shake(duration_seconds=5, plate_type="96_well"),
            ],
        )

        self.assertEqual(len(protocol.steps), 2)


if __name__ == "__main__":
    unittest.main()
