# MultiFlo plan

This is the single source of truth for the base MultiFlo driver as of
2026-09-25. It records the current implementation and remaining work. Follow
the linked code, tests, and evidence files for exact schemas and bytes.

## Goal and status

Provide a small, pure Python driver for one base BioTek MultiFlo and a validated
HTTP interface. The current driver supports primary peristaltic **dispense,
prime, purge**, plus **shake and soak**. A single worker executes ordered
protocol steps. The HTTP API offers individual operation routes and retains
multi-step protocol submission for existing clients. The 141 offline tests pass;
no hardware test was run during this repository cleanup.

The runtime does not load BioTek executables, managed assemblies, or `.LHC`
files. Vendor material in [protocols](protocols) and [manuals](manuals) is
development evidence. No FX, syringe, washer, firmware, raw-command, UI,
database, or multi-device implementation is in scope.

## Repository map

| Path | Responsibility |
| --- | --- |
| [models.py](src/multiflo/models.py) | Strict Pydantic operation and protocol models; plate, cassette, volume, geometry, and time limits |
| [codec.py](src/multiflo/codec.py) | 11-byte frames, checksum, response decoding, and fixture-derived operation bodies |
| [transport.py](src/multiflo/transport.py) | Byte-only D2XX, serial, and offline null transports |
| [driver.py](src/multiflo/driver.py) | One in-flight exchange, read-only inventory, preflight, batch lifecycle, and failure classification |
| [runner.py](src/multiflo/runner.py) | One worker, ordered steps, request-ID replay protection, abort, status, and crash marker |
| [api.py](src/multiflo/api.py), [service.py](src/multiflo/service.py) | Strict HTTP routes and one-worker loopback server |
| [tools](src/multiflo/tools) | Guarded hardware tools, RevPi entry point, OpenAPI export, and offline analysis helpers |
| [tests](tests) | Golden packets, simulated byte I/O, execution, transport, and API contracts |

The package uses a `src` layout; tests and evidence are outside the wheel.
`[project]` metadata and dependencies live in [pyproject.toml](pyproject.toml),
with a checked-in [uv.lock](uv.lock). Windows D2XX needs FTDI's native library;
serial transport uses the optional `serial` extra (`pyserial`). The RevPi
prototype runs on Python 3.11 by copying the package modules, since this
package currently declares Python 3.13 or newer for installation. The guarded
HTTP hardware tools use the optional `hardware-tools` extra (`httpx`), which
is also installed by the development dependency group.

## Contract and boundaries

- The driver selects the expected FTDI serial and `MultiFlo` description on
  D2XX. Before any motion it also verifies the instrument's in-band product
  serial, installed pump and cassette, and device-side `Ready` status. Serial
  transport uses a configured port and relies on that in-band serial check.
- Every protocol step must use one plate type. Supported plate types are
  `96_well`, `96_deep_well`, `384_well`, and `384_deep_well`; the default is
  `96_well`. The 384 deep-well type uses the standard 384 wire geometry with
  a different default dispense height; this behavior has offline tests but no
  direct hardware verification.
- Dispense volume limits are **1–1200 uL** for `1ul`, **5–2500 uL** in 5 uL
  increments for `5ul`, and **10–3000 uL** in 10 uL increments for `10ul`.
  Both 384 plate types require the detected `1ul` cassette. `any` still checks
  the installed cassette before motion. Prime and purge have a typed 1–3000 uL
  volume field; the driver does not change the instrument's cassette setting.
- Dispense defaults to full columns and rows. It supports partial column maps,
  384 odd/even row sections, X offsets from -60 to 60 steps, and Y offsets from
  -40 to 40. Default dispense heights are 333 (`384_well`), 553
  (`384_deep_well`), 336 (`96_well`), and 1020 (`96_deep_well`) steps above the
  carrier. A step may override height within 100–1100; lower means closer.
- Shake and soak accept 1–600 seconds. Shake encodes the fixture-proven medium
  speed and X axis only. All input models reject unknown fields.

The API routes are `GET /v1/health`, `GET /v1/device`,
`POST /v1/operations/{dispense,prime,purge,shake,soak}`,
`POST /v1/protocols/validate`, `POST /v1/runs`, `GET /v1/runs/{run_id}`,
and `POST /v1/runs/{run_id}/abort`. See [api.py](src/multiflo/api.py) or the
generated OpenAPI schema for request and response fields. Starts require a
client `request_id` and `operator_confirmed_idle: true`. An identical request
ID returns the original run during that process lifetime; a changed request or
another active run returns `409`. The operation routes construct one-step
protocols internally. The protocol routes remain available for multi-step
clients.

