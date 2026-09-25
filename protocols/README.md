# Controlled `.LHC` fixtures

These files are offline protocol evidence, not runtime inputs. The current
behavior and safety limits are in [plan.md](../plan.md); packet assertions are
in [test_codec.py](../tests/test_codec.py). The fixture numbers refer to the
original controlled LHC comparisons:

| Files | Changed parameter or operation |
| --- | --- |
| `calib1`–`calib7` | Dispense volume, 96-well plate type, flow, and column map |
| `calib8`–`calib9` | Primary peristaltic prime and purge |
| `calib10`–`calib16` | Shake, soak, height, and unused delay variants |
| `calib17`–`calib22` | Pre-dispense volume/cycles and cassette declarations |
| `calib23`–`calib25` | 384-well dispense, prime, and odd rows |
| `calib26`–`calib29` | Signed X/Y dispense offsets |
| `calib30`–`calib32` | Even rows and alternating 384-well columns |

`calib1` is the 100 uL, medium-flow 96-deep-well dispense used by the golden
request test. A fixture reflects LHC's encoding; it does not authorize motion
or prove that a physical instrument completed the step.
