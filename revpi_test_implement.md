# RevPi implementation prototype - serial transport and hardware run

Date: 2026-08-26 (Europe/Zurich)

## Outcome

The pure-Python driver now runs on the Revolution Pi that is physically wired to
the MultiFlo, and a small dispense-plus-shake protocol completed on the real
instrument driven entirely from the RevPi. This is the first time the driver
executed on the deployment target rather than the Windows development laptop.

The single new capability is a raw-serial byte transport. The MultiFlo framing,
checksum, command encoding, driver, runner, and motion lifecycle are all
unchanged; only the bottom byte-moving layer is new, because on Linux the FTDI
FT232 is reached through the `ftdi_sio` kernel driver's `/dev/ttyUSB0` rather
than the Windows D2XX DLL.

## Topology

```text
Windows laptop (172.25.81.102 on Wi-Fi, internet)
  └─ Realtek USB GbE adapter "Ethernet 2"  192.168.50.1/24
       └─ point-to-point Ethernet
            └─ RevPi143564  eth0 192.168.50.2/24  (no internet; link-local only)
                 └─ USB  →  FTDI FT232 (0403:6001)  →  MultiFlo, product serial 14071419
```

The RevPi is not on Wi-Fi. It is reachable only over the dedicated USB-Ethernet
link at `192.168.50.2`, with SSH on 22 and the Apache reverse proxy on
80/443/41443/1880/41880. Access used the `pi` account over SSH.

## RevPi environment (observed)

| Property | Value |
| --- | --- |
| Model / OS | RevPi143564, Debian 12 (bookworm), `aarch64` |
| Kernel | 6.12.56-revpi1-rpi-v8 PREEMPT_RT |
| System Python | 3.11.2 (`/usr/bin/python3`) |
| Tooling present | `uv`, `git`, pyserial 3.5 |
| Internet | none (point-to-point link only) |
| FTDI device | `Bus 001 Device 004: ID 0403:6001 FT232 Serial (UART)` |
| Serial node | `/dev/ttyUSB0`, owner `root:dialout` |
| Kernel driver | `ftdi_sio` + `usbserial` loaded and bound to the FT232 |
| `pi` groups | includes `dialout` (serial access without sudo) |

Because `ftdi_sio` already owns the FT232 and exposes it as `/dev/ttyUSB0`, the
serial path needs no kernel changes. The FTDI D2XX path would instead require
FTDI's ARM64 `libftd2xx.so` plus detaching `ftdi_sio`; that was not necessary
for this prototype.

## Code changes

### Cross-platform D2XX loader (`transport.py`)

`_D2xxLibrary` previously refused any non-Windows host. It now selects the
loader and default library name by platform - `ftd2xx.dll` via `WinDLL` on
Windows, `libftd2xx.so` via `CDLL` on Linux, `libftd2xx.dylib` on macOS - so the
existing D2XX code can also load FTDI's Linux library when one is installed. The
`FT_*` C API is identical across platforms, so no call sites changed.

### `SerialByteTransport` (`transport.py`)

A new transport implements the same four-method `ByteTransport` protocol
(`open` / `close` / `read` / `write`) over a serial port using pyserial. It
configures the port to match the D2XX settings the driver expects:

- 38400 baud, 8 data bits, 2 stop bits, no parity;
- no RTS/CTS, DSR/DTR, or XON/XOFF flow control;
- DTR and RTS asserted; and
- input/output buffers reset on open.

Timeout parity with D2XX is deliberate: a zero-byte (timed-out) read raises
`TransportError` instead of returning an empty buffer, so the driver's existing
disconnect / uncertain-state handling behaves the same over serial as over
D2XX. The class holds no MultiFlo semantics; it only moves bytes. `SerialConfig`
carries the tunable baud and timeout values.

Device identity is still guarded where it matters: the driver's in-band product
serial check (`0x0100` must equal `14071419`) runs before any motion regardless
of transport, so selecting the wrong port cannot silently drive the wrong
instrument. What the serial path gives up relative to D2XX is FTDI-level
selection by descriptor serial and description; on this Pi there is exactly one
FT232 and one `/dev/ttyUSB0`, and the stronger protocol-level serial check
remains.

### `src/multiflo/tools/revpi_run.py`

A prototyping entry point that builds a `MultiFloDriver` over
`SerialByteTransport` and drives the `ProtocolRunner` directly, bypassing the
FastAPI/HTTP layer. The HTTP boundary was already verified end to end in Phase 5
on the laptop; bypassing it here means the Pi needs no web dependencies. Two
modes:

- `--check` - read-only communication test and full device inventory; sends no
  motion command;
