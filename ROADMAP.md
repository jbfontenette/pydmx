# Roadmap — DMX Controller

Where this is going, and what each idea actually costs against the code that
exists. Known bugs live in `REVIEW.md`; hardware and validation notes live in
`TODO.md`. This file is for work not started.

**How to read it.** The priorities are the owner's. The cost analysis is from
reading the code, and every claim about the current implementation names the
function it came from so it can be checked. Claims about hardware the project
does not own are marked **[unverified]** — they come from published
specifications, not from a device on a bench, and they are exactly the kind of
thing the `Hard-won details` section of `CLAUDE.md` earns the right to state
and this file does not. The X-Touch Mini claims used to carry that mark and no
longer do: the device is on the bench and every control has been walked on it.
Two of the specification-derived guesses turned out wrong, which is the whole
argument for the mark.

Ideas keep the numbers they were first listed with, so they stay referable
even as the order changes.

---

## Four findings that shape the order

**1. Output and LED repaint share one flag.** In `controller.py`'s main loop
(line 816 today):

```python
if eng.dirty:
    sender.apply(eng.output())
    if surface and not state["flash_until"]:
        build_leds(surface, show, eng, style, state["layer"])
```

`dirty` only flips on an event today, so this is fine. Any feature whose
output changes *between* events — fades (1), automated movement (15) — makes
`dirty` true on most ticks, which repaints the whole surface at 200 Hz. That
is 80 MIDI messages a repaint, and `CLAUDE.md` already records what happens:
the output queue floods and the pads freeze. **Splitting these two flags is
the first task of any fade work**, not an afterthought.

**2. Dropping crossfade shrinks that foundation a lot.** A crossfade needs
two sources contributing *in proportion*, and `engine.output()` has no notion
of a weight — sources merge HTP or LTP, full strength. Fade-in and fade-out of
a single step need only a time-varying multiplier on one source, which HTP
already handles. So with crossfade deferred, the work is "let output change
between events", not "add weights to the merge model".

**3. Encoders need no foundation at all.** Turning an encoder is an input
event, exactly like moving a fader — and the X-Touch's are **absolute**, so
they reuse `apply_fader` rather than needing a binding kind of their own.
Confirmed twice: by the probe, then by the driver, which added no engine
concept at all. Manual pan/tilt is therefore reachable now, with no
continuous rendering. Only *automated* movement needs finding 1 solved.

**4. A digital BPM readout has nowhere to go but the screen.** Neither the
APC nor the X-Touch Mini has a numeric display. The X-Touch's LED rings *can*
be driven by the controller — the driver does it for level and scale
encoders — but thirteen segments cannot spell a number, so they still do not
answer this. The controller already prints `bpm 120.0` after a settle; the
problem is that it scrolls away. The requirement is a *persistent* readout, and that is a
screen decision — an in-place status line, or the separate-process pattern
`monitor.py` and `dmxmon.py` already establish.

---

## Priority

| Now | Medium | Later |
|---|---|---|
| ~~**3** X-Touch Mini~~ — done, see below | **2** beat fractions | **1b** crossfade |
| **16** choose the show folder from the command line | **1a** fade-in / hold / fade-out | **6** beat sync from audio |
| | **7** section-aware chasers via OS2L | **9** scenes of scenes |
| | **4** Ableton Link / Rekordbox | **11** GUI for configuration |
| | **5** Pro DJ Link | **12** GUI for fixture preview |
| | **8** independent solo groups | **13** scene recording |
| | **10** live BPM readout | **14** software pages |
| | **15a** manual pan/tilt on encoders | **15b** automated movement |
| | | **3b** two surfaces at once |

---

## Now

### 3. X-Touch Mini — DONE

Both halves are built: the probe that measured the device
(`xtouch_dump.py`, `xtouch_leds.py`) and the driver that uses it
(`xtouch.py`), selected with `--surface xtouch`. `README.md` carries the
control vocabulary; `CLAUDE.md` carries what the hardware turned out to be.

**The whole surface is one rule:** to drive a control, send the number that
control *sends*, on the layer currently showing. The other layer's numbers
are discarded — dropped as they arrive, not queued.

| | layer A | layer B |
|---|---|---|
| buttons, in and out | notes 8–23 | notes 32–47 |
| encoders and their LED rings | CC 1–8 | CC 11–18 |
| encoder push | notes 0–7 | notes 24–31 |
| fader | CC 9 | CC 10 |

Everything on MIDI channel 10. Note the fader irregularity: layer B's is
CC 10, not the CC 18 a uniform offset predicts.

**What it cost, against what this file estimated.** Two of the three design
problems were cheaper than expected and a fourth appeared:

- **Encoders needed no new binding kind.** They are absolute, so
  `apply_fader` took them unchanged. This was the fork the probe existed to
  resolve and it fell the cheap way.
- **Control naming** went as planned, but generalised further than
  estimated: the vocabulary moved out of `showfile.parse_pad` into each
  surface module, so `showfile.py` no longer knows what an APC is.
