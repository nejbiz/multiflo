"""Minimal FastAPI boundary for the Phase 2 dispense slice."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict

from .errors import BusyError
from .models import Protocol
from .runner import ProtocolRunner, RunStatus


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: Literal[True] = True
    protocol: Protocol


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Protocol
    operator_confirmed_idle: Literal[True]


def create_app(runner: ProtocolRunner) -> FastAPI:
    app = FastAPI(title="Base MultiFlo Driver", version="0.2.0")

    @app.post("/v1/protocols/validate", response_model=ValidationResult)
    def validate_protocol(protocol: Protocol) -> ValidationResult:
        return ValidationResult(protocol=protocol)

    @app.post(
        "/v1/runs",
        response_model=RunStatus,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_run(request: StartRunRequest) -> RunStatus:
        try:
            return runner.start(
                request.protocol,
                operator_confirmed_idle=request.operator_confirmed_idle,
            )
        except BusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/v1/runs/{run_id}", response_model=RunStatus)
    def get_run(run_id: UUID) -> RunStatus:
        run = runner.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run

    @app.post("/v1/runs/{run_id}/abort", response_model=RunStatus)
    def abort_run(run_id: UUID) -> RunStatus:
        run = runner.abort(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run

    return app

