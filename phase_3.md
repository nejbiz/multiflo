# Phase 3 - Installed-operation expansion and motion lifecycle

Date: 2026-08-26 (Europe/Zurich)

## Outcome

Phase 3 expands the pure-Python driver around the capability of the connected
base MultiFlo: one calibrated primary peristaltic pump. The supported protocol
steps are now:

- primary peristaltic dispense;
- primary peristaltic prime and purge;
- medium-speed X-axis shake; and
- timed soak.

Dispense support covers 96-well, 96-deep-well, and 384-well plates; low,
medium, and high flow; cassette requirements; pre-dispense settings; column maps
for every supported geometry; both 384-well row sections; and signed X/Y
fine-position offsets. All operations use the recovered batch lifecycle and are available
through the existing typed FastAPI protocol boundary.

This phase also corrects the meaning of a successful step-command response. A
zero device status means the step was accepted, not that mechanical execution
finished. Completion is now based on the device-side Program Step Status query.

## Evidence and pure-Python boundary

The controlled `.LHC` files under `protocols/` were treated as one evidence set.
They cover plate geometry, flow, volume, cassette requirements, pre-dispense,
column and row selection, X/Y offsets, prime, purge, shake, soak, and related
protocol features. The development tools decrypt the files and invoke vendor
encoding code only for offline differential analysis.

The resulting layouts, constants, and safety limits are implemented in project
Python and fixed in golden tests. No BioTek executable, managed assembly, LHC
process, CLR host, or `.LHC` parser is used by the runtime driver or API.

## Supported operation surface

| Model | Command | Implemented surface |
| --- | ---: | --- |
| `PeristalticDispense` | `0x008F` | Primary pump; three plate types; low/medium/high flow; cassette requirements; paired pre-dispense; partial column maps; 384-well row sections; X/Y offsets |
| `PeristalticPrime` | `0x0090` | Primary pump, volume mode, plate selector, low/medium/high flow, cassette requirement |
| `PeristalticPurge` | `0x0091` | Primary pump, volume mode, plate selector, low/medium/high flow, cassette requirement |
| `Shake` | `0x00A3` | Medium speed (5 Hz), X axis, 1-60 seconds, optional carrier-home move |
| `Soak` | `0x00A3` | Timed soak, 1-60 seconds, optional carrier-home move |

The protocol model is a strict discriminated union of these steps. Unknown
fields are rejected. The runner can execute them in an ordered protocol, and
the API exposes validation, start, polling, and cooperative abort without a raw
command escape hatch.

## Dispense encoding and validation

The peristaltic dispense body is 24 bytes including the plate selector:

| Offset | Size | Meaning |
| ---: | ---: | --- |
| 0 | 1 | Plate type: 384-well `1`, 96-well `4`, 96-deep-well `5` |
| 1 | 2 | Volume in uL, unsigned little-endian |
| 3 | 1 | Flow: low `0`, medium `1`, high `2` |
| 4 | 1 | Required cassette: any `0`, 1 uL `1`, 5 uL `2`, 10 uL `3` |
| 5 | 1 | Signed X offset in instrument steps |
| 6 | 1 | Signed Y offset in instrument steps |
| 7 | 2 | Plate-derived dispense height |
| 9 | 2 | Pre-dispense volume in uL |
| 11 | 1 | Pre-dispense cycles |
| 12 | 6 | Packed 48-position column map |
| 18 | 1 | Inverted row-section skip mask |
| 19 | 1 | Primary peristaltic pump `1` |
| 20 | 4 | Reserved, zero in all fixtures |

### Cassette behavior

The request can require a 1, 5, or 10 uL cassette, or use `any`. Immediately
before motion the driver reads the instrument's primary cassette setting. A
specific requirement must match it. `any` still requires the instrument to
report a known physical cassette, and dispense/pre-dispense volumes are then
validated against that installed cassette.

| Cassette | Dispense range | Increment |
| --- | ---: | ---: |
| 1 uL | 1-50 uL | 1 uL |
| 5 uL | 5-2500 uL | 5 uL |
| 10 uL | 10-3000 uL | 10 uL |

The 750 uL dispense fixture requiring a 1 uL cassette is retained as negative
evidence and is rejected because it exceeds the manual's 50 uL limit. Cassette
metadata is only checked; the driver never changes the instrument's onboard
cassette setting.

### Plate geometry and position maps

The recovered dispense heights are 333 steps for 384-well, 336 for standard
96-well, and 929 for the supported 96-deep-well geometry. These are selected by
plate type. A public custom Z/height override is not exposed.

All-column maps are supported for every plate type, and partial column
selection is supported for all three geometries. See the calib30-calib32
section below for the completed 384-well row and column encoding.

