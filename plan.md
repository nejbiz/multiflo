# Base MultiFlo Driver Plan

## Goal

Build a small, pure Python driver for the **base BioTek MultiFlo**, communicate over its USB-B/FTDI connection, and expose the completed driver through FastAPI.

The milestone is complete when an API client can:

- inspect the connected instrument;
- define and validate a protocol;
- run every supported base MultiFlo step;
- poll run state and step progress;
- abort a run safely;
- pause or resume only if those behaviors are verified on the instrument; and
- receive clear validation, device, transport, and ambiguous-execution errors.

Further clients, user interfaces, deployment integrations, and workflow integrations are outside this plan.

## Scope

In scope:

- Pure Python implementation of the MultiFlo protocol.
- USB-B transport through FTDI D2XX.
- Packet framing, checksum, command IDs, payload encoding, and response decoding.
- Instrument identity, installed-module, busy/idle, status, and error queries needed for safe execution.
- Typed models and validation for all base MultiFlo operations.
- Sequential protocol execution, progress, abort, and verified pause/resume.
- A minimal FastAPI/OpenAPI surface.
- Unit, golden-packet, fake-transport, API, and guarded hardware tests.

Out of scope:

- RS-232 or virtual COM ports.
- MultiFlo FX behavior.
- `pythonnet`, CLR hosting, COM automation, or wrapping BioTek managed assemblies.
- Loading or launching LHC at runtime.
- Firmware/basecode modification.
- Arbitrary raw-command API endpoints.
- A production `.LHC` import feature.
- Database-backed run history, event sourcing, SSE/WebSockets, multi-process scaling, authentication, TLS, or remote deployment policy.
- UI and later integrations.

## Pure Python boundary

All MultiFlo-specific behavior must be Python source owned by this project:

- framing and checksums;
- command and payload encoding;
- response and indication decoding;
- parameter validation;
- protocol execution and state;
- error handling; and
- FastAPI exposure.

The following BioTek files are reverse-engineering evidence only and must never be runtime dependencies:

- `Liquid Handling Control.exe`
- `BTILHCRunner.dll`
- `BTI406Interface.dll`
- `BTIMultiFloInterface.dll`
- `FTD2XX_NET.dll`

The operating system still needs an FTDI USB driver. Python may call the official native D2XX library through a maintained Python binding or a small reviewed `ctypes` adapter. That adapter may only enumerate, open, configure, purge, read, write, and close FTDI devices. It must contain no MultiFlo semantics.

Removing the BioTek application and managed assemblies must not break the installed driver or its tests once the permitted FTDI transport is available.

## Local evidence

- Sample protocol: `protocols/calib1.LHC`
- Operator manual: `manuals/BioTek+MultiFlo_Operator's+Manual.pdf`
- Main application:
  `C:\Users\nej\OneDrive\Documents\LHC-everything\LHC Installation\program files\BioTek\Liquid Handling Control 2.22\Liquid Handling Control.exe`
- LHC runner assembly:
  `C:\Users\nej\OneDrive\Documents\LHC-everything\LHC Installation\program files\BioTek\Liquid Handling Control 2.22\BTILHCRunner.dll`
- Base MultiFlo implementation:
  `C:\Users\nej\OneDrive\Documents\LHC-everything\LHC Installation\program files\BioTek\Liquid Handling Control 2.22\MultiFlo\BTI406Interface.dll`
- Small MultiFlo adapter:
  `C:\Users\nej\OneDrive\Documents\LHC-everything\LHC Installation\program files\BioTek\Liquid Handling Control 2.22\MultiFlo\BTIMultiFloInterface.dll`
- Managed FTDI reference and API XML:
  `C:\Users\nej\OneDrive\Documents\LHC-everything\LHC Installation\program files\BioTek\Liquid Handling Control 2.22\MultiFlo\FTD2XX_NET.dll`
  `C:\Users\nej\OneDrive\Documents\LHC-everything\LHC Installation\program files\BioTek\Liquid Handling Control 2.22\MultiFlo\FTD2XX_NET.xml`
