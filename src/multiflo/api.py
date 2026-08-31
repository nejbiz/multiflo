"""Minimal FastAPI boundary for validated sequential MultiFlo protocols."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from .errors import BusyError, PreconditionError
from .models import Protocol
from .runner import DeviceStatus, ProtocolRunner, RequestId, RunStatus


API_VERSION = "1.0.0"
API_DESCRIPTION = """Pure Python control surface for one base BioTek MultiFlo.

The API exposes only validated protocol operations. There is no raw command,
packet, firmware, or unvalidated maintenance endpoint. Pause and resume are not
exposed because no instrument-side pause command has been recovered and
verified. Abort is cooperative between steps and cannot cancel motion that is
already in flight.
"""


class HealthStatus(BaseModel):
    """Process health. Answering this never commands the instrument."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    service: str = "multiflo"
    api_version: str = API_VERSION
    controller_state: str
    reconciliation_required: bool
    active_run_id: UUID | None = None
    # Present when a previous run was interrupted, so an operator can see what
    # is blocking new work without reading the marker file.
    retained_run_id: UUID | None = None
    retained_last_confirmed_step: int | None = None
    marker_unreadable: bool = False


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: Literal[True] = True
    protocol: Protocol


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId
    protocol: Protocol
    operator_confirmed_idle: Literal[True]


def create_app(runner: ProtocolRunner) -> FastAPI:
    app = FastAPI(
        title="Base MultiFlo Driver",
        version=API_VERSION,
        description=API_DESCRIPTION,
    )

    @app.get("/v1/health", response_model=HealthStatus, operation_id="get_health")
    def get_health() -> HealthStatus:
        retained = runner.retained_marker
        return HealthStatus(
            controller_state=runner.state.value,
            reconciliation_required=runner.reconciliation_required,
            active_run_id=runner.active_run_id,
            retained_run_id=None if retained is None else retained.run_id,
            retained_last_confirmed_step=(
                None if retained is None else retained.last_confirmed_step
            ),
            marker_unreadable=runner.marker_unreadable,
        )

    @app.get("/v1/device", response_model=DeviceStatus, operation_id="get_device")
    def get_device() -> DeviceStatus:
        try:
            return runner.describe_device()
        except BusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post(
        "/v1/protocols/validate",
        response_model=ValidationResult,
        operation_id="validate_protocol",
    )
    def validate_protocol(protocol: Protocol) -> ValidationResult:
        return ValidationResult(protocol=protocol)

    @app.post(
        "/v1/runs",
        response_model=RunStatus,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="start_run",
    )
    def start_run(request: StartRunRequest, response: Response) -> RunStatus:
        try:
            result = runner.start(
                request.protocol,
                operator_confirmed_idle=request.operator_confirmed_idle,
                request_id=request.request_id,
            )
        except BusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except PreconditionError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if result.duplicate:
            # A repeated request ID returns the original run instead of
            # starting duplicate physical work.
            response.status_code = status.HTTP_200_OK
        return result.status

    @app.get("/v1/runs/{run_id}", response_model=RunStatus, operation_id="get_run")
    def get_run(run_id: UUID) -> RunStatus:
        run = runner.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run

    @app.post(
        "/v1/runs/{run_id}/abort",
        response_model=RunStatus,
        operation_id="abort_run",
    )
    def abort_run(run_id: UUID) -> RunStatus:
        run = runner.abort(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run

    return app
