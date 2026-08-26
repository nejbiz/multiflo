# Phase 5 - Finalized FastAPI boundary

Date: 2026-08-26 (Europe/Zurich)

## Outcome

Phase 5 closes the API surface described in the plan. The service now exposes
process health, a verified read-only device resource, protocol validation, run
creation with client-supplied request IDs, run polling, and cooperative abort.
Nothing else is reachable: there is no raw command, packet, firmware, or
maintenance endpoint, and no pause/resume endpoint.

The two behavioral additions are request-ID deduplication, which makes a
repeated `POST /v1/runs` return the original run instead of starting duplicate
physical work, and a read-only `GET /v1/device` inventory that runs on the same
single worker that owns the USB handle.

A guarded end-to-end check was run against the connected instrument through a
real loopback Uvicorn server, including one authorized 5-second shake. Both the
read-only pass and the motion pass succeeded.

## API surface

| Method | Path | Behavior |
| --- | --- | --- |
| `GET` | `/v1/health` | Process health from in-memory controller state; sends no command |
| `GET` | `/v1/device` | Read-only identity, installed modules, connection, and program-step state |
| `POST` | `/v1/protocols/validate` | Strict model validation; never opens the transport |
| `POST` | `/v1/runs` | Accepts a protocol plus `request_id`; returns a run ID immediately |
| `GET` | `/v1/runs/{run_id}` | State, current step, completed steps, errors, and step results |
| `POST` | `/v1/runs/{run_id}/abort` | Cooperative abort between steps |

Pause and resume are deliberately absent. Phase 4 did not verify an
instrument-side pause command, so the plan's conditional endpoints were not
added. The driver still decodes a device-side Paused status for diagnosis.

All schemas are strict Pydantic models with `extra="forbid"`, which the
generated OpenAPI document reports as `additionalProperties: false`. The
document is generated from the same models the runner uses; it can be exported
without hardware:

```powershell
uv run python -m multiflo.tools.export_openapi --output openapi.json
```

## Request-ID deduplication

`POST /v1/runs` now requires a client-supplied `request_id` of 1-64 characters
matching `^[A-Za-z0-9][A-Za-z0-9._:-]*$`. The runner keeps a process-lifetime
index from request ID to run ID and resolves it under the same lock that admits
runs:

| Case | Result |
| --- | --- |
| New request ID | `202 Accepted` with a new run ID |
| Repeated request ID, identical protocol | `200 OK` with the original run status; no new motion |
| Repeated request ID, different protocol | `409 Conflict` |
| New request ID while another run is active | `409 Conflict` |
| Malformed or missing request ID | `422` before the transport is touched |

Deduplication is intentionally process-local, matching the Phase 4 decision to
persist only the active-run crash marker. A restart clears the index, and a
retained marker then blocks new runs until reconciliation, so a replayed request
after a crash cannot silently repeat motion either.

A duplicate suppression is logged as `duplicate_request_suppressed` with the
original run ID.

## Device resource

`GET /v1/device` performs one read-only inventory: product serial, basecode
versions, installed peristaltic pumps, half-microliter support, the onboard
cassette setting, and the authoritative Program Step Status. It sends no batch,
motion, or configuration command and never clears a crash marker.

The query is submitted to the runner's single worker thread, so it cannot
interleave with a protocol run on the shared USB handle. If a run owns the
controller the endpoint returns `409` instead of queueing behind it, and a probe
that exceeds the runner's device-query timeout also returns `409`.

A failed probe is reported rather than raised: the response carries
`connected: false` with the failure text in `error`, so an operator can diagnose
an absent instrument, a busy device, or a serial mismatch through the API. The
transport is closed after a failed probe so the next attempt reopens and purges
cleanly. `serial_matches_expected` compares the reported product serial against
the configured motion allowlist.

## Ordering fix in run admission

While adding the conflict tests, `start()` was found to report the wrong reason
for a second concurrent run: the retained-marker check ran before the active-run
check, and an active run legitimately owns a marker. A run started during
another run therefore reported "startup reconciliation is required". The active
run check now comes first, so a concurrent start reports the actual conflict
while a genuinely retained marker still blocks new runs after a restart. Both
paths return `409`.

