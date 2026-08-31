# MultiFlo

Pure Python driver work for the base BioTek MultiFlo connected through FTDI D2XX.

## Layout

```text
src/multiflo/       the driver package (models, codec, transport, driver,
                    runner, api, service, errors, logs)
src/multiflo/tools/ guarded hardware tools and offline .LHC helpers
tests/              outside the package, so tests are not shipped in a wheel
protocols/          controlled .LHC fixtures and their change log
```

Import paths are unchanged by the src layout: the package is still `multiflo`
and the tools are still `python -m multiflo.tools.<name>`. The `phase_*.md`
reports predate the move and refer to the old top-level `tools/` path.

## Development

Create/update the locked environment and run the test suite:

```powershell
uv sync
uv run python -m unittest discover -s tests -v
```

The hardware scripts are opt-in and require the expected instrument serial. Close Liquid Handling Control before using them:

```powershell
uv run python -m multiflo.tools.hardware_smoke --expected-serial 14071419
uv run python -m multiflo.tools.hardware_inventory --expected-serial 14071419
```

Read `phase_0.md` through `phase_5.md` before hardware work. The Phase 0 and
Phase 1 procedures do not authorize motion. The Phase 3 guarded tool supports
one explicitly authorized step at a time, and Phase 4 defines restart
reconciliation.

The currently implemented operation models are primary peristaltic dispense,
prime, purge, shake, and soak. Motion preflight requires the read-only device
program-step state to be `ready`. Each step uses the recovered Start Batch,
status-polling, and End Batch lifecycle; an accepted command is not reported as
complete until the device returns to `ready`.

Offline fixture coverage includes 384-well primary peristaltic dispense and
prime, both 384-well row sections (calib25 odd, calib30 even), and partial
column maps for every supported geometry (calib3 deep-well, calib31/calib32
384-well). Only the odd-row 384-well dispense has hardware evidence. The driver
verifies cassette compatibility but never changes the instrument's cassette
setting automatically.

Every peristaltic dispense supports signed fine-positioning offsets in instrument
steps. `x_offset_steps` is limited to -60 (left) through 60 (right), and
`y_offset_steps` is limited to -40 (back) through 40 (forward), matching the
operator manual. Both default to centered (`0`) and are independent of cassette
type.

## API

The documented endpoints are:

- `GET /v1/health` - process health only; it never commands the instrument
- `GET /v1/device` - verified identity, installed modules, connection, program-step state
- `POST /v1/protocols/validate`
- `POST /v1/runs` - requires a client-supplied `request_id`
- `GET /v1/runs/{run_id}`
- `POST /v1/runs/{run_id}/abort`

There is no raw command, packet, firmware, or maintenance endpoint, and no
pause/resume endpoint, because no instrument-side pause command has been
recovered and verified.

Every `POST /v1/runs` needs a `request_id` of 1-64 characters matching
`^[A-Za-z0-9][A-Za-z0-9._:-]*$`. Repeating a request ID with the same protocol
returns the original run with `200` instead of starting duplicate motion.
Reusing it with a different protocol, or starting while another run is active,
returns `409`.

Abort is cooperative between steps. There is no verified hardware cancellation
command, so an in-flight operation cannot be cancelled by the API. An HTTP
timeout or client disconnect never cancels, retries, or replays motion.

Run the service on loopback with a single worker; several workers would compete
for one USB handle:

```powershell
uv run python -m multiflo.service --expected-serial 14071419 --host 127.0.0.1 --port 8000
uv run python -m multiflo.tools.export_openapi --output openapi.json
```

An active-run crash marker is written before motion and updated after each
confirmed step. If it survives a process interruption, new runs are blocked
until read-only Ready-state reconciliation or explicit physical operator
reconciliation. See `phase_4.md` and `multiflo.tools.hardware_reconcile`.

The one-shot motion tool is intentionally guarded and must only be run after its
physical checklist and exact dispense settings have been confirmed:

```powershell
uv run python -m multiflo.tools.hardware_dispense --expected-serial 14071419 --cassette 5ul --volume-ul 100 --flow-rate medium --pre-dispense-volume-ul 10 --pre-dispense-cycles 2 --authorization CASSETTE_PLATE_TUBING_IDLE_CONFIRMED
```

Phase 3 one-step hardware checks use `multiflo.tools.hardware_phase3_step` and
an operation-specific authorization token. Never retry a run in
`unknown_execution_state`; reconcile the instrument state first.

The Phase 5 end-to-end check drives a real loopback Uvicorn server over HTTP. It
is read-only by default; a motion step requires both `--motion` and the matching
authorization token:

```powershell
uv run python -m multiflo.tools.hardware_phase5_e2e --expected-serial 14071419 --motion none
uv run python -m multiflo.tools.hardware_phase5_e2e --expected-serial 14071419 --motion shake --duration-seconds 5 --authorization PHASE5_E2E_SETUP_CONFIRMED
```
