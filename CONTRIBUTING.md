# Contributing

## Setup

```bash
pip install -r requirements.txt
python3 -m unittest discover -s tests -t tests
```

The test suite needs no hardware and no third-party packages — the core
logic modules are stdlib-only.

## Working without hardware

```bash
python3 controller.py --sim --no-dmx --monitor   # terminal 1
python3 apcsim.py                                # terminal 2
python3 dmxmon.py                                # terminal 3
```

## Before opening a PR

- `python3 -m unittest discover -s tests -t tests` passes
- `python3 controller.py --check` still loads the example show
- New behaviour has a test; new CSV columns are documented in `README.md`
- Read `CLAUDE.md` — it lists the invariants that are easy to break silently

## Testing on hardware

Some things cannot be verified in software and should be re-checked on a
real rig after touching the relevant module:

| Changed | Re-test |
|---|---|
| `dmx.py` timing | a fixture held at a static level for a minute, watching for flicker |
| `apc.py` LED code | `apc_leds.py names`, then pad state under rapid pressing |
| `os2l.py` | a full track including a pause and a deck change |
| `colours.py` | `apc_leds.py names` on the device |
