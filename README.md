# pydmx — a CSV-driven DMX lighting controller

A small lighting desk for macOS. Scenes and chasers are plain CSV files, an
Akai APC mini mk2 is the control surface, and chasers can lock to the beat of
whatever Virtual DJ is playing.

Everything runs without hardware, so a show can be built and tested on a
laptop on a train: an on-screen APC, a DMX channel monitor, and a dry-run
output path.

```
   CSV show files ──▶ engine ──▶ DMX out ──▶ fixtures
                        ▲
        APC pads ───────┤
      beat clock ───────┘
```

---

## Hardware

| Part | Notes |
|---|---|
| DSD TECH SH-RS09B | USB-to-RS485. **Not** a DMX interface — it has no DMX firmware, so this software generates the DMX512 timing itself. Any FTDI RS485 adapter should work. |
| Akai APC mini mk2 | Class-compliant USB MIDI, no driver needed. Optional — the simulator replaces it. |
| 120Ω resistor | Across XLR pins 2 and 3 at the **last** fixture. Missing termination causes reflection flicker that gets worse with cable length and is easy to misdiagnose as a software bug. |

XLR pin 1 = ground, 2 = data−, 3 = data+. **If nothing responds, swap pins 2
and 3 before debugging code** — A/B labelling on RS485 adapters is unreliable.

Because DMX timing is generated in software, expect solid results on LED
washes and dimmers and occasional stutter on smooth moving-head fades. An
Enttec DMX USB Pro does the timing in hardware and is the upgrade path.

## Install

```bash
pip install pyserial mido python-rtmidi
pip install zeroconf          # optional, for OS2L discovery
```

Python 3.10+. The core logic modules have no third-party dependencies —
`pyserial` is only for DMX output and `mido` only for MIDI.

## Quick start

**With no hardware at all**, three terminals:

```bash
python3 controller.py --sim --no-dmx --monitor   # the desk
python3 apcsim.py                                # on-screen APC
python3 dmxmon.py                                # live DMX channel view
```

**With hardware:**

```bash
python3 controller.py --check    # validate the CSVs, touch nothing
python3 controller.py            # go
```

---

## Building a show

Four CSV files in `show/`. Blank lines and `#` comments are ignored
everywhere.

### 1. `profiles.csv` — fixture types

One row per feature of each model. Write each model once however many you own.

```csv
profile,feature,offset,mode,values
led_par,dimmer,1,fade,off=0;full=255
led_par,red,2,fade,
led_par,green,3,fade,
led_par,blue,4,fade,
led_par,colour,5,snap,off=0;red=10;amber=8-15;green=42
led_par,strobe,6,snap,off=0;slow=10-100;fast=101-200
```

- **`offset` is 1-based**, matching the manual's own "Channel 1: Dimmer"
  numbering. Transcribe without arithmetic.
- **`mode` is the most important column in the whole project.** See below.
- **`values`** is optional plain-text naming, separated by `;` or `|`. A range
  like `8-15` is usually better than a single number: a name resolves to the
  **middle** of its range, and the edges are where an off-by-one lands you in
  the next colour. `dmxmon` shows the matching name beside the live value.

#### fade vs snap

| | meaning | fading it | merging two |
|---|---|---|---|
| `fade` | a **level** — intensity, RGB, pan/tilt | fine | HTP, highest wins |
| `snap` | a **selector** — colour wheel, gobo, strobe, mode | **never** | LTP, latest wins |

A snap value is an index into the fixture's lookup table, not a brightness.
Crossfading a colour wheel from red to blue sweeps through every colour in
between. Halving colour index 42 gives index 21 — a *different colour*, not a
dimmer one. This is why the master fader never touches snap channels and why
level and scale faders are refused on them.

### 2. `fixtures.csv` — the patch

One row per physical fixture. `channel = address + offset − 1`.

```csv
fixture,profile,address
wash1,led_par,1
wash2,led_par,7
bar1,led_bar,20
```

Keep names globbable — a consistent prefix per group means scenes can target
`wash*` in one row.

### 3. `scenes.csv` — the looks

**Scenes are sparse**: list only the channels a scene touches. Everything else
contributes nothing, which is what lets scenes stack.

```csv
scene,fixture,feature,value
warm,wash*,dimmer,180
warm,wash*,red,255
warm,wash*,green,120
red_all,*,colour,red
```