- **Layers** became one `layer` column serving both devices, as sketched.
- **Layer tracking was the new work.** The device never announces a switch,
  so the driver infers it and reports a `('layer', index)` event. That seam
  also removed `controller.py`'s knowledge of which note SHIFT is.

**Two things the hardware settled that no plan could.** Button LEDs are
binary, so the APC's idle-glow scheme has no equivalent and a bound button
looks like an unbound one — the X-Touch shows *active* state instead. And a
ring's display style is a device-side setting made in X-Touch Editor, not
reachable over MIDI: the controller owns the value, the editor owns how it
is drawn.

**Still deferred, and why.** `xtouchsim` — the device is on the bench, so a
simulator earns nothing yet; it mirrors `apcsim.py` when a show needs
building away from the hardware. Two surfaces at once stays in **3b**.

### 16. Choose the show folder from the command line

Added after the original fifteen. Small, and it unblocks everything else that
needs a show to test against.

`showfile.Show(directory)` already takes a path — the plumbing is there. What
is missing is the wiring: `SHOW_DIR = "show"` is a module-level constant in
`controller.py`, `play_scene.py`, `dmxmon.py` and `os2l_drive.py`, so every
tool can only ever read the one folder beside it. A `--show PATH` flag on
each, defaulting to today's behaviour, is most of the work.

Why it matters more than its size suggests: a second show cannot exist
alongside the first. Different venues, a stripped-down rehearsal rig, or a
copy to experiment on all mean editing the live files in place. It also costs
time during development — every end-to-end check on this project so far has
had to copy `show/` into a scratch directory and run from there, purely
because the path could not be passed in.

Two details to settle when it is built:

- `Show._resolve_mapping` looks for `mapping.csv` in the show directory and
  then *beside the script*. That fallback exists because a mapping in the
  wrong place used to load zero bindings silently. With an explicit
  `--show PATH`, falling back to a `mapping.csv` from somewhere else becomes
  surprising rather than helpful — decide whether it still applies.
- `--watch` needs nothing: it watches whatever directory the `Show` was built
  with, so it follows the flag for free.

---

## Medium

### 2. Beat fractions (1/2, 1/4, 1/8)

Cheaper than it looks. `Chaser.step_at(pos)` is `pos % cycle_beats` and
already works unchanged on floats. The work is parsing `1/2` in
`chasers.csv`, and giving the engine a *fractional* position between beats —
`pos + (now - beat.at) / period` — evaluated in `Engine.tick()`, which today
skips beat-synced chasers entirely.

Critically this stays **derived**, so invariant 4 survives: a pause, seek or
deck change still needs no handling. At 1/8 and 180 BPM a step is 41 ms
against a 200 Hz loop, so there is room.

### 1a. Fade-in / hold / fade-out per step

Needs finding 1 solved first — split the output flag from the repaint flag.
Then a step gains optional fade-in and fade-out durations around its hold.

Two constraints that are not negotiable: **snap channels are never
interpolated** (invariant 1 — half of colour index 42 is a different colour,
not a dimmer one), and interpolation runs on the main loop, never on the DMX
thread (invariant 2).

### 7. Section-aware chasers, from OS2L

There is a cheap version available now and it needs no audio analysis at all.
`os2l.BeatClock.poll_messages()` already receives VirtualDJ's `btn` and `cmd`
events, and `controller.py` currently **logs and discards them**. VirtualDJ
can fire named buttons from POI markers, so "different chaser at the drop"
works from track markers today.

Automatic detection of intro/build/drop from audio is explicitly *not* this
item — see 6.

### 4 and 5. Ableton Link, and Pro DJ Link

The cheapest category architecturally: the seam is already right. A clock
source is anything with `poll() -> [Beat]`, `alive` and `bpm`, so each is a
new module and nothing else changes.

- **4, Ableton Link** — the natural fit for Rekordbox in performance mode.
  Carries tempo *and* phase, which maps directly onto `Beat`. **[unverified]**
  needs a Link binding for Python.
- **5, Pro DJ Link** — the CDJ protocol. Reverse-engineered but documented,
  with existing Python implementations to learn from. **[unverified]**

One real design task once a third source exists: the controller's arbitration
is currently "VirtualDJ if alive, else internal", written inline in the main
loop. Three or more sources need a stated priority policy.

### 8. Independent solo groups

An engine change plus a `group` column. Worth doing partly because it is the
principled answer to the open question in `REVIEW.md` item 3: "solo among its
own group" subsumes the solo-scope decision that was deferred there.

### 10. Live BPM readout

See finding 4. The minimum useful version is a persistent numeric BPM, always
visible, whichever clock is driving. Two routes:

- **In-process status line** — cheapest, and the constraint is that it must
  never do enough work to matter to the main loop.