- Native D2XX library shipped with LHC:
  `C:\Users\nej\OneDrive\Documents\LHC-everything\LHC Installation\program files\BioTek\Liquid Handling Control 2.22\ftd2xx.dll`
- Operator help:
  `C:\Users\nej\OneDrive\Documents\LHC-everything\LHC Installation\program files\BioTek\Liquid Handling Control 2.22\MultiFlo\MultiFlo.chm`

Use the DLLs only for offline analysis. Save conclusions as documentation and fixed test fixtures so normal tests never need vendor code.

## Known protocol facts

### Transport

LHC uses FTDI D2XX directly, not a virtual COM port. It finds the device by FTDI description and serial number and configures:

- 38400 baud;
- 8 data bits;
- no parity;
- 2 stop bits;
- no flow control;
- DTR and RTS; and
- explicit read/write timeouts.

Before implementation is considered stable, verify the target instrument's VID, PID, description, serial number, latency behavior, DTR/RTS values, purge behavior, and timeouts.

### Frame

Requests and responses use an 11-byte little-endian header:

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 1 | Class: request `1`, response `2`, indication `3` |
| 1 | 1 | Destination: instrument `2` |
| 2 | 2 | Command ID |
| 4 | 1 | Source: PC `1` |
| 5 | 2 | Message ID |
| 7 | 2 | Body length |
| 9 | 2 | Checksum |

Checksum:

```text
checksum = (-sum(header bytes 0..8 and all body bytes)) & 0xffff
```

The checksum is written little-endian. It detects corruption; it is not encryption or authentication.

The bodyless communication-test command is `0x0073`. With message ID zero:

```text
01 02 73 00 01 00 00 00 00 89 FF
```

This is the first command to test on hardware. Never use a motion command as a connection test.

### Known operation IDs

| Command | Operation |
| ---: | --- |
| `0x008F` | Peristaltic dispense |
| `0x0090` | Peristaltic prime |
| `0x0091` | Peristaltic purge |
| `0x00A3` | Shake/soak |

These are fixed IDs selected from the operation type. They are not calculated from volumes or visible LHC definition prefixes.

### Existing golden packet

`calib1.LHC` describes a 100 uL medium-speed primary peristaltic dispense to a 96-deep-well plate, with every position selected and a 10 uL/two-cycle pre-dispense.

The 23-byte step payload is:

```text
64 00 01 00 00 00 A1 03 0A 00 02 FF FF FF FF FF FF 00 01 00 00 00 00
```

Known fields:

| Bytes | Meaning |
| ---: | --- |
| 0-1 | Volume, unsigned 16-bit little-endian |
| 2 | Flow rate; Low `0`, Medium `1` |
| 3 | Required cassette; `0` means any |
| 4-5 | Signed horizontal offsets |
| 6-7 | Dispense height |
| 8-9 | Pre-dispense volume |
| 10 | Pre-dispense cycles |
| 11-16 | Packed 48-position map |
| 17 | Inverted row-skip mask |
| 18 | Pump: primary `1` |
| 19-22 | Unknown/reserved; zero in this fixture |

Plate type `5` is prepended. The complete request is:

```text
01 02 8F 00 01 00 00 18 00 40 F8
05 64 00 01 00 00 00 A1 03 0A 00 02 FF FF FF FF FF FF 00 01 00 00 00 00
```

Keep this only as a golden test unless a hardware test explicitly authorizes motion.

## Simple design

Use a small package. Start with these modules and split only when a file becomes genuinely difficult to understand:

```text
multiflo/
  models.py       Protocol and operation models plus validation
  codec.py        Frame, checksum, command bodies, response parsing
  transport.py    FTDI D2XX byte transport and fake transport
  driver.py       Device ownership, commands, run state, abort
  api.py          FastAPI schemas and routes
  errors.py       Small stable error set
```

