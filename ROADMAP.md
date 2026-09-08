# Roadmap — DMX Controller

Where this is going, and what each idea actually costs against the code that
exists. Known bugs live in `REVIEW.md`; hardware and validation notes live in
`TODO.md`. This file is for work not started.

**How to read it.** The priorities are the owner's. The cost analysis is from
reading the code, and every claim about the current implementation names the
function it came from so it can be checked. Claims about hardware the project
does not own yet are marked **[unverified]** — they come from published
specifications, not from a device on a bench, and they are exactly the kind of
thing the `Hard-won details` section of `CLAUDE.md` earns the right to state
and this file does not.

Ideas keep the numbers they were first listed with, so they stay referable
even as the order changes.

---

## Four findings that shape the order

**1. Output and LED repaint share one flag.** In `controller.py`'s main loop
(line 688 today):

```python
if eng.dirty:
    sender.apply(eng.output())
    if surface and not state["flash_until"]:
        build_leds(surface, show, eng, style, state["shift"])
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
event, exactly like moving a fader. Manual pan/tilt control on encoders is
therefore reachable as soon as the X-Touch driver exists — no continuous
rendering required. Only *automated* movement patterns need finding 1 solved.

**4. A digital BPM readout has nowhere to go but the screen.** Neither the
APC nor the X-Touch Mini has a numeric display, so LED rings cannot answer it.
The controller already prints `bpm 120.0` after a settle; the problem is that
it scrolls away. The requirement is a *persistent* readout, and that is a
screen decision — an in-place status line, or the separate-process pattern
`monitor.py` and `dmxmon.py` already establish.

---

## Priority

| Now | Medium | Later |
|---|---|---|
| **3** X-Touch Mini: validate, then drive (swap model, A/B, encoder push) | **2** beat fractions | **1b** crossfade |
| | **1a** fade-in / hold / fade-out | **6** beat sync from audio |
| | **7** section-aware chasers via OS2L | **9** scenes of scenes |
| | **4** Ableton Link / Rekordbox | **11** GUI for configuration |
| | **5** Pro DJ Link | **12** GUI for fixture preview |
| | **8** independent solo groups | **13** scene recording |
| | **10** live BPM readout | **14** software pages |
| | **15a** manual pan/tilt on encoders | **15b** automated movement |
| | | **3b** two surfaces at once |

---

## Now

### 3. X-Touch Mini — validate first, then drive

The end state is APC alone, X-Touch alone, or both together. **Both together
is deferred**; one surface at a time is the target for now, which the code
already does for `apc` versus `virtualapc`. Extending that to four choices —
`apc`, `apcsim`, `xtouch`, `xtouchsim` — is a flag change, not an
architecture change. Two devices at once would mean merging event sources and
keeping LED state per device, which is a different and much larger job.

**Step one is a standalone probe, before any of it touches `controller.py`.**
That is how every subsystem here was built (`CLAUDE.md`, *When adding a
feature*): the APC was proven by `apc_dump.py` for input and `apc_leds.py` for
output, and only then wrapped in `apc.py`. So:

- **`xtouch_dump.py`** — input monitor, the `apc_dump.py` of this device.
  What it has to establish, because the specs cannot be trusted until a device
  confirms them:
  - note numbers for the 16 buttons and for the 8 **encoder push switches**;
  - CC numbers for the 8 encoders and the fader;
  - what the **layer A/B** button does — **[unverified]** the device is
    documented to handle layers internally and send a *different* set of note
    and CC numbers on layer B, which if true means the controller never needs
    a page state machine for it;
  - **the big one: whether the encoders send absolute positions or relative
    deltas.** **[unverified]** This depends on how the unit is configured
    (Standard versus Mackie Control mode), and everything downstream forks on
    the answer — see the binding note below.
- **`xtouch_leds.py`** — output tester, the `apc_leds.py` equivalent: button
  LEDs and the encoder LED rings.

Both stay standalone and depend on nothing in the project. Their findings
become a confirmed control map, written into the driver's header the way
`apc.py` carries the APC's.

**Then the driver**, with three things to design:

- **Control naming.** `showfile.parse_pad` hard-codes the APC's geometry — an
  8×8 grid, `t1`–`t8`, `s1`–`s8`, `f1`–`f9` — and computes MIDI note numbers
  directly. The X-Touch needs its own vocabulary for buttons, encoders,
  encoder pushes and its fader, resolved per device.
- **Layers.** `mapping.csv` already has a `shift` column for the APC's second
  layer. The X-Touch's A/B is the same user-facing idea reached by a different
  mechanism — held modifier versus latching hardware switch. One `layer`
  column can serve both: the binding says which layer a control lives on, and
  the device decides how a layer is reached.
- **Encoders as a new binding kind.** `apply_fader(number, value)` assumes an
  absolute 0–127 position, which is why `introduce()` exists — to ask the
  hardware where it is sitting at startup. Relative encoders have no position
  to report: the software owns the value, and invariant 10's startup blindness
  does not apply to them. If the probe finds the encoders absolute, they can
  reuse the fader path unchanged. This is the fork the probe resolves.

`xtouchsim` mirrors `apcsim`, so a show can still be built on a train.

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

Arrives with the X-Touch driver, by finding 3. One real gap to fix alongside
it: **16-bit pan/tilt is not modelled anywhere.** Most moving heads split pan
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
X-Touch's A/B is handled by the device. Generalising to N pages is a
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

## Note on the unmerged cleanup

The branch `claude/watcher-thread-race-hhbp9x` carries the `REVIEW.md` fixes
and is deliberately not merged. One of them matters to this roadmap: it
consolidates the surface constant tables — currently duplicated across
`apc.py`, `virtualapc.py` and `apc_leds.py` — into a single
`surface_constants.py`. The X-Touch work in **3** would otherwise add a fourth
copy. Worth merging that branch before the device work starts, or repeating
the consolidation as part of it.