## Service entry point

`multiflo.service` builds one transport, one driver, one runner, and one
application, then runs Uvicorn with a single worker:

```powershell
uv run python -m multiflo.service --expected-serial 14071419 --host 127.0.0.1 --port 8000
```

The host must be a loopback address unless `--allow-non-loopback` is passed
explicitly, and the worker count is fixed at one because several workers would
compete for one USB handle. `build_service` is also importable, which is how the
end-to-end tool starts a real server in-process.

## Execution guarantees at the boundary

- Validation failures are answered by the models; the transport is never opened
  and no bytes are written.
- Run creation returns immediately; execution continues on the runner's worker.
- An HTTP timeout, a closed client, or an abandoned poll never cancels, retries,
  or replays motion. A reconnecting client polls the same run ID and sees the
  confirmed progress.
- Abort stays cooperative: the in-flight step finishes through status polling
  and End Batch, and the next step never starts.
- `unknown_execution_state` still retains the crash marker and blocks new runs
  until reconciliation.

## Automated verification

The Phase 5 suite contains 79 tests on Python 3.13.15:

```powershell
uv run python -m unittest discover -s tests -v
```

The new contract tests verify:

- the generated OpenAPI document exposes exactly the six documented routes, with
  no raw/packet/command/firmware/pause/resume surface, and strict schemas;
- health reports controller state, active run, and a retained marker without
  opening the transport or writing a byte;
- the device resource returns identity, modules, and `ready`, with the exact
  read-only command order and no communication-test or motion command;
- a missing instrument is reported as `connected: false` with an error instead of
  a raised exception;
- a repeated request ID returns the original completed run with `200` while the
  scripted transport shows no second motion sequence;
- a reused request ID with a different protocol, a second run while one is
  active, and a device query during a run are all `409`;
- a client that disconnects mid-step does not change execution, and a new client
  sees the completed run;
- malformed request IDs are rejected with `422` and no transport activity; and
- unknown run IDs are `404` while a malformed UUID is `422`.

`tests/test_service.py` covers loopback classification and confirms that a
non-loopback bind exits before the device is opened.

Existing API tests were also given per-test crash-marker paths. They previously
shared the default working-directory marker, which made them order-dependent and
could leave a marker in the repository after an interrupted run.

The Starlette `httpx` TestClient deprecation warning remains non-failing.

## Guarded hardware evidence

Both runs used the exact FTDI/product serial `14071419` with LHC closed, driving
a real Uvicorn server on `127.0.0.1` over HTTP through `httpx`, not an in-process
test client.

| Check | Result |
| --- | --- |
| Read-only pass (`--motion none`) | Health `ok`; device connected with serial `14071419`, part `7210200`, software `1.12`, primary peristaltic installed, no secondary, 1 uL cassette, state `ready`; validate `200`/`422`; unknown run `404` |
| Motion pass (`--motion shake --duration-seconds 5`) | Start `202`, repeated request ID `200` returning the same run ID, conflicting start `409`, run `completed` with device status `0` and no indications, device `ready` afterwards, no crash marker retained |

The motion run was `5578c6e1-654a-48f9-890e-6782a57ca4e0` with request ID
`phase5-shake-2529fd7d2f07`. The repeated request returned that same run ID and
produced no second shake.

This is boundary and lifecycle evidence, not a calibration or volumetric study.

## Boundaries carried forward

- Request-ID deduplication is process-local by design; there is no persistent
  request store.
- Startup reconciliation remains a guarded command tool, not an HTTP endpoint.
  Clearing a marker still requires local operator action.
- Pause/resume and instrument-side cancellation are still unsupported.
- The device resource reports the onboard cassette setting but the API can never
  change it.
- The end-to-end hardware evidence covers a shake step. Peristaltic dispense,
  prime, and purge remain covered by the Phase 3 guarded single-step evidence
  and offline fixtures.
- Authentication, TLS, remote binding policy, and multi-process scaling stay out
  of scope; the service binds loopback with one worker.
- The in-memory run history is process-local; only the active crash marker
  persists.

With these boundaries, the Phase 5 completion condition is met: the API contract
tests pass against the fake transport, and the guarded end-to-end test passed
against the real instrument.