One process owns the USB handle and one worker performs blocking I/O. A step
uses `Start Batch → operation → Program Step Status until Ready → End Batch`.
The response to the operation means accepted, not mechanically complete.
Abort is cooperative between steps; no verified device cancellation or
pause/resume command is exposed. HTTP disconnects never stop or replay motion.
Read-only exchanges have bounded retries; motion is never retried after an
uncertain send. A retained [crash marker](src/multiflo/runner.py) blocks new
runs until read-only Ready reconciliation or explicit physical reconciliation.
Request IDs and completed run history are process-local; only the active
marker persists.

## Evidence and confidence

The instrument used for guarded checks had product and FTDI serial
`14071419`, basecode `7210200` / `1.12`, a primary peristaltic pump, and no
secondary pump. The D2XX path used 38400 baud, 8-N-2, no flow control, DTR/RTS,
and exact serial/description selection. On a RevPi, the same driver completed
a 10 uL 96-well dispense followed by a five-second shake over `/dev/ttyUSB0`.

Controlled `.LHC` fixtures in [protocols](protocols) established packet fields;
[golden tests](tests/test_codec.py) pin the resulting bytes. Hardware checks
established repeated read-only inventory, a five-second shake, a 30-second
soak, a 100 uL prime, and two 10 uL 384-well odd-row dispenses. The operator
observed liquid only in the selected odd rows. The earlier 96-deep-well
dispense showed command acceptance, but its original report preceded the
recovered completion lifecycle. The later lifecycle checks and RevPi run are
the stronger completion evidence. These checks are not calibration or volume
accuracy measurements. Purge, X/Y offsets, and 384 deep-well dispensing have
offline coverage but no direct hardware motion evidence.

The observed device response starts with an ACK byte and returns message ID
zero, even when a request used another ID. The driver therefore permits only
one in-flight request and matches the command ID. Frame/profile behavior and
fault cases are pinned by [transport](tests/test_transport.py),
[driver](tests/test_driver.py), and [retry](tests/test_retries.py) tests.

## Hardware procedure

1. Close Liquid Handling Control normally; confirm no other process owns the
   instrument. Confirm the exact serial and intended cassette/plate.
2. Check tubing, liquid or waste, cover, carrier path, and emergency stop.
   Verify the physical setup for the specific operation.
3. Run the offline suite, then use
   [hardware_smoke.py](src/multiflo/tools/hardware_smoke.py) and
   [hardware_inventory.py](src/multiflo/tools/hardware_inventory.py) for
   read-only checks. Use the guarded
   [single-step tool](src/multiflo/tools/hardware_phase3_step.py) only with its
   operation-specific authorization token. `--dry-run` prints encoded bytes
   without opening the instrument.
4. If a run reports `unknown_execution_state` or leaves a marker, inspect the
   instrument and use the local
   [reconciliation tool](src/multiflo/tools/hardware_reconcile.py). Do not
   resubmit the same motion. A stationary stale Busy batch has a separate
   guarded [End Batch recovery tool](src/multiflo/tools/hardware_end_batch_recovery.py).

The service binds to loopback with one worker by default. Non-loopback binding
is an explicit opt-in; deployment access control is outside this driver.
Keep the crash marker at a stable writable path on deployed systems.

## Verification and remaining work

Run `uv sync --locked` and
`uv run --locked python -m unittest discover -s tests -q`. Offline tests cover
models, golden packets, D2XX and serial behavior, read-only/motion errors,
whole-protocol preflight, run deduplication, abort, marker reconciliation,
and the API contract. Hardware checks are opt-in; ordinary tests do not move
the device.

- Keep direct hardware evidence for purge, offsets, and 384 deep-well behavior
  open until a guarded physical check is authorized and performed.
- The RevPi serial path is a working prototype. Its deployment currently needs
  a separately installed `pyserial`, a stable marker location, and an explicit
  service/package setup if it becomes permanent.
- Keep `/v1/runs` while multi-step clients use it. Removal requires a separate
  compatibility decision. Do not add raw hardware, firmware, or maintenance
  HTTP endpoints to simplify clients.
- Before publishing, review inclusion of vendor evidence files and confirm the
  private GitHub destination. Keep this file current as behavior changes.