### Fine positioning

Every peristaltic dispense accepts cassette-independent signed offsets:

- `x_offset_steps`: -60 (left) through 60 (right);
- `y_offset_steps`: -40 (back) through 40 (forward).

Both default to zero. The bounds and directions come from the operator manual,
not merely the wider signed-byte wire representation. The controlled encodings
include `X=+19`, `X=-19`, `X=-19/Y=+6`, and `X=-19/Y=-6`. These options are for
fine tuning and were intentionally not exercised on hardware in this phase.

## Prime, purge, shake, and soak formats

Prime and purge use distinct command IDs but share an 11-byte volume-mode body:
plate, volume, zero duration, flow, the fixture-proven volume-mode flag,
cassette requirement, primary pump, and two reserved zero bytes. Duration mode
is intentionally unsupported.

Shake and soak share a 12-byte body. The fixtures prove medium speed code `3`,
X-axis code `0`, separate shake and soak duration fields, and the carrier-home
flag. Only the fixture-proven medium speed and X axis are encoded.

Representative golden bodies are:

```text
prime 3000 uL, medium, 96-well:
04 B8 0B 00 00 01 01 00 01 00 00

purge 2000 uL, medium, 96-well:
04 D0 07 00 00 01 01 00 01 00 00

shake 5 s, medium X, move home, 96-well:
04 01 05 00 03 00 00 00 00 00 00 00

soak 30 s, move home, 96-well:
04 01 00 00 03 00 1E 00 00 00 00 00
```

## Authoritative motion lifecycle

Every motion step now follows the same recovered sequence:

1. Send Start Batch `0x008D` with the plate selector.
2. Send exactly one typed operation command.
3. Poll Program Step Status `0x0092` every 500 ms.
4. Continue while the state is Busy or Paused.
5. Return only when the device reports Ready.
6. Raise a device error on Error or Stopped.
7. Send End Batch `0x008C` after successful completion.

The seven-byte status data contains a two-byte state, four-byte error code, and
one-byte error source. Recovered states are Ready `1`, Busy `2`, Paused `3`,
Error `4`, and Stopped `5`.

Any transport or protocol failure after the batch sequence begins becomes
`unknown_execution_state`; the driver clears its motion authorization and never
retries automatically. A command ACK or zero response status alone is never
reported as mechanical completion.

A guarded stale-Busy recovery path is included for a physically stationary
device left Busy by an interrupted or incomplete lifecycle. The verified path:

- requires an explicit stationary/clear-area/LHC-closed confirmation;
- first requires the device to report Busy;
- sends End Batch exactly once;
- verifies the resulting state is Ready; and
- treats an uncertain post-send result as non-retriable.

## Motion preflight and hardware guard

Each run requires literal operator setup/idle confirmation. The fresh preflight
then performs a communication test, requires Program Step Status to be Ready,
checks the expected product serial `14071419`, inventories installed modules,
requires the primary pump where applicable, and verifies the onboard cassette
setting.

The one-step Phase 3 hardware tool runs through the FastAPI boundary and demands
an operation-specific authorization token. Dispense options include plate,
cassette, volume, flow, pre-dispense, columns, row section, and X/Y offsets. It
does not retry uncertain motion.

LHC must be closed before opening the D2XX device. Physical setup confirmation
remains operation-specific: correct plate and cassette, tubing and liquid or
waste positioning, closed pump cover, clear carrier path, and accessible
emergency stop.

## Automated verification

The final Phase 3 suite contains 58 passing tests on Python 3.13.15:

```powershell
uv run python -m unittest discover -s tests -v
```

Coverage includes exact command bodies, plate selectors, cassette rules,
pre-dispense variations, partial 96-well maps, 384-well odd-row selection,
signed X/Y offsets, model bounds, the guarded CLI model path, Program Step
Status decoding, Start Batch/status/End Batch ordering, fake-transport failure
states, and a mixed prime/purge/shake/soak protocol through FastAPI.

The emitted Starlette `httpx` TestClient deprecation warning does not affect the
test result.

## Guarded hardware evidence

All live runs used the exact FTDI/product serial `14071419`, explicit physical
confirmation, and closed LHC.

| Operation | Hardware result |
| --- | --- |
| Stale-batch recovery | One guarded End Batch changed stationary Busy to Ready; no retry was needed |
| Shake | One 5-second medium X-axis shake completed through the full lifecycle; the operator observed approximately five seconds of shaking |
| Soak | One 30-second soak completed through Start Batch, status polling, and End Batch without a reported error |
| Prime | One 100 uL medium-flow prime using the calibrated 5 uL primary cassette completed through the full lifecycle |
| 384-well odd-row dispense | Two 10 uL high-flow runs using the calibrated 1 uL cassette completed with status zero and no indications; after the first run the operator confirmed liquid only in A, C, E, G, I, K, M, and O across all columns |

