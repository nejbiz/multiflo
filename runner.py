"""Single-worker protocol execution with persistent crash reconciliation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .driver import ExchangeResult, MultiFloDriver, ProgramStepState
from .errors import BusyError, UnknownExecutionState
from .models import (
    CassetteType,
    PeristalticDispense,
    PeristalticPrime,
    PeristalticPurge,
    Protocol,
    ProtocolStep,
    Shake,
    Soak,
)


DEFAULT_CRASH_MARKER_PATH = Path(".multiflo-active-run.json")
DEFAULT_DEVICE_QUERY_TIMEOUT_SECONDS = 30.0
_LOGGER = logging.getLogger(__name__)

RequestId = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]


class ControllerState(str, Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    RUNNING = "running"
    ABORTING = "aborting"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CLOSED = "closed"


class RunState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    ABORTING = "aborting"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"
    UNKNOWN_EXECUTION_STATE = "unknown_execution_state"


TERMINAL_STATES = {
    RunState.COMPLETED,
    RunState.ABORTED,
    RunState.FAILED,
    RunState.UNKNOWN_EXECUTION_STATE,
}


class RunStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    request_id: RequestId
    protocol_name: str
    state: RunState
    total_steps: int = Field(ge=1)
    completed_steps: int = Field(ge=0)
    current_step: int | None = Field(default=None, ge=0)
    error: str | None = None
    abort_is_cooperative: bool = True
    results: list["StepResult"] = Field(default_factory=list)


class StepResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_index: int = Field(ge=0)
    operation: str
    device_status: int = Field(ge=0, le=0xFFFF)
    indication_count: int = Field(ge=0)


class DeviceIdentity(BaseModel):
    """Read-only identity reported by the connected instrument."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    product_serial_number: str
    part_number: str
    software_version: str
    data_version: str
    ui_version: str
    motion_controller_version: str


class DeviceModules(BaseModel):
    """Installed capabilities reported by the connected instrument."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    primary_peristaltic: bool
    secondary_peristaltic: bool
    half_microliter_supported: bool
    primary_cassette: CassetteType


class DeviceStatus(BaseModel):
    """Verified identity, modules, connection, and controller state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connected: bool
    controller_state: ControllerState
    reconciliation_required: bool
    active_run_id: UUID | None = None
    expected_product_serial: str | None = None
    serial_matches_expected: bool = False
    identity: DeviceIdentity | None = None
    modules: DeviceModules | None = None
    program_step_state: Literal["ready", "busy", "paused", "error", "stopped"] | None = None
    program_step_error_code: int | None = Field(default=None, ge=0)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class StartResult:
    """One accepted run plus whether a repeated request ID was suppressed."""

    status: RunStatus
    duplicate: bool