Development-only `.LHC` decryption/differential scripts belong under `tools/`, not in the production driver or API.

Keep the boundaries simple:

- `transport.py` moves bytes and knows nothing about MultiFlo commands.
- `codec.py` converts typed commands and frames to/from bytes.
- `driver.py` is the single authoritative controller and protocol runner.
- `api.py` validates HTTP input and calls `driver.py`; it never touches D2XX or raw packets.
- Use the same Pydantic protocol models internally and at the API boundary unless a real mismatch appears. Do not create duplicate model layers pre-emptively.

## Operating rules

- One process owns the USB handle.
- One worker thread performs blocking D2XX reads and writes.
- One request is in flight at a time.
- One protocol run may be active.
- FastAPI handlers never block the event loop on USB I/O.
- An HTTP timeout or client disconnect never cancels or retries physical motion.
- A timeout or disconnect after sending motion becomes `unknown_execution_state`.
- Never retry a command with an uncertain outcome.
- Start with a single Uvicorn worker. Multiple workers must not compete for one instrument.
- Use polling for run progress. Do not add streaming infrastructure unless polling proves inadequate.

Use a small error set:

- `ValidationError`
- `TransportError`
- `ProtocolError`
- `DeviceError`
- `BusyError`
- `UnknownExecutionState`

Add another error class only when callers need to handle it differently.

## Development phases

### Phase 0 - Evidence and safe setup

- Record the exact instrument, serial number, installed modules, firmware/basecode, and calibration status.
- Select the Python version, host architecture, native D2XX distribution, and minimal Python binding/adapter.
- Record FTDI VID/PID, description, and serial number.
- Define a safe dry test setup and allowed parameter ranges.
- Identify known read-only/non-motion commands.
- Store the current checksum and dispense fixtures as test data.

Done when the environment is reproducible and there is a written hardware test procedure.

### Phase 1 - USB, framing, and read-only communication

- Implement the frame model, little-endian fields, checksum, and response parser.
- Implement the narrow D2XX transport: enumerate, select, open, configure, purge, read, write, close.
- Add a scripted fake transport; do not build a full instrument simulator.
- Test success, fragmented reads, malformed length, bad checksum, timeout, and disconnect.
- Send only `0x0073` on initial hardware tests.
- Add the minimum identity, firmware, installed-module, busy/idle, and status queries required before motion.

Done when the intended instrument is selected reliably and repeated read-only communication succeeds.

### Phase 2 - First end-to-end vertical slice

Implement peristaltic dispense completely before broadening the design:

- typed request model and validation;
- `0x008F` payload encoding;
- golden test using `calib1`;
- one safe hardware verification;
- minimal protocol model containing one or more dispense steps;
- sequential execution and basic run state;
- minimal FastAPI endpoints for validation, starting a run, polling it, and aborting it.

Do not generalize beyond what this slice proves. Refactor only after the complete path works from JSON request to verified hardware response.

Done when a validated one-step dispense can run through FastAPI and its result can be polled without exposing raw commands.

### Phase 3 - Add the remaining operations one at a time

For each operation, repeat the same small loop:

1. Generate controlled LHC fixtures that change one parameter at a time.
2. Recover the body format through offline DLL analysis and differential comparison.
3. Add the typed model and only its necessary validation.
4. Add golden encoding/decoding tests.
5. Verify the lowest-risk valid example on hardware.
6. Record remaining unknowns explicitly.

Operations - this is the final scope, do not deviate:

- peristaltic prime, dispense, shake, pause and purge;
- plate types and position maps; and
- advanced options actually exposed for those operations.

Do not create one universal payload abstraction. Separate encoders are clearer when operation formats differ.

Done when every operation has a tested model, encoder, validator, response behavior, and hardware evidence.

### Phase 4 - Finish protocol execution and failure behavior

