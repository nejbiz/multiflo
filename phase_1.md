# Phase 1 - USB, framing, and read-only communication

Date: 2026-08-26 (Europe/Zurich)

## Outcome

Phase 1 has a working pure-Python frame codec, guarded native D2XX transport, scripted fake transport, serialized driver exchange, typed identity/version/module queries, regression tests, and repeated successful read-only communication with the intended instrument.

No motion or hardware-changing command was sent.

## Implementation

- `codec.py`: little-endian 11-byte header, additive two's-complement checksum, strict lengths, complete-frame decoder, and fragmented stream decoder.
- `transport.py`: exact D2XX enumeration/selection, open/configure/purge/read/write/close, plus `ScriptedFakeTransport`.
- `driver.py`: one in-flight exchange, ACK/NAK handling, response matching, device-status decoding, and typed read-only inventory.
- `errors.py`: stable validation, transport, protocol, device, busy, and ambiguous-execution errors.
- `tools/hardware_smoke.py`: serial-guarded script that can send only `0x0073`.
- `tools/hardware_inventory.py`: serial-guarded identity, firmware, and installed-module inventory.
- `tools/dump_managed_il.ps1`: offline evidence helper; vendor code is never a runtime dependency.

## Important hardware correction to the original plan

The request golden packet is correct, but the connected base MultiFlo's response behavior differs from the planned response description.

Observed communication-test transaction:

```text
TX: 01 02 73 00 01 07 00 00 00 82 FF
RX: 06
RX: 01 00 73 00 00 00 00 02 00 8A FF 00 00
```

The request above used message ID 7 deliberately. The instrument:

- sends an ASCII ACK byte `0x06` before the response frame;
- returns first header byte `1`, destination byte `0`, source byte `0`;
- returns message ID `0` instead of echoing request message ID 7;
- declares a two-byte body; and
- returns body `00 00`, interpreted as zero device status.

The response checksum `0xFF8A` validates over header bytes 0-8 and body bytes. The parser now supports this observed base-MultiFlo profile. The response-class/destination/source/message-ID profile stated in `plan.md` remains an unverified hypothesis for other firmware and is covered only by fake-transport compatibility tests.

Because this device does not echo message IDs, correctness depends on the required one-request-in-flight rule and command-ID matching.

## Tests

Run from the repository root:

```powershell
python -m unittest discover -s multiflo\tests -v
```

Covered cases:

- communication-test golden request;
- existing `calib1` dispense golden request (codec only);
- complete and byte-by-byte fragmented frames;
- malformed declared length;
- truncated packet;
- bad checksum;
- mismatched response command;
- ACK and NAK handling;
- nonzero device status;
- read timeout;
- disconnect during a fragmented response;
- indication before a planned-profile response; and
- fake transport open/closed behavior, splitting, and injected failures.

Final result after the peristaltic-only profile correction: 18 tests passed on Python 3.13.15.

## Hardware evidence

The guarded smoke test completed 10 consecutive `0x0073` exchanges:

- 10 ACKs received;
- 10 valid response checksums;
- 10 matching command IDs;
- 10 zero device statuses;
- no indications; and
- no timeout or disconnect.

The typed hardware inventory then completed successfully and returned:

```text
product serial:              14071419
basecode part/version:       7210200 / 1.12
UI checksum/version:         ABFB / 002
motion checksum/version:     61FF / 003
data version:                103
primary peristaltic:         installed and manufacturer-calibrated
secondary peristaltic:       not installed
syringe manifold:            not installed (operator-confirmed)
other liquid modules:        not installed (peristaltic-only machine)
half-microliter support:     enabled
FTDI latency timer:          16 ms (observed, unchanged)
```

## Failure behavior

- A missing ACK, invalid header/profile, wrong command, malformed length, or checksum mismatch raises `ProtocolError`.
- NAK or nonzero response status raises `DeviceError`.
- D2XX timeouts, disconnects, selection failures, and short writes raise `TransportError`.
- The driver does not retry commands.
- The D2XX handle is closed through context-manager cleanup on success or error.

## Remaining safety gates

Phase 1 meets its hardware completion criterion: exact device selection and repeated read-only communication succeed. Two items remain deliberately unresolved before Phase 2 motion:

1. No authoritative read-only busy/idle command has been verified. LHC's `GetProgramStepStatus` is event-backed local state, not a device query. Phase 2 must establish safe startup reconciliation before enabling `0x008F`.
2. The primary cassette calibration is operator-confirmed from its manufacturer certificate. Its certificate identifier/date remain optional documentation fields; there is no syringe manifold to calibrate.

Until both gates are handled, the implemented hardware tools remain read-only and the known dispense packet remains fixture-only.
