"""Single-owner command exchange for the base MultiFlo."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import logging
from threading import Event, Lock
import time

from .codec import (
    Endpoint,
    Frame,
    HEADER_SIZE,
    MAX_BODY_LENGTH,
    MessageClass,
    decode_frame,
    decode_header,
    encode_batch_start,
    encode_peristaltic_dispense,
    encode_peristaltic_prime,
    encode_peristaltic_purge,
    encode_request,
    encode_shake,
    encode_soak,
)
from .errors import (
    DeviceError,
    PreconditionError,
    ProtocolError,
    TransportError,
    UnknownExecutionState,
)
from .logs import log_event, truncated_hex
from .models import (
    CASSETTE_TYPES_BY_CODE,
    CassetteType,
    PeristalticDispense,
    PeristalticPrime,
    PeristalticPurge,
    PlateType,
    Protocol,
    Shake,
    Soak,
    validate_volume_for_cassette,
)
from .transport import ByteTransport


COMMUNICATION_TEST = 0x0073
PERISTALTIC_DISPENSE = 0x008F
PERISTALTIC_PRIME = 0x0090
PERISTALTIC_PURGE = 0x0091
END_BATCH = 0x008C
START_BATCH = 0x008D
PROGRAM_STEP_STATUS = 0x0092
SHAKE_SOAK = 0x00A3
BASECODE_VERSION = 0x00A0
PRODUCT_SERIAL_NUMBER = 0x0100
PERISTALTIC_INSTALLED = 0x0104
PERISTALTIC_CASSETTE = 0x0108
HALF_MICROLITER_SUPPORT = 0x0154
ACK = 0x06
NAK = 0x15

_LOGGER = logging.getLogger(__name__)
# How often a long motion reports progress, so a 10-minute step is not silent.
_HEARTBEAT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ExchangeResult:
    response: Frame
    indications: tuple[Frame, ...]


@dataclass(frozen=True, slots=True)
class BasecodeVersionInfo:
    part_number: str
    software_version: str
    ui_checksum: str
    motion_controller_checksum: str
    data_version: str
    ui_version: str
    motion_controller_version: str
    reserved: bytes


@dataclass(frozen=True, slots=True)
class InstalledModules:
    primary_peristaltic: bool
    secondary_peristaltic: bool
    half_microliter_supported: bool
    primary_cassette: CassetteType


@dataclass(frozen=True, slots=True)
class ReadOnlyDeviceInfo:
    product_serial_number: str
    basecode: BasecodeVersionInfo
    modules: InstalledModules


class ProgramStepState(IntEnum):
    """States returned by the recovered 0x0092 Program Step Status query."""

    READY = 1
    BUSY = 2
    PAUSED = 3
    ERROR = 4
    STOPPED = 5


@dataclass(frozen=True, slots=True)
class ProgramStepStatus:
    state: ProgramStepState
    error_code: int
    error_source: int


@dataclass(frozen=True, slots=True)
class BatchRecoveryResult:
    before: ProgramStepStatus
    end_batch: ExchangeResult
    after: ProgramStepStatus


class MultiFloDriver:
    """Serialize request/response exchanges over one owned transport."""

    def __init__(
        self,
        transport: ByteTransport,
        *,
        expected_product_serial: str | None = None,
        max_body_length: int = MAX_BODY_LENGTH,
        completion_timeout_seconds: float = 600.0,
        completion_poll_interval_seconds: float = 0.5,
        paused_timeout_seconds: float = 60.0,
        read_only_attempts: int = 3,
        open_attempts: int = 3,
        poll_failure_budget: int = 5,
        retry_backoff_seconds: float = 0.5,
        logger: logging.Logger | None = None,
    ) -> None:
        self.transport = transport
        self.expected_product_serial = expected_product_serial
        self.max_body_length = max_body_length
        if completion_timeout_seconds <= 0:
            raise ValueError("completion timeout must be positive")
        if completion_poll_interval_seconds < 0:
            raise ValueError("completion poll interval cannot be negative")
        if paused_timeout_seconds < 0:
            # Zero means fail on the second consecutive paused poll.
            raise ValueError("paused timeout cannot be negative")
        if min(read_only_attempts, open_attempts) < 1:
            raise ValueError("attempt counts must be at least 1")
        if poll_failure_budget < 0:
            raise ValueError("poll failure budget cannot be negative")
        self.completion_timeout_seconds = completion_timeout_seconds
        self.completion_poll_interval_seconds = completion_poll_interval_seconds
        self.paused_timeout_seconds = paused_timeout_seconds
        self.read_only_attempts = read_only_attempts
        self.open_attempts = open_attempts
        self.poll_failure_budget = poll_failure_budget
        self.retry_backoff_seconds = retry_backoff_seconds
        self._logger = logger or _LOGGER
        self._next_message_id = 0
        self._exchange_lock = Lock()
        self._is_open = False
        self._operator_confirmed_idle = False
        self._motion_preflight: ReadOnlyDeviceInfo | None = None
        # Last observation of the machine, so a caller can report device state
        # without issuing another command. Written under _exchange_lock.
        self.last_status: ProgramStepStatus | None = None
        self.last_status_at: float | None = None
        self.consecutive_poll_failures = 0
        # Set by the runner on shutdown so a long poll stops promptly instead
        # of blocking teardown for the full completion timeout.
        self.stop_requested = Event()

    def open(self) -> None:
        """Open the transport, retrying a failed connection a bounded number of times.

        Opening moves no liquid, so a transient enumeration or handle failure
        is safe to retry. Nothing else in the connection path is retried.
        """

        if self._is_open:
            return
        for attempt in range(1, self.open_attempts + 1):
            try:
                self.transport.open()
            except TransportError as error:
                if attempt == self.open_attempts:
                    log_event(
                        self._logger,
                        "transport_open_failed",
                        level=logging.ERROR,
                        attempts=attempt,
                        error=f"{type(error).__name__}: {error}",
                    )
                    raise
                delay = self.retry_backoff_seconds * attempt
                log_event(
                    self._logger,
                    "transport_open_retry",
                    level=logging.WARNING,
                    attempt=attempt,
                    of=self.open_attempts,
                    retry_in_seconds=delay,
                    error=f"{type(error).__name__}: {error}",
                )
                time.sleep(delay)
            else:
                self._is_open = True
                log_event(self._logger, "transport_opened", attempts=attempt)
                return

    def close(self) -> None:
        if self._is_open:
            try:
                self.transport.close()
            finally:
                self._is_open = False
                self._operator_confirmed_idle = False
                self._motion_preflight = None

    def communication_test(self) -> ExchangeResult:
        """Send the bodyless, non-motion communication-test command only."""

        result = self._exchange_with_retry(
            COMMUNICATION_TEST,
            attempts=self.read_only_attempts,
        )
        self._response_data(COMMUNICATION_TEST, result.response.body)
        return result

    def query_basecode_version(self) -> BasecodeVersionInfo:
        data = self._read_only_data(BASECODE_VERSION)
        if len(data) < 34:
            raise ProtocolError(f"basecode version response is only {len(data)} bytes")
        return BasecodeVersionInfo(
            part_number=data[0:7].decode("ascii").strip(),
            software_version=data[7:15].decode("ascii").strip(),
            ui_checksum=data[15:19].decode("ascii").strip(),
            motion_controller_checksum=data[19:23].decode("ascii").strip(),
            data_version=data[23:28].decode("ascii").strip(),
            ui_version=data[28:31].decode("ascii").strip(),
            motion_controller_version=data[31:34].decode("ascii").strip(),
            reserved=data[34:],
        )

    def query_product_serial_number(self) -> str:
        data = self._read_only_data(PRODUCT_SERIAL_NUMBER)
        return data.split(b"\x00", 1)[0].decode("ascii").strip()

    def query_peristaltic_installed(self, pump: int) -> bool:
        if pump not in (1, 2):
            raise PreconditionError("peristaltic pump must be 1 or 2")
        data = self._read_only_data(PERISTALTIC_INSTALLED, bytes((pump,)))
        if len(data) != 1:
            raise ProtocolError("peristaltic-installed response must contain one data byte")
        return bool(data[0])

    def query_half_microliter_support(self) -> bool:
        data = self._read_only_data(HALF_MICROLITER_SUPPORT)
        if len(data) != 1:
            raise ProtocolError("half-microliter response must contain one data byte")
        return bool(data[0])

    def query_peristaltic_cassette(self, pump: int) -> CassetteType:
        if pump not in (1, 2):
            raise PreconditionError("peristaltic pump must be 1 or 2")
        data = self._read_only_data(PERISTALTIC_CASSETTE, bytes((pump,)))
        if len(data) != 1:
            raise ProtocolError("peristaltic-cassette response must contain one data byte")
        try:
            return CASSETTE_TYPES_BY_CODE[data[0]]
        except KeyError as error:
            raise ProtocolError(
                f"instrument returned unknown cassette type 0x{data[0]:02x}"
            ) from error

    def inspect_device(self) -> ReadOnlyDeviceInfo:
        serial = self.query_product_serial_number()
        version = self.query_basecode_version()
        primary = self.query_peristaltic_installed(1)
        secondary = self.query_peristaltic_installed(2)
        half_microliter = self.query_half_microliter_support()
        cassette = self.query_peristaltic_cassette(1)
        return ReadOnlyDeviceInfo(
            serial,
            version,
            InstalledModules(
                primary,
                secondary,
                half_microliter,
                cassette,
            ),
        )

    def query_program_step_status(self) -> ProgramStepStatus:
        """Read the authoritative device-side program-step state."""

        data = self._read_only_data(PROGRAM_STEP_STATUS)
        if len(data) != 7:
            raise ProtocolError(
                "program-step-status response must contain seven data bytes"
            )
        state_code = int.from_bytes(data[0:2], "little")
        try:
            state = ProgramStepState(state_code)
        except ValueError as error:
            raise ProtocolError(
                f"instrument returned unknown program-step state {state_code}"
            ) from error
        status = ProgramStepStatus(
            state=state,
            error_code=int.from_bytes(data[2:6], "little"),
            error_source=data[6],
        )
        self.last_status = status
        self.last_status_at = time.monotonic()
        return status

    def recover_end_batch(
        self,
        *,
        operator_confirmed_stationary: bool,
    ) -> BatchRecoveryResult:
        """Send one guarded End Batch to recover a stale Busy state.

        This is a recovery action, not a normal motion path. It deliberately
        refuses any initial state other than Busy and never retries.
        """

        if not operator_confirmed_stationary:
            raise PreconditionError("stationary recovery confirmation is required")
        before = self.query_program_step_status()
        if before.state is not ProgramStepState.BUSY:
            raise PreconditionError(
                f"End Batch recovery requires busy state, got "
                f"{before.state.name.lower()}"
            )
        try:
            end_batch = self._exchange(END_BATCH)
            self._response_data(END_BATCH, end_batch.response.body)
            after = self.query_program_step_status()
        except DeviceError:
            raise
        except (TransportError, ProtocolError) as error:
            raise UnknownExecutionState(
                "communication failed after End Batch recovery was sent; "
                "do not retry automatically"
            ) from error
        if after.state is not ProgramStepState.READY:
            raise DeviceError(
                f"End Batch recovery returned {after.state.name.lower()}, not ready"
            )
        return BatchRecoveryResult(before, end_batch, after)

    def authorize_motion(self, *, operator_confirmed_idle: bool) -> None:
        """Record an operator's per-run idle/setup confirmation.

        The base protocol's authoritative busy query is not yet recovered. This
        confirmation is therefore deliberately required before the fresh
        communication and inventory preflight used by Phase 2.
        """

        if not operator_confirmed_idle:
            raise PreconditionError("operator idle/setup confirmation is required")
        self._operator_confirmed_idle = True
        self._motion_preflight = None

    def prepare_motion(self, *, require_primary_peristaltic: bool = True) -> ReadOnlyDeviceInfo:
        if not self._operator_confirmed_idle:
            raise PreconditionError("motion has not been authorized by the operator")
        if not self.expected_product_serial:
            raise PreconditionError("an expected product serial is required for motion")
        self.communication_test()
        status = self.query_program_step_status()
        if status.state is not ProgramStepState.READY:
            raise PreconditionError(
                f"instrument program-step state is {status.state.name.lower()}, not ready"
            )
        info = self.inspect_device()
        if info.product_serial_number != self.expected_product_serial:
            raise PreconditionError(
                "connected instrument serial does not match the motion allowlist"
            )
        if require_primary_peristaltic:
            if not info.modules.primary_peristaltic:
                raise PreconditionError("primary peristaltic pump is not installed")
            if info.modules.primary_cassette is CassetteType.ANY:
                raise PreconditionError("instrument cassette setting must be 1ul, 5ul, or 10ul")
        self._motion_preflight = info
        return info

    def validate_protocol(self, protocol: Protocol) -> None:
        """Validate every step against fresh device inventory before motion."""

        info = self._require_motion_preflight()
        installed = info.modules.primary_cassette
        for index, step in enumerate(protocol.steps):
            if not isinstance(
                step,
                (PeristalticDispense, PeristalticPrime, PeristalticPurge),
            ):
                continue
            if not info.modules.primary_peristaltic:
                raise PreconditionError(
                    f"step {index} requires the primary peristaltic pump"
                )
            if installed is CassetteType.ANY:
                raise PreconditionError(
                    "instrument cassette setting must be 1ul, 5ul, or 10ul"
                )
            if (
                step.cassette_type is not CassetteType.ANY
                and step.cassette_type is not installed
            ):
                raise PreconditionError(
                    f"step {index} requires {step.cassette_type.value}, but the "
                    f"instrument setting is {installed.value}"
                )
            if isinstance(step, PeristalticDispense):
                try:
                    validate_volume_for_cassette(step.volume_ul, installed)
                    if step.pre_dispense_volume_ul:
                        validate_volume_for_cassette(
                            step.pre_dispense_volume_ul,
                            installed,
                        )
                except ValueError as error:
                    raise PreconditionError(f"step {index}: {error}") from error

    def peristaltic_dispense(self, step: PeristalticDispense) -> ExchangeResult:
        installed = self._check_peristaltic_cassette(step.cassette_type)
        validate_volume_for_cassette(step.volume_ul, installed)
        if step.pre_dispense_volume_ul:
            validate_volume_for_cassette(step.pre_dispense_volume_ul, installed)
        return self._motion_exchange(
            PERISTALTIC_DISPENSE,
            encode_peristaltic_dispense(step),
            step.plate_type,
        )

    def peristaltic_prime(self, step: PeristalticPrime) -> ExchangeResult:
        self._check_peristaltic_cassette(step.cassette_type)
        return self._motion_exchange(
            PERISTALTIC_PRIME,
            encode_peristaltic_prime(step),
            step.plate_type,
        )

    def peristaltic_purge(self, step: PeristalticPurge) -> ExchangeResult:
        self._check_peristaltic_cassette(step.cassette_type)
        return self._motion_exchange(
            PERISTALTIC_PURGE,
            encode_peristaltic_purge(step),
            step.plate_type,
        )

    def shake(self, step: Shake) -> ExchangeResult:
        self._require_motion_preflight()
        return self._motion_exchange(SHAKE_SOAK, encode_shake(step), step.plate_type)

    def soak(self, step: Soak) -> ExchangeResult:
        self._require_motion_preflight()
        return self._motion_exchange(SHAKE_SOAK, encode_soak(step), step.plate_type)

    def _require_motion_preflight(self) -> ReadOnlyDeviceInfo:
        if self._motion_preflight is None:
            raise PreconditionError("motion preflight has not completed")
        return self._motion_preflight

    def _check_peristaltic_cassette(self, requested: CassetteType) -> CassetteType:
        info = self._require_motion_preflight()
        if not info.modules.primary_peristaltic:
            raise PreconditionError("primary peristaltic pump is not installed")
        installed = info.modules.primary_cassette
        if installed is CassetteType.ANY:
            raise PreconditionError("instrument cassette setting must be 1ul, 5ul, or 10ul")
        if requested is not CassetteType.ANY and requested is not installed:
            raise PreconditionError(
                f"requested {requested.value} cassette does not match "
                f"instrument setting {installed.value}"
            )
        return installed

    def _motion_exchange(
        self,
        command_id: int,
        body: bytes,
        plate_type: PlateType,
    ) -> ExchangeResult:
        try:
            start = self._exchange(START_BATCH, encode_batch_start(plate_type))
            self._response_data(START_BATCH, start.response.body)
            result = self._exchange(command_id, body)
            self._response_data(command_id, result.response.body)
            self._wait_for_program_step_ready(command_id)
            end = self._exchange(END_BATCH)
            self._response_data(END_BATCH, end.response.body)
            return result
        except DeviceError:
            raise
        except (TransportError, ProtocolError) as error:
            self._operator_confirmed_idle = False
            self._motion_preflight = None
            raise UnknownExecutionState(
                f"communication failed after the batch/step sequence for motion command "
                f"0x{command_id:04x} began; do not retry automatically"
            ) from error

    def _wait_for_program_step_ready(self, command_id: int) -> None:
        """Poll until the instrument reports Ready.

        A status poll is read-only and idempotent, so a lost response says
        nothing about the motion itself. Failing the run on the first one turns
        a momentary USB hiccup into a retained crash marker that blocks every
        later run, so a bounded budget of consecutive faults is absorbed first.
        """

        started = time.monotonic()
        deadline = started + self.completion_timeout_seconds
        paused_since: float | None = None
        failures = 0
        last_heartbeat = started

        while True:
            if self.stop_requested.is_set():
                raise UnknownExecutionState(
                    f"stopped while waiting for motion command 0x{command_id:04x}; "
                    "the physical outcome is unknown"
                )

            status = self._poll_status(command_id, failures)
            if status is None:
                failures += 1
                time.sleep(self.completion_poll_interval_seconds)
                continue
            failures = 0
            self.consecutive_poll_failures = 0

            if status.state is ProgramStepState.READY:
                log_event(
                    self._logger,
                    "motion_completed",
                    command_id=f"0x{command_id:04x}",
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
                return
            self._raise_for_motion_state(command_id, status)

            now = time.monotonic()
            paused_since = self._check_paused(command_id, status, paused_since, now)
            if now - last_heartbeat >= _HEARTBEAT_SECONDS:
                last_heartbeat = now
                log_event(
                    self._logger,
                    "motion_in_progress",
                    command_id=f"0x{command_id:04x}",
                    device_state=status.state.name.lower(),
                    device_error_code=status.error_code,
                    elapsed_seconds=round(now - started, 3),
                )
            if now >= deadline:
                raise TransportError(
                    f"timed out waiting for motion command 0x{command_id:04x} to finish"
                )
            time.sleep(self.completion_poll_interval_seconds)

    def _poll_status(self, command_id: int, failures: int) -> ProgramStepStatus | None:
        """One status read. Returns None when a transient fault was absorbed."""

        try:
            return self.query_program_step_status()
        except (TransportError, ProtocolError) as error:
            self.consecutive_poll_failures = failures + 1
            if self.consecutive_poll_failures > self.poll_failure_budget:
                log_event(
                    self._logger,
                    "poll_budget_exhausted",
                    level=logging.ERROR,
                    command_id=f"0x{command_id:04x}",
                    consecutive_failures=self.consecutive_poll_failures,
                    error=f"{type(error).__name__}: {error}",
                )
                raise
            log_event(
                self._logger,
                "poll_failed",
                level=logging.WARNING,
                command_id=f"0x{command_id:04x}",
                consecutive_failures=self.consecutive_poll_failures,
                budget=self.poll_failure_budget,
                error=f"{type(error).__name__}: {error}",
            )
            self._resync()
            return None

    def _raise_for_motion_state(
        self,
        command_id: int,
        status: ProgramStepStatus,
    ) -> None:
        if status.state is ProgramStepState.ERROR:
            raise DeviceError(
                f"motion command 0x{command_id:04x} entered error state "
                f"0x{status.error_code:08x} (source {status.error_source})"
            )
        if status.state is ProgramStepState.STOPPED:
            raise DeviceError(
                f"motion command 0x{command_id:04x} was stopped by the instrument"
            )

    def _check_paused(
        self,
        command_id: int,
        status: ProgramStepStatus,
        paused_since: float | None,
        now: float,
    ) -> float | None:
        """Fail a stuck pause instead of spinning out the completion timeout.

        No resume command has been recovered, so a pause will not clear itself.
        """

        if status.state is not ProgramStepState.PAUSED:
            return None
        if paused_since is None:
            log_event(
                self._logger,
                "device_paused",
                level=logging.WARNING,
                command_id=f"0x{command_id:04x}",
                fails_after_seconds=self.paused_timeout_seconds,
            )
            return now
        if now - paused_since >= self.paused_timeout_seconds:
            raise DeviceError(
                f"instrument stayed paused for {self.paused_timeout_seconds} s "
                f"during motion command 0x{command_id:04x}; no resume command "
                "is supported"
            )
        return paused_since

    def _read_only_data(self, command_id: int, body: bytes = b"") -> bytes:
        result = self._exchange_with_retry(
            command_id,
            body,
            attempts=self.read_only_attempts,
        )
        return self._response_data(command_id, result.response.body)

    def _exchange_with_retry(
        self,
        command_id: int,
        body: bytes = b"",
        *,
        attempts: int,
    ) -> ExchangeResult:
        """Retry an idempotent command, purging the stream between attempts.

        Only read-only queries may use this. Motion commands, Start Batch, and
        End Batch call `_exchange` directly so they can never acquire retry
        behaviour by accident: a re-sent motion command is a second physical
        operation.
        """

        for attempt in range(1, attempts + 1):
            try:
                return self._exchange(command_id, body)
            except (TransportError, ProtocolError) as error:
                if attempt == attempts:
                    raise
                log_event(
                    self._logger,
                    "read_only_retry",
                    level=logging.WARNING,
                    command_id=f"0x{command_id:04x}",
                    attempt=attempt,
                    of=attempts,
                    error=f"{type(error).__name__}: {error}",
                )
                self._resync()
        raise AssertionError("unreachable")  # pragma: no cover

    def _resync(self) -> None:
        """Discard buffered bytes so a partial frame cannot poison the next read.

        Without this a single bad checksum or stray ACK leaves the byte stream
        shifted for the life of the session.
        """

        purge = getattr(self.transport, "purge", None)
        if purge is None:
            return
        try:
            purge()
        except Exception as error:  # A failed purge must not mask the original fault.
            log_event(
                self._logger,
                "resync_failed",
                level=logging.WARNING,
                error=f"{type(error).__name__}: {error}",
            )

    @staticmethod
    def _response_data(command_id: int, body: bytes) -> bytes:
        if len(body) < 2:
            raise ProtocolError(
                f"response for command 0x{command_id:04x} has no two-byte device status"
            )
        status = int.from_bytes(body[:2], "little")
        if status:
            raise DeviceError(
                f"command 0x{command_id:04x} returned device status 0x{status:04x}"
            )
        return body[2:]

    def _exchange(self, command_id: int, body: bytes = b"") -> ExchangeResult:
        if not self._is_open:
            raise TransportError("driver is not open")
        with self._exchange_lock:
            message_id = self._next_message_id
            self._next_message_id = (self._next_message_id + 1) & 0xFFFF
            request = encode_request(command_id, message_id, body)
            started = time.monotonic()
            try:
                result = self._exchange_locked(command_id, message_id, request)
            except Exception as error:
                log_event(
                    self._logger,
                    "exchange_failed",
                    level=logging.ERROR,
                    command_id=f"0x{command_id:04x}",
                    message_id=message_id,
                    request=truncated_hex(request),
                    elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                    error=f"{type(error).__name__}: {error}",
                )
                raise
            log_event(
                self._logger,
                "exchange",
                level=logging.DEBUG,
                command_id=f"0x{command_id:04x}",
                message_id=message_id,
                request=truncated_hex(request),
                response=truncated_hex(result.response.body),
                indication_count=len(result.indications),
                elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            )
            return result

    def _exchange_locked(
        self,
        command_id: int,
        message_id: int,
        request: bytes,
    ) -> ExchangeResult:
        """One request/response round trip. Caller holds the exchange lock."""

        self.transport.write(request)
        acknowledgement = self._read_exactly(1)[0]
        if acknowledgement == NAK:
            raise DeviceError(f"instrument rejected command 0x{command_id:04x} with NAK")
        if acknowledgement != ACK:
            raise ProtocolError(
                f"expected ACK 0x06 before response, got 0x{acknowledgement:02x}"
            )
        indications: list[Frame] = []
        while True:
            frame = self._read_frame()
            if frame.message_class == MessageClass.INDICATION:
                # Indications carry instrument-side warnings. They were
                # previously counted and discarded; log them so a failed
                # run can be diagnosed from the stream alone.
                log_event(
                    self._logger,
                    "indication",
                    level=logging.WARNING,
                    command_id=f"0x{frame.command_id:04x}",
                    during=f"0x{command_id:04x}",
                    body=truncated_hex(frame.body),
                )
                indications.append(frame)
                continue
            planned_response = (
                frame.message_class == MessageClass.RESPONSE
                and frame.destination == Endpoint.PC
                and frame.source == Endpoint.INSTRUMENT
                and frame.message_id == message_id
            )
            observed_base_response = (
                frame.message_class == MessageClass.REQUEST
                and frame.destination == 0
                and frame.source == 0
                and frame.message_id == 0
            )
            if not (planned_response or observed_base_response):
                raise ProtocolError(
                    "response header does not match the planned or observed base MultiFlo profile"
                )
            if frame.command_id != command_id:
                raise ProtocolError(
                    f"response command 0x{frame.command_id:04x} does not match "
                    f"request 0x{command_id:04x}"
                )
            return ExchangeResult(frame, tuple(indications))

    def _read_frame(self) -> Frame:
        header_bytes = self._read_exactly(HEADER_SIZE)
        header = decode_header(header_bytes, max_body_length=self.max_body_length)
        body = self._read_exactly(header.body_length) if header.body_length else b""
        return decode_frame(header_bytes + body, max_body_length=self.max_body_length)

    def _read_exactly(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = self.transport.read(size - len(result))
            if not chunk:
                raise TransportError(
                    f"transport disconnected after {len(result)} of {size} expected bytes"
                )
            result.extend(chunk)
        return bytes(result)

    def __enter__(self) -> "MultiFloDriver":
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
