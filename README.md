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

Read `phase_0.md` and `phase_1.md` before hardware work. No motion command is authorized by those procedures.
