# MultiFlo

Pure Python driver work for the base BioTek MultiFlo connected through FTDI D2XX.

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

Read `phase_0.md`, `phase_1.md`, and `phase_2.md` before hardware work. The
Phase 0 and Phase 1 procedures do not authorize motion; Phase 2 documents the
only hardware-verified motion slice.

Phase 2 implements only full-plate primary peristaltic dispensing to the proven
96-deep-well geometry. Its API endpoints are:

- `POST /v1/protocols/validate`
- `POST /v1/runs`
- `GET /v1/runs/{run_id}`
- `POST /v1/runs/{run_id}/abort`

Abort is cooperative between steps. There is no verified hardware cancellation
command, so an in-flight dispense cannot be cancelled by the Phase 2 API.

The one-shot motion tool is intentionally guarded and must only be run after its
physical checklist and exact dispense settings have been confirmed:

```powershell
uv run python -m multiflo.tools.hardware_dispense --expected-serial 14071419 --cassette 5ul --volume-ul 100 --flow-rate medium --pre-dispense-volume-ul 10 --pre-dispense-cycles 2 --authorization CASSETTE_PLATE_TUBING_IDLE_CONFIRMED
```
