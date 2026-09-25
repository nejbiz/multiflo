# MultiFlo

Pure Python control for one base BioTek MultiFlo. It supports the primary
peristaltic pump, shake, and soak through a small FastAPI service or a guarded
command-line tool. The [plan](plan.md) is the source of truth for scope, safety,
implementation status, and known gaps.

## Quick start

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/). From this directory:

```powershell
uv sync --locked
uv run --locked python -m unittest discover -s tests -q
```

On Windows, install the native FTDI D2XX library separately. The service owns
one USB handle and listens on loopback by default:

```powershell
uv run --locked python -m multiflo.service --expected-serial YOUR_SERIAL
```

For serial transport on a supported Python version, use
`uv sync --locked --extra serial`. On the RevPi's Python 3.11 prototype,
install `pyserial` in its environment and use
`python -m multiflo.tools.revpi_run --check --expected-serial YOUR_SERIAL`
for a read-only inventory. See [transport](src/multiflo/transport.py) and the
[RevPi tool](src/multiflo/tools/revpi_run.py) for options.

The service exposes OpenAPI at `/docs`; an offline schema can be exported with
`python -m multiflo.tools.export_openapi --output openapi.json`.

## Repository

| Path | Purpose |
| --- | --- |
| [src/multiflo](src/multiflo) | Models, codec, transports, driver, runner, API, service, and guarded tools |
| [tests](tests) | Unit, golden-packet, fake-transport, runner, and API tests |
| [protocols](protocols) | Controlled `.LHC` evidence fixtures |
| [manuals](manuals) | Operator-manual evidence |
| [plan.md](plan.md) | Current scope, behavior, evidence, and follow-ups |

Hardware commands need the correct physical setup and explicit authorization.
Read the [hardware procedure](plan.md#hardware-procedure) before using a tool
that can move the instrument.
