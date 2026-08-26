# Phase 2 - First end-to-end vertical slice

Date: 2026-08-26 (Europe/Zurich)

## Outcome

Phase 2 implements and hardware-verifies one narrow operation: a full-plate
primary peristaltic dispense to the proven 96-deep-well geometry. A validated
one-step protocol ran from JSON through FastAPI, the single-worker runner, the
pure-Python driver, and D2XX to base MultiFlo product serial `14071419`.

The instrument acknowledged the dispense and returned device status zero. The
pollable run result reported one completed step, no error, and no indications.
No raw-command API was added.

This phase does not implement any Phase 3 operations.

## Implemented slice

- `models.py`: strict Pydantic protocol and peristaltic-dispense models.
- `codec.py`: `0x008F` body encoder using the `calib1.LHC` fixture.
- `driver.py`: cassette query, serial/module/cassette motion preflight,
  peristaltic dispense, and ambiguous post-send failure handling.
- `runner.py`: one active protocol, one worker thread, ordered steps, pollable
  progress/results, and cooperative abort requests.
- `api.py`: validation, start, poll, and abort endpoints.
- `tools/hardware_dispense.py`: guarded one-shot motion test through FastAPI.

Supported request surface:

- primary peristaltic pump only;
- 96-deep-well plate only;
- full plate only;
- low and medium flow only;
- 1, 5, and 10 uL cassette declarations;
- dispense volume and paired pre-dispense volume/cycle settings; and
- one or more sequential dispense steps.

High flow, arbitrary plate maps, offsets, alternate plate types, secondary
pump, syringe/manifold operations, prime, purge, shake, wash, pause, resume,
and hardware cancellation remain outside this phase.

## Validation

The cassette limits recorded from the operator manual are enforced:

| Cassette | Volume range | Increment |
| --- | ---: | ---: |
| 1 uL | 1-50 uL | 1 uL |
| 5 uL | 5-2500 uL | 5 uL |
| 10 uL | 10-3000 uL | 10 uL |

The manual's special 0.5 uL behavior is intentionally unsupported. Pre-dispense
volume and cycle count must either both be zero or both be positive. Unknown
JSON fields are rejected.

Immediately before motion, the driver requires an operator idle/setup
confirmation and then performs a fresh read-only preflight:

1. communication test `0x0073`;
2. product serial query and exact match to `14071419`;
3. basecode and installed-module inventory;
4. primary peristaltic installation check; and
5. configured cassette query `0x0108` and request compatibility check.

No authoritative hardware busy/idle query has been recovered. The explicit
operator confirmation is the documented Phase 2 reconciliation mechanism, not
a claim that the device state can be queried authoritatively.

## Golden encoding

The 100 uL, medium-flow, any-cassette, 10 uL/two-cycle pre-dispense fixture
encodes to this 24-byte command body:

```text
05 64 00 01 00 00 00 A1 03 0A 00 02
FF FF FF FF FF FF 00 01 00 00 00 00
```

With command `0x008F` and message ID zero, the complete request is:

```text
01 02 8F 00 01 00 00 18 00 40 F8
05 64 00 01 00 00 00 A1 03 0A 00 02
FF FF FF FF FF FF 00 01 00 00 00 00
```

The guarded hardware request declared `5ul`, changing the cassette byte from
`00` to `02`; the instrument's own read-only setting also reported `5ul`.

## FastAPI behavior

Endpoints in this slice:

- `POST /v1/protocols/validate`
- `POST /v1/runs`
- `GET /v1/runs/{run_id}`
- `POST /v1/runs/{run_id}/abort`

Starting a run returns quickly and blocking USB work occurs on one worker
thread. Polling returns state, current/completed step counts, error information,
and per-step device status/indication count.

Abort is cooperative only. It sets an abort request and prevents subsequent
steps from starting, but it cannot cancel a dispense already in flight. The API
reports `abort_is_cooperative: true`. No physical abort capability is claimed.

If transport or packet validation fails after a motion request is sent, the run
becomes `unknown_execution_state`. The driver never retries that command.

## Automated verification

Command:

```powershell
uv run python -m unittest discover -s tests -v
```

Final result: 29 tests passed on Python 3.13.15.

Coverage includes:

- exact `calib1` command body and full request packet;
- cassette ranges, increments, pre-dispense pairing, and strict JSON fields;
- fresh preflight and exact serial/cassette guarding;
- successful motion exchange and post-send ambiguous failure;
- JSON-to-driver FastAPI execution and pollable result;
- validation failure without opening the transport; and
- cooperative abort behavior during an in-flight fake step.

The only emitted warning is a Starlette deprecation notice concerning its
current `httpx` TestClient integration; it does not affect test results.

## Hardware verification

Before motion, the operator confirmed that LHC was closed, the instrument was
idle, the calibrated 5 uL cassette and cover were installed with a matching
instrument setting, tubing was primed into a safe test liquid, a 96-deep-well
plate was seated with sufficient capacity, the motion area was clear, and the
emergency stop was accessible.

The guarded tool then sent exactly one API run:

```text
instrument product/FTDI serial: 14071419
configured cassette query:      5ul
plate:                           96 deep well, full plate
volume:                          100 uL per well
flow:                            medium
pre-dispense:                    10 uL, 2 cycles
run ID:                          16e51a0e-5cb7-48ec-8b91-ed9298122d48
final API state:                 completed
completed steps:                1 of 1
device status:                  0
indications:                    0
error:                           none
```

The command process completed in approximately 1.47 seconds. This verifies the
JSON-to-hardware request and zero-status response path. It is not a volumetric
calibration or accuracy measurement. No separate completion indication was
observed, so Phase 2 treats the matching zero-status command response as step
completion; response-versus-final-mechanical timing remains an explicit item to
revisit when a device status/indication model is recovered.

## Remaining uncertainties and safety boundaries

1. There is still no authoritative read-only busy/idle device query.
2. There is no verified instrument-side abort command; API abort is cooperative.
3. No separate completion indication was observed for the hardware dispense.
4. A client disconnect does not cancel or retry physical motion.
5. Any ambiguous post-send failure requires operator reconciliation before a
   new run.
6. Only the exact narrow peristaltic slice above is hardware-authorized.

Within those documented boundaries, the Phase 2 completion criterion is met:
a validated one-step dispense ran through FastAPI and its zero-status result was
polled without exposing raw commands.
