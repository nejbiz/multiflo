# Phase 0 - Evidence and safe setup

Date: 2026-08-26 (Europe/Zurich)

## Outcome

The host, FTDI device, Python runtime, native transport choice, read-only command allowlist, packet fixtures, and guarded hardware procedure are recorded. The device was only enumerated during the initial Phase 0 inventory; no command was sent until the Phase 1 framing and fake-transport tests passed.

The installed primary peristaltic cassette is calibrated. This is operator-confirmed from its manufacturer certificate; the certificate identifier and date were not supplied. The local `calib1.LHC` file is a dispense protocol and is not used as calibration evidence.

## Reproducible environment

- Host: Windows 11, AMD64, 64-bit process.
- Python: CPython 3.13.15 at `C:\Users\nej\AppData\Local\Programs\Python\Python313\python.exe`.
- Python FTDI dependency: none. The implementation uses a small project-owned `ctypes` adapter.
- Selected native library: 64-bit `C:\Windows\System32\ftd2xx.dll`.
- D2XX DLL file version: 3.2.21.1; driver package product version: 2.12.36.20.
- The 32-bit system DLL and the older vendor-shipped DLL are not runtime dependencies.
- LHC application/assemblies are offline evidence only and are neither loaded nor called by the driver.

## Exact connected device

- Instrument family: base BioTek MultiFlo.
- Product serial returned by the instrument: `14071419`.
- FTDI serial: `14071419`.
- FTDI description: `MultiFlo`.
- VID/PID: `0403:6001`.
- Windows instances: `USB\VID_0403&PID_6001\14071419` and COM3 VCP sibling.
- D2XX device type: `0` (`FT_DEVICE_BM`).
- D2XX location ID at observation time: `0x23` / 35; this is recorded but is not a stable identity key.
- Basecode part number: `7210200`.
- Basecode software version: `1.12`.
- UI checksum/version: `ABFB` / `002`.
- Motion-controller checksum/version: `61FF` / `003`.
- Data version: `103`.

Installed capabilities returned by the device:

- primary peristaltic pump: installed;
- secondary peristaltic pump: not installed;
- syringe manifold: not installed (operator-confirmed physical configuration);
- washer and other liquid-handling modules: not installed (operator-confirmed peristaltic-only machine); and
- half-microliter mode: supported.

An exploratory syringe-manifold getter returned byte value `4`, but this conflicts with the physical machine and operator confirmation. It is recorded as a legacy/default response that is not authoritative for this peristaltic-only basecode and has been removed from the production inventory path. Queries for washer, external-valve, vacuum, ultrasonic, cell-washing, and Y-axis features returned device status `0x8107`; those unsupported getters are also excluded from production inventory.

## Transport configuration

The adapter selects by exact serial and exact description, then configures:

- 38400 baud;
- 8 data bits, no parity, 2 stop bits;
- no flow control;
- DTR asserted;
- RTS asserted;
- explicit read/write timeouts;
- RX and TX purge immediately after configuration; and
- one owned D2XX handle.

The observed FTDI latency timer is 16 ms. Phase 1 leaves it unchanged because no evidence yet justifies a different value.

## Read-only/non-motion allowlist

The following command IDs were recovered from offline managed-assembly IL and then verified on the connected device:

| Command | Request body | Purpose |
| ---: | --- | --- |
| `0x0073` | empty | communication test |
| `0x00A0` | empty | basecode/version information |
| `0x0100` | empty | product serial number |
| `0x0104` | one byte: pump 1 or 2 | selected peristaltic pump installed |
| `0x0154` | empty | half-microliter support |

No operation, reset, home, prime, purge, dispense, wash, aspirate, shake, abort, pause, resume, calibration, or maintenance command is allowed in the Phase 0/1 procedure.

## Safe hardware test procedure

1. Power the MultiFlo and leave it physically idle.
2. Save work and close Liquid Handling Control normally. Never terminate it automatically.
3. Confirm D2XX enumeration contains exactly one device with serial `14071419` and description `MultiFlo`, and that its open flag is clear.
4. Run all unit/fake-transport tests before opening hardware.
5. Open only by the expected serial and description; reject zero, multiple, already-open, or mismatched devices.
6. Configure and purge the transport as recorded above.
7. For the initial test, send only bodyless `0x0073`; do not substitute a motion command.
8. Require ACK `0x06`, a valid checksum, the matching command ID, and zero two-byte device status.
9. Repeat sequentially with one request in flight. Never retry automatically after a timeout or malformed response.
10. Close the D2XX handle before reopening LHC.

The physical emergency stop and power control should remain accessible even though this procedure contains no motion commands. No plate, tubing, liquid, cassette, or dispense range is authorized by Phase 0/1.

## Fixtures

- Communication request: `tests/fixtures/communication_test_request.hex`.
- Known `calib1` dispense request: `tests/fixtures/calib1_dispense_request.hex` (test data only; not authorized for Phase 0/1 hardware use).
- Source protocol SHA-256: `FF636FC2367E74A60FAC116B15B0EC0125D75541937DFEBA3244BA4ADD6286EC`.

## Open evidence

- The cassette calibration is operator-confirmed from a manufacturer certificate. Certificate number/date should be added if traceability beyond this confirmation is required.
- A read-only busy/idle command was not found. Offline IL shows `GetProgramStepStatus` reads local fields updated by run events; it does not query the instrument. Motion must remain blocked until Phase 2 establishes a reliable idle/reconciliation gate.
