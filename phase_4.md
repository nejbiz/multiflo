# Phase 4 - Sequential execution and failure containment

Date: 2026-08-26 (Europe/Zurich)

## Outcome

Phase 4 completes the software safety layer around ordered mixed-operation
protocols. Before the first motion, the runner now validates the entire protocol
against fresh device inventory. During execution it records structured run and
step events and maintains one atomic crash marker containing the active run,
current step, and last confirmed step. A retained marker blocks all later runs
until explicit startup reconciliation.

No new `.LHC` protocol was required. The LHC fixtures establish individual
command bodies; the project-owned runner composes those typed steps. The mixed
prime, purge, shake, and soak path is verified end to end through FastAPI and a
scripted transport.

The MultiFlo was physically disconnected for the entire phase. No hardware
connection, read-only query, or motion command was attempted. All Phase 4
verification used models, unit tests, FastAPI tests, and scripted fake
transports. Phase 3 already established the individual command lifecycle on the
connected instrument. A future guarded mixed hardware run may be useful as
final integration evidence, but it is not needed to recover or implement
multi-step orchestration.

## Whole-protocol preflight

A run still begins with the Phase 3 communication, Ready-state, serial, module,
and cassette inventory preflight. Phase 4 adds a second pass over every protocol
step before creating a crash marker or sending Start Batch.

For all peristaltic steps this pass verifies:

- the primary peristaltic pump is installed;
- the instrument reports a concrete 1, 5, or 10 uL cassette;
- every explicit cassette requirement matches that installed cassette; and
- dispense and pre-dispense volumes are valid for the actual installed cassette,
  including requests declared with cassette type `any`.

Plate types, units, position maps, row sections, X/Y bounds, durations, and
operation-specific ranges are already enforced by the strict Pydantic models.
Only plate types and map layouts with encoders are members of those models.

The important behavioral guarantee is all-or-nothing preflight: an incompatible
later step fails the run before the first motion. The API regression uses a
mixed protocol whose first step matches the installed 5 uL cassette and whose
second step requires 1 uL. It performs the read-only inventory, reports
`failed`, completes zero steps, and emits no Start Batch or operation command.

## Sequential execution

One `ProtocolRunner` owns one driver and one worker thread. It permits one active
run and executes typed steps in list order. Each step independently uses the
verified sequence:

```text
Start Batch -> operation -> Program Step Status until Ready -> End Batch
```

The next step cannot begin before the current step has completed that sequence.
Step results record the zero/nonzero device status and indication count. Run
polling reports total steps, completed steps, current step, terminal error, and
the ordered results accumulated so far.

The mixed FastAPI regression covers primary peristaltic prime, primary
peristaltic purge, shake, and soak in one submitted protocol. The fake transport
asserts the exact command and message order for all four steps.

## State model

Controller-level states describe whether the runner can accept work:

| State | Meaning |
| --- | --- |
| `disconnected` | No active run; the driver has not established a live session |
| `idle` | Driver session available and no run active |
| `running` | A queued or executing run owns the controller |
| `aborting` | Cooperative abort requested; an in-flight step may still finish |
| `reconciliation_required` | A crash/uncertain marker blocks all new runs |
| `closed` | Runner shut down and cannot accept work |

Per-run states are `queued`, `running`, `aborting`, `completed`, `aborted`,
`failed`, and `unknown_execution_state`.

Pause/resume is not exposed. The driver can decode a device-side Paused status,
but no pause or resume command has been recovered and verified. It therefore
does not claim a controllable paused run state.

## Cooperative abort

Abort remains deliberately cooperative because there is no verified
instrument-side cancellation command.

- An abort before motion prevents the first step from starting.
- An abort during a step changes the visible state to `aborting` but does not
  send a cancellation command or close the transport.
- The in-flight step completes normally through status polling and End Batch.
- The runner records that completed step, marks the run `aborted`, and does not
  start the next step.
- An already-terminal run is not changed.

The regression test blocks the first of two dispense steps, requests abort,
then releases the step. Exactly one driver motion call occurs, one step is
reported complete, the second step never starts, and the crash marker is safely
removed.

## Persistent crash marker

The default marker is `.multiflo-active-run.json` in the process working
directory. The path is configurable when constructing `ProtocolRunner`, and a
deployment should give it a stable writable location shared by successive
instances of the one-worker service. The marker and its temporary file are
excluded from Git.

The marker is written atomically before the first motion and before each step.
It is updated after every fully confirmed step. Its small JSON schema contains:

```json
{
  "version": 1,
  "run_id": "UUID",
  "protocol_name": "name",
  "current_step": 1,
  "last_confirmed_step": 0,
  "updated_at": "UTC timestamp"
}
```

The file is removed only after a normal `completed` or safely cooperative
`aborted` terminal state. A failure after motion may have begun, a timeout, a
disconnect, or `unknown_execution_state` retains it. The API also keeps the
uncertain step index visible in the run status.

If the process stops during the second step after confirming the first, a new
runner sees `current_step: 1` and `last_confirmed_step: 0`. It refuses every new
run with a conflict response. It never reconstructs, resumes, or replays the
interrupted protocol automatically.

## Startup reconciliation

A retained marker has two explicit resolution paths:

1. **Read-only device reconciliation.** With LHC closed, open the exact FTDI
   device and query Program Step Status. The marker is cleared only if the
   device reports Ready. Busy, Paused, Error, Stopped, a transport failure, or
   an unrecognized response leaves the marker intact.
2. **Operator reconciliation.** After physically inspecting and reconciling the
   instrument, an operator may explicitly acknowledge that work. This clears
   the marker without claiming a device query succeeded. The next run still
   performs the full fresh motion preflight.

The guarded helper is:

```powershell
# Preferred read-only Ready-state reconciliation:
uv run python -m multiflo.tools.hardware_reconcile --expected-serial 14071419

# Only after physical reconciliation when read-only reconciliation cannot be used:
uv run python -m multiflo.tools.hardware_reconcile --expected-serial 14071419 --operator-acknowledgement INTERRUPTED_RUN_PHYSICALLY_RECONCILED
```

If the device is physically stationary but remains Busy because of a stale
batch, the separate guarded one-shot End Batch recovery from Phase 3 must be
used with its physical authorization. Reconciliation never sends End Batch on
its own.

## Structured logs

The runner emits one-line JSON through the standard `multiflo.runner` Python
logger. It does not configure a global handler; the host service controls log
destination and retention.

Events are:

- `run_queued`;
- `run_started`;
- `step_started` with run ID, step index, operation, timestamp, and validated
  typed request;
- `step_completed` with device status, indication count, and Ready completion;
- `abort_requested`;
- `run_terminal` with state, completed count, and error details where present;
  and
- `startup_reconciled` with the reconciliation method.

The logs intentionally record typed protocol parameters and response summaries,
not arbitrary raw command injection.

## Failure behavior

| Failure point | Result |
| --- | --- |
| Schema or whole-protocol preflight failure | `failed`; no motion and no crash marker |
| Definite error after motion begins | `failed`; marker retained for reconciliation |
| Transport/protocol uncertainty after batch begins | `unknown_execution_state`; marker retained; no retry |
| Timeout waiting for Ready | `unknown_execution_state`; marker retained; no retry |
| Process restart with marker | Controller enters `reconciliation_required`; API start returns conflict |
| Cooperative abort between steps | `aborted`; confirmed progress retained; marker removed |
| Normal completion | `completed`; marker removed; controller returns to `idle` |

This separates a known pre-motion validation failure from any condition that
might otherwise duplicate physical work after restart.

## Automated verification

The final Phase 4 suite contains 65 tests on Python 3.13.15:

```powershell
uv run python -m unittest discover -s tests -v
```

In addition to earlier codec, transport, driver, and hardware-regression
coverage, Phase 4 verifies:

- whole-protocol cassette/inventory validation before motion;
- exact mixed-operation ordering through FastAPI;
- cooperative abort during an in-flight first step and suppression of the
  second step;
- atomic marker progress with current and last-confirmed step indices;
- retained-marker blocking after an uncertain second step;
- Ready-state reconciliation and Busy-state refusal;
- explicit operator acknowledgement without an implicit hardware query;
- API conflict with zero transport activity when a marker exists; and
- structured request, response, completion, state, and timestamp log fields.

The Starlette `httpx` TestClient deprecation warning remains non-failing.

## Boundaries carried into Phase 5

- The current API does not yet expose process health or a device/controller
  status resource.
- Startup reconciliation is available through the runner and guarded command
  tool, not a general remote acknowledgement endpoint.
- API request-ID deduplication has not been added.
- Pause/resume and instrument-side cancellation remain unsupported.
- Structured logs require the host to configure a handler and retention policy.
- The in-memory run history is process-local; only the active crash marker is
  persistent, by design.

Within these boundaries, the Phase 4 completion condition is met: mixed
protocols execute sequentially, the full protocol is checked before motion,
abort cannot silently start a later step, and timeout, disconnect, or restart
cannot automatically replay uncertain physical work.