The first and repeated 384-well run IDs were
`cb9d3fe4-dfad-4b0d-8523-0faa87c1a967` and
`b2597b46-e7dc-4b51-a87c-3825166cf3a4`. The repeat deliberately added another
10 uL to the same odd-row wells.

These checks verify command selection, lifecycle completion, and the observed
384-well row pattern. They are not volumetric accuracy or calibration studies.
The manufacturer certificate remains the calibration evidence for the physical
cassette.

## Boundaries carried forward

- Purge has exact fixture, codec, model, driver, fake-transport, and API
  coverage, but was not run on hardware.
- X/Y offsets have exact offline fixture coverage and manual bounds, but were
  intentionally not run on hardware.
- Protocol loops are represented by explicit repeated steps; no loop construct
  is exposed.
- Custom dispense height/Z is not exposed; heights are selected by plate type.
- Pause is decoded as a device status but no pause/resume command or API is
  claimed.
- Abort remains cooperative between steps. There is no verified command to
  cancel physical motion already in flight.
- A client disconnect does not cancel or replay an operation. Ambiguous
  execution requires operator reconciliation before another run.

Phase 3 is complete for the implemented primary-peristaltic configuration and
its verified advanced dispense surface. It does not claim hardware evidence for
purge. Those boundaries remain explicit inputs to Phase 4 rather than inferred
functionality.

## Completed 384-well row and column selection (calib30-calib32)

The three final controlled fixtures close the last open item in the Phase 3
scope. Each changes exactly one aspect of the calib29 dispense, so the vendor
encoder output isolates the field under test.

| Fixture | Change from calib29 | Definition map fields | Encoded bytes 12-18 |
| --- | --- | --- | --- |
| calib30 | Other row section | columns `1...1`, rows `0111` | `FF FF FF FF FF FF 01` |
| calib31 | Odd columns only | columns `1010...` + `1...1`, rows `1111` | `55 55 55 FF FF FF 00` |
| calib32 | Even columns only | columns `0101...` + `1...1`, rows `1111` | `AA AA AA FF FF FF 00` |

### 384-well row sections

Byte 18 is an inverted skip mask over the two cassette passes across a
384-well plate. Bit 0 selects the first section and bit 1 selects the second.
calib25's `1011` map clears bit 1 and encodes `0x02`; the guarded hardware run
confirmed liquid only in rows A, C, E, G, I, K, M, and O, so the first section
is the odd rows. calib30's `0111` map clears bit 0 and encodes `0x01`, which is
therefore the even rows B, D, F, H, J, L, N, and P.

`row_sections` is now `all` (mask `0x00`, the default), `odd` (`0x02`), or
`even` (`0x01`), and it is still rejected for 96-well geometries. The two
remaining high bits of the field are set in every fixture and are never cleared
by the encoder.

### Partial 384-well column maps

Bytes 12-17 are the same 48-bit positional map used by the 96-well geometries:
bit `column - 1`, least significant bit first within each byte. calib31 selects
columns 1, 3, ... 23 and encodes `55 55 55`; calib32 selects columns 2, 4, ... 24
and encodes `AA AA AA`. Both leave the 24 unused trailing positions set, which
matches the deep-well behavior already proven by calib3 and differs from the
standard 96-well map, where LHC clears them.

The encoder now derives both the column count and the trailing-position
convention from the plate type:

| Plate | Columns | Unused trailing positions |
| --- | ---: | --- |
| 384-well | 24 | set |
| 96-deep-well | 12 | set |
| 96-well | 12 | cleared |

`columns` therefore accepts any unique subset of 1-24 for a 384-well plate and
1-12 for the two 96-well geometries. The fixtures directly cover all-column,
odd-column, and even-column 384-well maps; arbitrary subsets follow from the
same positional rule, which calib3 already proved for a single deep-well column.

### Verification

`encode_peristaltic_dispense` reproduces the vendor encoder output byte for byte
for calib30, calib31, and calib32, and the three bodies are fixed as golden
tests. The suite is now 82 passing tests on Python 3.13.15:

```powershell
uv run python -m unittest discover -s tests -v
```

The guarded one-step hardware tool accepts `--row-sections even` alongside `odd`
and `all`. These fixtures were integrated offline; the even-row section and the
partial 384-well column maps were not run on hardware, so the odd-row 384-well
dispense remains the only hardware-observed 384-well pattern.

With calib30-calib32 integrated, every fixture under `protocols/` that falls in
the Phase 3 operation scope is encoded, validated, and covered by a golden test.