- Support ordered mixed-operation protocols.
- Validate installed modules, plate compatibility, units, ranges, and position maps before motion.
- Keep a small state machine: `disconnected`, `idle`, `running`, `paused` where supported, `aborting`, `completed`, `failed`, and `unknown_execution_state`.
- Verify abort before relying on it from the API.
- Add pause/resume only if instrument behavior is understood and tested.
- Write ordinary structured logs with run ID, step index, request, response/status, and timestamps.
- Keep one small crash marker containing the active run and last confirmed step. On restart, block motion until read-only state reconciliation or operator acknowledgement. Do not build an event store.

Done when mixed protocols run sequentially and timeout, disconnect, abort, and restart behavior cannot silently duplicate motion.

### Phase 5 - Finalize the FastAPI boundary

Keep the API small:

- `GET /v1/health` - process health; no hardware command.
- `GET /v1/device` - verified identity, modules, connection, and state.
- `POST /v1/protocols/validate`
- `POST /v1/runs` - accepts a protocol and client-supplied request ID; returns a run ID quickly.
- `GET /v1/runs/{run_id}` - state, current step, errors, and final result.
- `POST /v1/runs/{run_id}/abort`
- Pause/resume endpoints only if Phase 4 verified them.

Requirements:

- Strict Pydantic schemas and generated OpenAPI.
- No raw command, packet, firmware, or unvalidated maintenance endpoints.
- A repeated request ID must not start a duplicate run during the service lifetime.
- Starting while another run is active returns a conflict.
- Validation errors do not open or command the instrument.
- HTTP disconnects do not alter execution.
- Bind to loopback during development and run one API worker.

Done when API contract tests pass with the fake transport and a guarded end-to-end test passes against the real instrument.

## Test strategy

Keep four test groups:

1. **Unit and golden tests:** checksum, framing, known payloads, parsing, models, and validation.
2. **Fake-transport tests:** fragmented responses, device errors, timeouts, disconnects, abort, and ambiguous state.
3. **Hardware tests:** opt-in, guarded by expected serial number, with read-only and motion tests separated.
4. **FastAPI tests:** schemas, validation, conflicts, request-ID deduplication, polling, abort, and one complete fake run.

Every hardware bug should become a small unit or fake-transport regression when possible.

## Definition of done

- The driver is pure Python except for the permitted low-level FTDI/OS transport dependency.
- No `pythonnet`, CLR, BioTek managed assembly, LHC process, COM, or GUI dependency exists.
- The driver connects only to the intended base MultiFlo over USB-B/D2XX.
- Identity and installed capabilities are checked before motion.
- Every supported base MultiFlo operation is encoded, validated, and hardware-verified.
- Mixed protocols execute sequentially with pollable step progress.
- Abort works; pause/resume exists only if verified.
- Timeout, disconnect, and crash cannot cause automatic replay or duplicate motion.
- FastAPI exposes the minimal documented API and no raw hardware escape hatch.
- Unit, fake, hardware, and API tests pass.
- Protocol facts, unknowns, safe operating procedure, and recovery procedure are documented.

## Rules for future work

- Prefer the smallest working vertical slice over a framework designed for hypothetical integrations.
- Add abstractions only after duplication or a concrete second implementation makes them useful.
- Keep FastAPI thin and keep USB ownership in `driver.py`.
- Keep `.LHC` parsing as development tooling unless product compatibility is separately requested.
- Do not add a database, broker, event store, plugin system, streaming layer, or multi-worker design without a demonstrated need.
- Stay on the base MultiFlo; ignore FX-only material unless it proves a shared primitive.
- Separate observed facts from inferences and record evidence for each recovered field.
- Never solve a gap by wrapping a BioTek assembly. Reimplement understood behavior in Python.
- Never perform a hardware-changing action without explicit authorization and a safe setup.
- Preserve unrelated user files and changes.
