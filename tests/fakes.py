"""Shared test doubles.

`ScriptedFakeTransport` used to live in `multiflo/transport.py`, which shipped
test-only code (including a bare `AssertionError` raiser) inside the runtime
package. It lives here instead.

Two rules this module exists to enforce:

* A write that does not match the script raises `AssertionError`, never
  `TransportError`. `MultiFloDriver._motion_exchange` catches `TransportError`
  and converts it to `UnknownExecutionState`, so a byte-level mismatch used to
  surface as ``'unknown_execution_state' != 'completed'`` with the hex diff
  thrown away.
* `FakeDriver` carries the same attributes the real driver exposes to the
  runner (`last_status`, `stop_requested`, `expected_product_serial`), so
  production code never needs a defensive branch for a test double.
"""

from __future__ import annotations

from collections import deque
from threading import Event
from typing import Deque, Iterable

from multiflo.codec import Frame, MessageClass, encode_request
from multiflo.driver import (
    ExchangeResult,
    InstalledModules,
    ProgramStepState,
    ProgramStepStatus,
    ReadOnlyDeviceInfo,
    BasecodeVersionInfo,
)
from multiflo.errors import TransportError
from multiflo.models import CassetteType, Protocol


BASECODE = b"7210200" + b"1.12    " + b"ABFB" + b"61FF" + b"103  " + b"002" + b"003"
EXPECTED_SERIAL = "14071419"


# --------------------------------------------------------------------------
# Scripted byte-level fakes
# --------------------------------------------------------------------------


def response(command: int, body: bytes = b"\x00\x00") -> bytes:
    """One ACK plus the observed base-MultiFlo response frame for `command`."""

    return bytes((0x06,)) + Frame(MessageClass.REQUEST, 0, command, 0, 0, body).encode()


def program_status_response(
    state: ProgramStepState,
    *,
    error_code: int = 0,
    error_source: int = 2,
) -> bytes:
    return response(
        0x0092,
        b"\x00\x00"
        + int(state).to_bytes(2, "little")
        + error_code.to_bytes(4, "little")
        + bytes((error_source,)),
    )