- `fixture` accepts a glob: `wash*`, `*`.
- `value` accepts a number **or** a name from `profiles.csv`. Names resolve
  per fixture, so `*,colour,red` gives each model its own correct number.
- A glob hitting a fixture that lacks the feature warns and skips it. Naming
  a fixture explicitly and getting the feature wrong is an error.

> **A scene of zeros cannot turn anything off.** Under HTP it loses every
> comparison, so it is indistinguishable from being inactive. Going dark means
> *deactivating* sources — use a `clear` pad, or a `solo` pad if you want one
> look left standing. A `solo` pad drops running chasers as well as scenes;
> see the note under `mode` below.

### 4. `chasers.csv` — sequences

```csv
chaser,step,scene,duration_ms,beats
walk,10,warm,800,
walk,20,cold,800,
barwalk,10,warm,,4
barwalk,20,cold,,4
manual,10,warm,,
manual,20,cold,,
```

- **Steps sort by the `step` column**, not file order, so you can insert 15
  between 10 and 20 without renumbering.
- `duration_ms` auto-advances. **0 or blank means hold** until something else
  advances it, so a fully manual chaser is just a chaser with no timers.
- `beats` locks the step to musical time. **All steps must declare beats** or
  the chaser falls back to timers and warns — deriving position needs a known
  cycle length.

Beat-synced chasers derive their position from the track's beat number rather
than counting. Pause, seek and deck changes therefore need no handling: the
chaser is always wherever the music says it is.

### 5. `mapping.csv` — the surface

```csv
pad,type,target,mode,colour,shift
r0c0,scene,warm,toggle,red,
r0c1,scene,strobe_hit,flash,white,
r1c0,chaser,barwalk,toggle,cyan,
r1c1,chaser_step,,,white,
r0c0,scene,special,toggle,mauve,yes
t1,clear,,,red,
s1,reload,,,cyan,
t8,tap,,,magenta,
f9,master
f1,level,wash*.dimmer
f2,scale,wash*.dimmer
```

**`pad`** — `r0c0` grid (row 0 is the **bottom** row, note = row×8+col),
`t1`–`t8` track buttons, `s1`–`s8` scene launch (s1 is the top one),
`f1`–`f9` faders, or a raw note number.

**`type`**

| type | what it does | uses `mode`? |
|---|---|---|
| `scene` | activate a look | yes |
| `chaser` | start/stop a chaser | yes |
| `chaser_step` | advance a chaser one step. Blank target = every running chaser | no |
| `tap` | tap tempo for the internal clock | no |
| `clear` | drop everything (panic button) | no |
| `reload` | re-read the CSVs live | no |
| `master` *(fader)* | global level, scales fade channels only | no |
| `level` *(fader)* | adds to a group — HTP, can only raise | no |
| `scale` *(fader)* | multiplies a group — can only lower | no |
| `bpm` *(fader)* | 60–180 BPM for the internal clock | no |

**`mode`** — `toggle` stacks, `solo` becomes the only live source, `flash` is
active only while held. Leave blank on the types that ignore it; writing one
there warns.

> **`solo` stops chasers too.** "The only live source" is literal: a solo pad
> drops *everything* running — every scene and every chaser, including a
> beat-synced one — and then starts its own target. That is deliberate, but it
> is easy to meet by accident when a solo scene pad silently ends the chaser
> driving the room. Use `toggle` if you want the chaser to survive.
>
> `flash` is the opposite: it touches only its own target. It never stops
> anything else. Note that the release stops that target whoever started it,
> so flashing a chaser that is already running from another pad will stop it
> when you let go.

**`colour`** — a name (`red`, `amber`, `yellow_warm`, `yellow`, `green`,
`turquoise`, `cyan`, `blue`, `lavender`, `mauve`, `magenta`, `pink`,
`warm_white`, `white`, `cold_white`, `uv`) or a raw palette index 0–127.
Faders have no LEDs, so leave it blank there.

**`shift`** — `yes` binds to the SHIFT layer, giving a second full 64-pad
layer. Unshifted bindings show through where the shift layer has nothing.

**Level and scale faders** take `fixture-glob.feature` as their target, e.g.
`wash*.dimmer`. Both are refused on snap features.