- motion - builds a protocol from `--dispense` and/or `--shake` options and runs
  it. Motion requires `--authorization REVPI_DISPENSE_SHAKE_SETUP_CONFIRMED`,
  matching the guarded-tool convention from Phases 2, 3, and 5.

## Deployment (offline)

The Pi has no internet, so dependencies were transferred rather than installed
from PyPI. Only the pydantic stack was needed, because the runner and driver
depend on pydantic but not on FastAPI, and pyserial and typing-extensions are
already present system-wide.

1. On the laptop, aarch64 / CPython 3.11 wheels were fetched with
   `pip download --only-binary=:all: --platform manylinux2014_aarch64
   --python-version 3.11 --abi cp311` for `pydantic`, `pydantic-core`,
   `annotated-types`, `typing-extensions`, and `typing-inspection`.
2. The `multiflo` package modules (`__init__`, `errors`, `models`, `codec`,
   `transport`, `driver`, `runner`), an empty `multiflo/tools/__init__.py`,
   `tools/revpi_run.py` (now `src/multiflo/tools/revpi_run.py`), and the
   wheels were copied to `/home/pi/mf` over SFTP.
   The FastAPI-only modules (`api.py`, `service.py`, the D2XX/FastAPI hardware
   tools) were intentionally not shipped.
3. On the Pi: `uv venv --system-site-packages --python 3.11 .venv` (so the
   venv sees system pyserial), then
   `VIRTUAL_ENV=.venv uv pip install --no-index --find-links wheels pydantic`.

The package is run from a `PYTHONPATH=/home/pi/mf` layout rather than installed,
so no build step runs on the Pi.

## Hardware evidence

All runs used the FT232 at `/dev/ttyUSB0` on RevPi143564 and the in-band product
serial `14071419`. LHC was not involved; nothing else held the port.

### Read-only check

```powershell
.venv/bin/python -m multiflo.tools.revpi_run --check --expected-serial 14071419 --port /dev/ttyUSB0
```

Result: program-step state `ready`; product serial `14071419`; part `7210200`;
software `1.12`; primary peristaltic installed, no secondary; half-microliter
supported; onboard cassette `1ul`. This matches the Phase 5 laptop D2XX reading
exactly, confirming the serial transport is faithful.

### Dispense and shake (motion)

```powershell
.venv/bin/python -m multiflo.tools.revpi_run \
  --dispense --shake --plate-type 96_well --cassette 1ul \
  --volume-ul 10 --flow-rate medium --shake-seconds 5 \
  --expected-serial 14071419 --port /dev/ttyUSB0 \
  --authorization REVPI_DISPENSE_SHAKE_SETUP_CONFIRMED
```

A two-step protocol - a 10 uL medium-flow primary peristaltic dispense to a
96-well plate with no pre-dispense, then a 5-second medium X-axis shake with a
carrier-home move - ran through the full recovered lifecycle for each step
(Start Batch, the operation command, Program Step Status polling to Ready, End
Batch).

| Step | Operation | Device status | Indications | Result |
| ---: | --- | ---: | ---: | --- |
| 0 | `peristaltic_dispense` | 0 | 0 | completed |
| 1 | `shake` | 0 | 0 | completed |

Run `75b18f59-90f1-4f03-a30b-625ca8e20fac` finished `completed`, 2 of 2 steps,
controller returned to `idle`, and no crash marker was retained. This is
lifecycle and command-path evidence, not a volumetric or calibration study.

## Automated verification

The local suite still passes on the laptop after the transport changes:

```powershell
uv run python -m unittest discover -s tests -v
```

79 tests pass on Python 3.13.15. The serial transport was exercised against real
hardware rather than a unit fixture; adding a fake-serial regression is a
reasonable follow-up.

## Boundaries and follow-ups

- The serial path gives up FTDI descriptor-level device selection; it relies on
  the in-band product-serial preflight, which is sufficient with one FT232 on
  this Pi but should regain descriptor selection if multiple FTDI devices are
  ever attached.
- FastAPI/Uvicorn were not deployed to the Pi. Running the documented HTTP
  service on the RevPi would need the web dependency wheels transferred offline
  (or the Pi given internet) and is a natural next prototyping step.
- The D2XX loader now supports Linux, but `libftd2xx.so` is not installed on the
  Pi and the D2XX path was not exercised there. The serial path is the working
  one today.
- No fake-serial unit test exists yet; the `SerialByteTransport` timeout and
  framing parity are currently covered only by the live run.
- The prototype deploys a `PYTHONPATH` copy under `/home/pi/mf`, not a packaged
  install or a service unit. Productionizing would add a proper install, a
  systemd unit, and a stable crash-marker location.
- The dispense pumped liquid on operator request; physical setup (tubing, plate,
  cassette, waste) remains the operator's responsibility and is not something the
  driver can verify.