def inventory_reads(
    *,
    serial: str = EXPECTED_SERIAL,
    cassette_code: int = 2,
    state: ProgramStepState = ProgramStepState.READY,
) -> list[bytes]:
    """Reads for one communication test, ready check, and full inventory."""

    return [
        response(0x0073),
        program_status_response(state),
        response(0x0100, b"\x00\x00" + serial.encode() + b"\x00"),
        response(0x00A0, b"\x00\x00" + BASECODE),
        response(0x0104, b"\x00\x00\x01"),
        response(0x0104, b"\x00\x00\x00"),
        response(0x0154, b"\x00\x00\x01"),
        response(0x0108, b"\x00\x00" + bytes((cassette_code,))),
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


class ScriptedFakeTransport:
    """Return scripted fragments or failures; intentionally not a simulator."""

    def __init__(
        self,
        reads: Iterable[bytes | Exception] = (),
        *,
        expected_writes: Iterable[bytes] | None = None,
    ) -> None:
        self._reads: Deque[bytes | Exception] = deque(reads)
        self._current = b""
        self._expected_writes = deque(expected_writes or ())
        self.writes: list[bytes] = []
        self.purge_count = 0
        self.is_open = False

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def purge(self) -> None:
        self.purge_count += 1
        self._current = b""

    def write(self, data: bytes) -> None:
        if not self.is_open:
            raise TransportError("fake transport is not open")
        self.writes.append(data)
        if self._expected_writes:
            expected = self._expected_writes.popleft()
            if data != expected:
                # AssertionError, not TransportError: the driver catches
                # TransportError during motion and would hide the hex diff.
                raise AssertionError(
                    f"write {len(self.writes)} was {data.hex(' ')}, "
                    f"expected {expected.hex(' ')}"
                )

    def read(self, size: int) -> bytes:
        if not self.is_open:
            raise TransportError("fake transport is not open")
        if not self._current:
            if not self._reads:
                raise TransportError("scripted read timed out")
            item = self._reads.popleft()
            if isinstance(item, Exception):
                raise item
            self._current = item
        result, self._current = self._current[:size], self._current[size:]
        if not result:
            raise TransportError("scripted transport disconnected")
        return result

    def assert_script_consumed(self) -> None:
        if self._expected_writes:
            raise AssertionError(f"{len(self._expected_writes)} expected writes remain")
        if self._reads or self._current:
            raise AssertionError("scripted reads remain")

    def assert_writes(self, expected: list[bytes]) -> None:
        """Assert the exact write sequence, including that there were no extras.

        `expected_writes` only validates a prefix, so a driver that sends
        additional commands after the script is exhausted passes silently
        without this.
        """

        if self.writes != expected:
            raise AssertionError(
                f"wrote {len(self.writes)} frames, expected {len(expected)}:\n"
                + "\n".join(
                    f"  {index}: {got.hex(' ') if got else '-'}"
                    f" != {want.hex(' ') if want else '-'}"
                    for index, (got, want) in enumerate(
                        zip(
                            self.writes + [b""] * max(0, len(expected) - len(self.writes)),
                            expected + [b""] * max(0, len(self.writes) - len(expected)),
                        )
                    )
                    if got != want
                )
            )


class FailingTransport:
    """Transport whose device is absent; opening it always fails."""

    def __init__(self, *, fail_times: int | None = None) -> None:
        self.open_attempts = 0
        self.fail_times = fail_times
        self.is_open = False
        self.writes: list[bytes] = []

    def open(self) -> None:
        self.open_attempts += 1
        if self.fail_times is not None and self.open_attempts > self.fail_times:
            self.is_open = True
            return
        raise TransportError(f"device with serial {EXPECTED_SERIAL} was not found")

    def close(self) -> None:
        self.is_open = False

    def purge(self) -> None:
        pass

    def read(self, size: int) -> bytes:  # pragma: no cover - never reached
        raise TransportError("device is not open")

    def write(self, data: bytes) -> None:  # pragma: no cover - never reached
        raise TransportError("device is not open")


# --------------------------------------------------------------------------
# Driver-level fake
# --------------------------------------------------------------------------


def device_info(cassette: CassetteType = CassetteType.FIVE_UL) -> ReadOnlyDeviceInfo:
    return ReadOnlyDeviceInfo(
        EXPECTED_SERIAL,
        BasecodeVersionInfo("7210200", "1.12", "ABFB", "61FF", "103", "002", "003", b""),
        InstalledModules(True, False, True, cassette),
    )


class FakeDriver:
    """One driver stand-in for the runner and API tests.

    Replaces the four fakes that used to live in test_runner.py and the near
    identical one in test_api_contract.py. `block` makes a motion step wait on
    `release_motion` so abort and conflict races can be sequenced; `fail_on`
    raises a given exception on the Nth motion call.
    """

    def __init__(
        self,
        *,
        block: bool = False,
        fail_on: tuple[int, Exception] | None = None,
        cassette: CassetteType = CassetteType.FIVE_UL,
        program_step_state: ProgramStepState = ProgramStepState.READY,
        release_on_close: bool = False,
    ) -> None:
        self.expected_product_serial = EXPECTED_SERIAL
        self.block = block
        self.fail_on = fail_on
        self.cassette = cassette
        self.program_step_state = program_step_state
        # Real teardown does not unblock a step; only tests that explicitly
        # want that behaviour should ask for it.
        self.release_on_close = release_on_close

        self.motion_started = Event()
        self.release_motion = Event()
        self.motion_calls = 0
        self.query_count = 0
        self.closed = False

        # Mirrors of the real driver's public surface.
        self.last_status: ProgramStepStatus | None = None
        self.last_status_at: float | None = None
        self.consecutive_poll_failures = 0
        self.stop_requested = Event()

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        if self.release_on_close:
            self.release_motion.set()

    def authorize_motion(self, *, operator_confirmed_idle: bool) -> None:
        if not operator_confirmed_idle:
            raise RuntimeError("confirmation missing")

    def prepare_motion(
        self,
        *,
        require_primary_peristaltic: bool = True,
    ) -> ReadOnlyDeviceInfo:
        return device_info(self.cassette)

    def validate_protocol(self, protocol: Protocol) -> None:
        pass

    def query_program_step_status(self) -> ProgramStepStatus:
        self.query_count += 1
        status = ProgramStepStatus(self.program_step_state, 0, 0)
        self.last_status = status
        return status

    def _motion(self, command_id: int) -> ExchangeResult:
        self.motion_calls += 1
        if self.fail_on is not None and self.motion_calls == self.fail_on[0]:
            raise self.fail_on[1]
        if self.block:
            self.motion_started.set()
            self.release_motion.wait(timeout=5)
        return ExchangeResult(
            Frame(MessageClass.REQUEST, 0, command_id, 0, 0, b"\x00\x00"),
            (),
        )

    def peristaltic_dispense(self, step) -> ExchangeResult:
        return self._motion(0x008F)

    def peristaltic_prime(self, step) -> ExchangeResult:
        return self._motion(0x0090)

    def peristaltic_purge(self, step) -> ExchangeResult:
        return self._motion(0x0091)

    def shake(self, step) -> ExchangeResult:
        return self._motion(0x00A3)

    def soak(self, step) -> ExchangeResult:
        return self._motion(0x00A3)


def write_marker(
    path,
    *,
    run_id: str = "11111111-2222-3333-4444-555555555555",
    protocol_name: str = "interrupted run",
    current_step: int | None = 0,
    last_confirmed_step: int | None = None,
) -> None:
    """Write a valid retained crash marker.

    Tests used to write ``"{}"`` here, which passed only because nothing parsed
    the file. The runner now reads it to report what was interrupted.
    """

    import json
    from datetime import datetime, timezone
    from pathlib import Path

    Path(path).write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": run_id,
                "protocol_name": protocol_name,
                "current_step": current_step,
                "last_confirmed_step": last_confirmed_step,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