---

## Beat sync with Virtual DJ

OS2L over TCP. **Virtual DJ connects to us**, not the other way round.

1. VDJ → Settings → search `os2l` → set `os2l` to `auto` and `os2lDirectIp`
   to `127.0.0.1:9996`
2. `python3 controller.py --os2l`
3. **Press a DMX pad in Virtual DJ once.** It will not connect otherwise —
   DNS-SD advertisement does not avoid this, it was tested.

### Fallback when VDJ is unavailable

Bind a fader to `bpm` and/or a pad to `tap`. The internal clock stays silent
until given a tempo and yields to Virtual DJ whenever VDJ is actually
delivering beats. Two taps give a tempo; the tap itself becomes the downbeat.

---

## Tools

| Command | Purpose |
|---|---|
| `controller.py --check` | validate the CSVs, print the patch, touch no hardware |
| `dmxmon.py` | live view of patched channels with names and colour swatches |
| `apcsim.py` | on-screen APC mini mk2 |
| `play_scene.py` | output one scene, no MIDI needed |
| `apc_dump.py --grid` | live view of what the real APC is sending |
| `apc_leds.py names` | check the 16 named colours on the device |
| `os2l_test.py` | inspect raw Virtual DJ messages |
| `os2l_drive.py <chaser>` | run one chaser from the beat clock, no hardware |
| `dmx_test.py` | adapter smoke test — holds two raw channels, no show files |
| `dmx_cycle.py` | adapter smoke test — cycles one channel |

`dmx_test.py` and `dmx_cycle.py` predate the engine and depend on nothing
else. They are the right first thing to run on a new adapter or a new cable:
if a fixture responds to them, the hardware is good and any later problem is
in the show files.

Controller flags: `--check`, `--sim`, `--no-dmx`, `--no-midi`, `--monitor`,
`--os2l [port]`, `--watch`, `--beats`, `--feedback <style>`.

`--watch` auto-reloads when the show files change. The watcher thread only
notices the change; the main loop does the reload, on the same code path as
the `reload` pad. It waits for the files to stop changing before asking, so
saving all five CSVs at once is one reload rather than five, and a file caught
mid-write is not read as a typo. Each save is reported once — if the files do
not parse, the running show is kept and the next save is what tries again.

## Tests

```bash
python3 -m unittest discover -s tests -t tests
```

146 tests, no hardware and no third-party packages required. They cover the
merge policy, patch arithmetic, chaser phase-locking, OS2L framing, reload
reconciliation and the tempo maths — the decisions that are easy to
"simplify" into bugs.

## Layout

```
controller.py     main loop, input routing, startup
engine.py         scene stacking, chaser clocking, channel merging
showfile.py       CSV parsing and the reloadable Show object
dmx.py            DMX512 timing over serial (+ NullSender for dry runs)
apc.py            APC mini mk2 MIDI wrapper
os2l.py           Virtual DJ beat clock listener
tempo.py          internal clock: tap tempo and fader BPM
colours.py        the 16 named colours and the device palette
monitor.py        UDP tap so dmxmon can watch the output
simlink.py        UDP protocol between controller and apcsim
virtualapc.py     drop-in stand-in for apc.py, talks to apcsim
show/             the CSV show definition
tests/            unittest suite, hardware-free
REVIEW.md         known bugs and improvements, ordered by risk
```

## Project files

- `REVIEW.md` — known bugs and improvements, ordered by how likely they are
  to hurt during a set. Read before adding features.
- `CLAUDE.md` — invariants and hard-won hardware details, for anyone (or any
  LLM) working on the code.
- `TODO.md` — deferred validation, notably the full-load test with every
  fixture and Virtual DJ running together.

## Known quirks

- Virtual DJ needs a DMX pad press per session before it connects.
- OS2L `feedback` can only address named buttons — pads configured as
  `os2l_cmd` send a numeric id and cannot be lit.
- SysEx pad colouring on the APC is unverified; `--feedback rgb` is
  experimental and the palette path is the default.
- APC pulse and blink rates sync to an external MIDI clock, so with none
  running they use the device default and may not animate.
- LEDs hold their state if the process is killed with `kill -9`. Recover with
  `python3 apc_leds.py off`.

## License

MIT — see `LICENSE`.
