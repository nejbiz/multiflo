"""Single-worker sequential execution for the Phase 2 vertical slice."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from threading import Lock
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .driver import MultiFloDriver
from .errors import BusyError, UnknownExecutionState
from .models import Protocol


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


class _RunRecord:
    def __init__(self, run_id: UUID, protocol: Protocol) -> None:
        self.run_id = run_id
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
            state=self.state,
            total_steps=len(self.protocol.steps),
            completed_steps=self.completed_steps,
            current_step=self.current_step,
            error=self.error,
            results=list(self.results),
        )


class ProtocolRunner:
    """Own one driver and execute at most one protocol at a time."""

    def __init__(self, driver: MultiFloDriver) -> None:
        self.driver = driver
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="multiflo")
        self._runs: dict[UUID, _RunRecord] = {}
        self._active_run_id: UUID | None = None
        self._closed = False

    def start(self, protocol: Protocol, *, operator_confirmed_idle: bool) -> RunStatus:
        with self._lock:
            if self._closed:
                raise BusyError("runner is closed")
            if self._active_run_id is not None:
                raise BusyError("another protocol run is active")
            run_id = uuid4()
            record = _RunRecord(run_id, protocol)
            self._runs[run_id] = record
            self._active_run_id = run_id
            self._executor.submit(self._execute, record, operator_confirmed_idle)
            return record.status()

    def get(self, run_id: UUID) -> RunStatus | None:
        with self._lock:
            record = self._runs.get(run_id)
            return None if record is None else record.status()

    def abort(self, run_id: UUID) -> RunStatus | None:
        """Request a cooperative stop between steps.

        Phase 2 has no verified instrument-side cancellation command, so this
        cannot interrupt a dispense that is already in flight.
        """

        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            if record.state not in TERMINAL_STATES:
                record.abort_requested = True
                record.state = RunState.ABORTING
            return record.status()

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)
        self.driver.close()

    def _execute(self, record: _RunRecord, operator_confirmed_idle: bool) -> None:
        try:
            with self._lock:
                record.state = RunState.RUNNING
            self.driver.open()
            self.driver.authorize_motion(
                operator_confirmed_idle=operator_confirmed_idle,
            )
            self.driver.prepare_motion()
            for index, step in enumerate(record.protocol.steps):
                with self._lock:
                    if record.abort_requested:
                        record.state = RunState.ABORTED
                        record.current_step = None
                        return
                    record.current_step = index
                exchange = self.driver.peristaltic_dispense(step)
                with self._lock:
                    record.results.append(
                        StepResult(
                            step_index=index,
                            operation="peristaltic_dispense",
                            device_status=int.from_bytes(
                                exchange.response.body[:2], "little"
                            ),
                            indication_count=len(exchange.indications),
                        )
                    )
                    record.completed_steps += 1
                    record.current_step = None
                    if record.abort_requested:
                        record.state = RunState.ABORTED
                        return
            with self._lock:
                record.state = RunState.COMPLETED
        except UnknownExecutionState as error:
            with self._lock:
                record.state = RunState.UNKNOWN_EXECUTION_STATE
                record.error = str(error)
                record.current_step = None
        except Exception as error:
            with self._lock:
                record.state = RunState.FAILED
                record.error = str(error)
                record.current_step = None
        finally:
            with self._lock:
                if self._active_run_id == record.run_id:
                    self._active_run_id = None