- **Separate HUD process over the monitor tap** — follows the existing
  `monitor.py` / `dmxmon.py` pattern, which exists precisely so a viewer can
  never stall the controller. The tap carries only the 513-byte frame today,
  so this needs a small status datagram (bpm, clock source, active sources) —
  work that any later GUI would also use.

### 15a. Manual pan/tilt on encoders

Half-arrived with the X-Touch driver, by finding 3. An encoder bound to
`level` already drives a channel group and shows its value on the ring, so a
pan or tilt channel can be turned by hand today. What is missing is a
`position` type that names pan and tilt together, and one real gap to fix
alongside it: **16-bit pan/tilt is not modelled anywhere.** Most moving heads split pan
and tilt across a coarse and a fine channel, and `showfile.py` has no notion
of pairing two channels into one value. That limits precision regardless of
what is turning the knob.

---

## Later

Each keeps its analysis so picking it up starts from the findings.

**1b. Crossfade between steps.** Deferred from 1. Needs weighted merging in
`engine.output()`, which is the part of the fade work that is genuinely new
machinery rather than new timing.

**6. Beat sync from audio decode.** Real-time onset detection is latent,
CPU-hungry, and CPU is the one resource the DMX timing cannot spare —
`_wire_free_at` exists because `time.sleep()` overshooting truncates a frame.
Research, not a feature, and the last of the clock sources for good reason.

**9. Scenes of scenes.** Flatten at load in `showfile.py` and it is contained,
with cycle detection. One semantic decision to write down first: flattening
equals stacking for fade channels, because HTP is associative, but **not** for
snap channels, where LTP order decides the winner — so composition order has
to be defined rather than discovered.

**11. GUI for configuration.** The stated preference is to stay with text
files. If it ever happens, the constraint that keeps it compatible with
everything else is that **the CSVs stay canonical**: a GUI that edits the same
files and lets `--watch` reload them adds no second source of truth.

**12. GUI for fixture preview.** Needs data that does not exist yet — fixture
position, orientation, beam angle — which is the same data a visualiser for
15 would want. Out of process, over the tap, for the same reason as 10.

**13. Scene recording from the live DMX state.** Both halves already exist:
`DmxSender.snapshot()` returns the live frame, and `Patch._by_channel` maps a
channel back to (fixture, feature). What is missing is the *process* — which
channels to capture, given scenes are deliberately sparse (invariant 8), how
the scene is named, and when the file is written relative to a reload. Best
picked up alongside 11.

**14. Software pages.** The APC's SHIFT already gives a second layer, and the
X-Touch's A/B is handled by the device for input (though its LED output still
needs the controller to track which layer is showing). Generalising to N pages is a
`binding_for` and `build_leds` change with the engine untouched — worth doing
when 64 pads plus a layer stops being enough, not before.

**15b. Automated movement patterns.** Circles, figure-eights and the like are
generated output, so they need finding 1 solved exactly as fades do.

**3b. Two surfaces at once.** APC grid plus X-Touch encoders simultaneously.
`_SURFACE_MODULE` is a module-level global and `main()` builds a single
`surface`, so the whole design assumes swapping, not combining. Event sources
would have to merge and LED state become per-device.

---

## The cross-cutting risk: CSV format churn

Ideas 1, 2, 8, 9, 14 and 15 all want new columns or new semantics in the show
files — and `show/chasers.csv` is over two thousand hand-maintained lines.
Landing them piecemeal means migrating that file repeatedly.

Two rules make that survivable:

1. **Decide the whole format shape once**, even if it is implemented over
   months, so the migration is thought about once rather than six times.
2. **Every addition is optional with a blank default**, so existing files keep
   parsing unchanged and an old show never needs rewriting to run on new code.

`CLAUDE.md` already requires parsing, a warning when a column is set on a type
that ignores it, README documentation and a test for every new column.
Deciding the shape up front is what stops that bill being paid six times over.

---

## What is already on main

Everything the show has run on, plus three branches merged once the X-Touch
probe was finished. Nothing in this file is blocked on unmerged work.

- **The `REVIEW.md` cleanup.** Every open item closed or recorded as
  deliberately left. One of them matters to the work below:
  `surface_constants.py` now holds the APC constant tables that were
  duplicated across `apc.py`, `virtualapc.py` and `apc_leds.py`, so the
  X-Touch driver does not add a fourth copy. That duplication is not a
  hypothetical risk — the X-Touch LED probe wrote the MIDI channel down a
  second time, as 0 instead of 10, and a whole hardware session was spent
  testing an LED protocol that was working fine.
- **The X-Touch probe.** `xtouch_dump.py` and `xtouch_leds.py` with their
  tests: the measured control map that section **3** rests on. Standalone,
  the way `apc_dump.py` and `apc_leds.py` were before `apc.py` existed.
- **This file.**

`main` is also what ran the six-hour show, and stays the branch to be careful
with. The habit that produced these merges is worth keeping: build each
subsystem standalone, prove it on the hardware, and merge it only once its
findings are written down.