class _CrashMarker(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    run_id: UUID
    protocol_name: str
    current_step: int | None = Field(default=None, ge=0)
    last_confirmed_step: int | None = Field(default=None, ge=0)
    updated_at: datetime


class _RunRecord:
    def __init__(self, run_id: UUID, request_id: str, protocol: Protocol) -> None:
        self.run_id = run_id
        self.request_id = request_id
        self.protocol = protocol
        self.state = RunState.QUEUED
        self.completed_steps = 0
        self.current_step: int | None = None
        self.error: str | None = None
        self.abort_requested = False
        self.results: list[StepResult] = []

    def status(self) -> RunStatus:
        return RunStatus(
            run_id=self.run_id,
            request_id=self.request_id,
            protocol_name=self.protocol.name,
            state=self.state,
            total_steps=len(self.protocol.steps),
            completed_steps=self.completed_steps,
            current_step=self.current_step,
            error=self.error,
            results=list(self.results),
        )


class ProtocolRunner:
    """Own one driver and execute at most one protocol at a time."""

    def __init__(
        self,
        driver: MultiFloDriver,
        *,
        crash_marker_path: str | Path = DEFAULT_CRASH_MARKER_PATH,
        logger: logging.Logger | None = None,
        device_query_timeout_seconds: float = DEFAULT_DEVICE_QUERY_TIMEOUT_SECONDS,
    ) -> None:
        self.driver = driver
        self.crash_marker_path = Path(crash_marker_path)
        self._logger = logger or _LOGGER
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="multiflo")
        if device_query_timeout_seconds <= 0:
            raise ValueError("device query timeout must be positive")
        self.device_query_timeout_seconds = device_query_timeout_seconds
        self._runs: dict[UUID, _RunRecord] = {}
        self._requests: dict[str, UUID] = {}
        self._active_run_id: UUID | None = None
        self._closed = False
        self._reconciliation_required = self.crash_marker_path.exists()
        self._controller_state = (
            ControllerState.RECONCILIATION_REQUIRED
            if self._reconciliation_required
            else ControllerState.DISCONNECTED
        )

    @property
    def state(self) -> ControllerState:
        with self._lock:
            return self._controller_state

    @property
    def reconciliation_required(self) -> bool:
        with self._lock:
            return self._reconciliation_required

    @property
    def active_run_id(self) -> UUID | None:
        with self._lock:
            return self._active_run_id

    def start(
        self,
        protocol: Protocol,
        *,
        operator_confirmed_idle: bool,
        request_id: str,
    ) -> StartResult:
        """Accept one run, or return the existing run for a repeated request ID."""

        with self._lock:
            if self._closed:
                raise BusyError("runner is closed")
            existing_id = self._requests.get(request_id)
            if existing_id is not None:
                existing = self._runs[existing_id]
                if existing.protocol != protocol:
                    raise BusyError(
                        f"request ID {request_id!r} was already used for a "
                        "different protocol"
                    )
                duplicate = StartResult(existing.status(), duplicate=True)
                self._log_event(
                    "duplicate_request_suppressed",
                    run_id=str(existing_id),
                    request_id=request_id,
                )
                return duplicate
            if self._active_run_id is not None:
                # An active run legitimately owns a marker, so this check comes
                # before the retained-marker check.
                raise BusyError("another protocol run is active")
            if self.crash_marker_path.exists():
                self._reconciliation_required = True
                self._controller_state = ControllerState.RECONCILIATION_REQUIRED
            if self._reconciliation_required:
                raise BusyError(
                    "startup reconciliation is required before another protocol run"
                )
            run_id = uuid4()
            record = _RunRecord(run_id, request_id, protocol)
            self._runs[run_id] = record
            self._requests[request_id] = run_id
            self._active_run_id = run_id
            self._controller_state = ControllerState.RUNNING
            self._log_event(
                "run_queued",
                run_id=str(run_id),
                request_id=request_id,
                protocol_name=protocol.name,
                total_steps=len(protocol.steps),
            )
            self._executor.submit(self._execute, record, operator_confirmed_idle)
            status = record.status()
        return StartResult(status, duplicate=False)

    def get(self, run_id: UUID) -> RunStatus | None:
        with self._lock:
            record = self._runs.get(run_id)
            return None if record is None else record.status()

    def abort(self, run_id: UUID) -> RunStatus | None:
        """Request a cooperative stop between steps.

        There is no verified instrument-side cancellation command, so this
        cannot interrupt a step that is already in flight.
        """

        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            if record.state not in TERMINAL_STATES:
                record.abort_requested = True
                record.state = RunState.ABORTING
                self._controller_state = ControllerState.ABORTING
            status = record.status()
        self._log_event("abort_requested", run_id=str(run_id))
        return status

    def describe_device(self) -> DeviceStatus:
        """Run one read-only identity/state inventory on the owned worker.

        This never sends a motion command and never clears a crash marker. A
        failed probe is reported as a disconnected status, not an exception,
        so an operator can diagnose the instrument through the API.
        """

        with self._lock:
            if self._closed:
                raise BusyError("runner is closed")
            if self._active_run_id is not None:
                raise BusyError(
                    "a protocol run owns the instrument; device inspection is "
                    "unavailable until it finishes"
                )
        future = self._executor.submit(self._read_device_status)
        try:
            return future.result(timeout=self.device_query_timeout_seconds)
        except FutureTimeoutError as error:
            raise BusyError(
                "device inspection did not complete within "
                f"{self.device_query_timeout_seconds} seconds"
            ) from error

    def _read_device_status(self) -> DeviceStatus:
        identity: DeviceIdentity | None = None
        modules: DeviceModules | None = None
        program_step_state: str | None = None
        program_step_error_code: int | None = None
        error: str | None = None
        connected = False
        try:
            self.driver.open()
            info = self.driver.inspect_device()
            status = self.driver.query_program_step_status()
            connected = True
            identity = DeviceIdentity(
                product_serial_number=info.product_serial_number,
                part_number=info.basecode.part_number,
                software_version=info.basecode.software_version,
                data_version=info.basecode.data_version,
                ui_version=info.basecode.ui_version,
                motion_controller_version=info.basecode.motion_controller_version,
            )
            modules = DeviceModules(
                primary_peristaltic=info.modules.primary_peristaltic,
                secondary_peristaltic=info.modules.secondary_peristaltic,
                half_microliter_supported=info.modules.half_microliter_supported,
                primary_cassette=info.modules.primary_cassette,
            )
            program_step_state = status.state.name.lower()
            program_step_error_code = status.error_code
        except Exception as probe_error:  # Reported, never raised to the client.
            error = f"{type(probe_error).__name__}: {probe_error}"
            try:
                self.driver.close()
            except Exception:  # pragma: no cover - close failures are not useful here.
                pass

        expected = getattr(self.driver, "expected_product_serial", None)
        with self._lock:
            if not self._closed and self._active_run_id is None:
                if connected and not self._reconciliation_required:
                    self._controller_state = ControllerState.IDLE
                elif not connected and self._controller_state is ControllerState.IDLE:
                    self._controller_state = ControllerState.DISCONNECTED
            controller_state = self._controller_state
            reconciliation_required = self._reconciliation_required
            active_run_id = self._active_run_id
        self._log_event(
            "device_inspected",
            connected=connected,
            controller_state=controller_state.value,
            program_step_state=program_step_state,
            error=error,
        )
        return DeviceStatus(
            connected=connected,
            controller_state=controller_state,
            reconciliation_required=reconciliation_required,
            active_run_id=active_run_id,
            expected_product_serial=expected,
            serial_matches_expected=bool(
                identity is not None
                and expected
                and identity.product_serial_number == expected
            ),
            identity=identity,
            modules=modules,
            program_step_state=program_step_state,
            program_step_error_code=program_step_error_code,
            error=error,
        )

    def reconcile_startup(
        self,
        *,
        operator_acknowledged: bool = False,
    ) -> ControllerState:
        """Clear a crash marker after read-only Ready or explicit acknowledgement."""

        with self._lock:
            if self._closed:
                raise BusyError("runner is closed")
            if self._active_run_id is not None:
                raise BusyError("cannot reconcile while a protocol run is active")
            marker_exists = self.crash_marker_path.exists()
            if not marker_exists and not self._reconciliation_required:
                return self._controller_state

        if operator_acknowledged:
            self._clear_crash_marker()
            with self._lock:
                self._reconciliation_required = False
                self._controller_state = ControllerState.DISCONNECTED
                state = self._controller_state
            self._log_event("startup_reconciled", method="operator_acknowledgement")
            return state

        self.driver.open()
        status = self.driver.query_program_step_status()
        if status.state is not ProgramStepState.READY:
            raise BusyError(
                f"instrument state is {status.state.name.lower()}, not ready; "
                "crash marker retained"
            )
        self._clear_crash_marker()
        with self._lock:
            self._reconciliation_required = False
            self._controller_state = ControllerState.IDLE
            state = self._controller_state
        self._log_event("startup_reconciled", method="device_ready")
        return state

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)
        self.driver.close()
        with self._lock:
            self._controller_state = ControllerState.CLOSED

    def _execute(self, record: _RunRecord, operator_confirmed_idle: bool) -> None:
        driver_opened = False
        marker_started = False
        try:
            with self._lock:
                abort_before_open = record.abort_requested
                if not abort_before_open:
                    record.state = RunState.RUNNING
            if abort_before_open:
                self._finish_without_reconciliation(record, RunState.ABORTED)
                return
            self._log_event(
                "run_started",
                run_id=str(record.run_id),
                protocol_name=record.protocol.name,
            )

            self.driver.open()
            driver_opened = True
            self.driver.authorize_motion(
                operator_confirmed_idle=operator_confirmed_idle,
            )
            requires_peristaltic = any(
                isinstance(
                    step,
                    (PeristalticDispense, PeristalticPrime, PeristalticPurge),
                )
                for step in record.protocol.steps
            )
            self.driver.prepare_motion(
                require_primary_peristaltic=requires_peristaltic,
            )
            self.driver.validate_protocol(record.protocol)

            with self._lock:
                abort_before_motion = record.abort_requested
            if abort_before_motion:
                self._finish_without_reconciliation(record, RunState.ABORTED)
                return

            self._write_crash_marker(record, current_step=None)
            marker_started = True

            for index, step in enumerate(record.protocol.steps):
                with self._lock:
                    if record.abort_requested:
                        abort_before_step = True
                    else:
                        abort_before_step = False
                        record.current_step = index
                if abort_before_step:
                    self._finish_without_reconciliation(record, RunState.ABORTED)
                    return

                self._write_crash_marker(record, current_step=index)
                self._log_event(
                    "step_started",
                    run_id=str(record.run_id),
                    step_index=index,
                    operation=step.operation,
                    request=step.model_dump(mode="json"),
                )
                exchange = self._execute_step(step)
                result = StepResult(
                    step_index=index,
                    operation=step.operation,
                    device_status=int.from_bytes(exchange.response.body[:2], "little"),
                    indication_count=len(exchange.indications),
                )
                with self._lock:
                    record.results.append(result)
                    record.completed_steps += 1
                    record.current_step = None
                    abort_after_step = record.abort_requested
                self._write_crash_marker(record, current_step=None)
                self._log_event(
                    "step_completed",
                    run_id=str(record.run_id),
                    step_index=index,
                    operation=step.operation,
                    response={
                        "device_status": result.device_status,
                        "indication_count": result.indication_count,
                        "completion_status": "ready",
                    },
                )
                if abort_after_step:
                    self._finish_without_reconciliation(record, RunState.ABORTED)
                    return

            self._finish_without_reconciliation(record, RunState.COMPLETED)
        except UnknownExecutionState as error:
            self._finish_with_error(
                record,
                RunState.UNKNOWN_EXECUTION_STATE,
                error,
                reconciliation_required=marker_started,
                driver_opened=driver_opened,
            )
        except Exception as error:
            self._finish_with_error(
                record,
                RunState.FAILED,
                error,
                reconciliation_required=marker_started,
                driver_opened=driver_opened,
            )
        finally:
            with self._lock:
                if self._active_run_id == record.run_id:
                    self._active_run_id = None

    def _execute_step(self, step: ProtocolStep) -> ExchangeResult:
        if isinstance(step, PeristalticDispense):
            return self.driver.peristaltic_dispense(step)
        if isinstance(step, PeristalticPrime):
            return self.driver.peristaltic_prime(step)
        if isinstance(step, PeristalticPurge):
            return self.driver.peristaltic_purge(step)
        if isinstance(step, Shake):
            return self.driver.shake(step)
        if isinstance(step, Soak):
            return self.driver.soak(step)
        raise TypeError(f"unsupported protocol step {type(step).__name__}")

    def _finish_without_reconciliation(
        self,
        record: _RunRecord,
        state: RunState,
    ) -> None:
        self._clear_crash_marker()
        with self._lock:
            record.state = state
            record.current_step = None
            self._reconciliation_required = False
            self._controller_state = ControllerState.IDLE
        self._log_event(
            "run_terminal",
            run_id=str(record.run_id),
            state=state.value,
            completed_steps=record.completed_steps,
        )

    def _finish_with_error(
        self,
        record: _RunRecord,
        state: RunState,
        error: Exception,
        *,
        reconciliation_required: bool,
        driver_opened: bool,
    ) -> None:
        with self._lock:
            record.state = state
            record.error = str(error)
            if not reconciliation_required:
                record.current_step = None
            if reconciliation_required or self.crash_marker_path.exists():
                self._reconciliation_required = True
                self._controller_state = ControllerState.RECONCILIATION_REQUIRED
            else:
                self._controller_state = (
                    ControllerState.IDLE
                    if driver_opened
                    else ControllerState.DISCONNECTED
                )
        self._log_event(
            "run_terminal",
            run_id=str(record.run_id),
            state=state.value,
            completed_steps=record.completed_steps,
            error_type=type(error).__name__,
            error=str(error),
            reconciliation_required=self._reconciliation_required,
        )

    def _write_crash_marker(
        self,
        record: _RunRecord,
        *,
        current_step: int | None,
    ) -> None:
        last_confirmed_step = (
            record.completed_steps - 1 if record.completed_steps else None
        )
        marker = _CrashMarker(
            run_id=record.run_id,
            protocol_name=record.protocol.name,
            current_step=current_step,
            last_confirmed_step=last_confirmed_step,
            updated_at=datetime.now(timezone.utc),
        )
        self.crash_marker_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.crash_marker_path.with_name(
            f"{self.crash_marker_path.name}.tmp"
        )
        temporary_path.write_text(
            json.dumps(marker.model_dump(mode="json"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, self.crash_marker_path)

    def _clear_crash_marker(self) -> None:
        self.crash_marker_path.unlink(missing_ok=True)

    def _log_event(self, event: str, **fields: object) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        self._logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))
