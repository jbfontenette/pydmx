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
event, exactly like moving a fader — and the probe confirmed the X-Touch's are
**absolute**, so they reuse `apply_fader` rather than needing a binding kind of
their own. Manual pan/tilt control is therefore reachable as soon as the driver
exists, with no continuous rendering and no new engine concept. Only
*automated* movement patterns need finding 1 solved.

**4. A digital BPM readout has nowhere to go but the screen.** Neither the
APC nor the X-Touch Mini has a numeric display. The X-Touch's LED rings can be
driven by the controller — measured, not assumed — but thirteen segments
cannot spell a number, so they still do not answer this.
The controller already prints `bpm 120.0` after a settle; the problem is that
it scrolls away. The requirement is a *persistent* readout, and that is a
screen decision — an in-place status line, or the separate-process pattern
`monitor.py` and `dmxmon.py` already establish.

---

## Priority

| Now | Medium | Later |
|---|---|---|
| **3** X-Touch Mini: driver (swap model, A/B, encoder push) — probe done | **2** beat fractions | **1b** crossfade |
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

### 3. X-Touch Mini — probe done, driver next

The end state is APC alone, X-Touch alone, or both together. **Both together
is deferred**; one surface at a time is the target for now, which the code
already does for `apc` versus `virtualapc`. Extending that to four choices —
`apc`, `apcsim`, `xtouch`, `xtouchsim` — is a flag change, not an
architecture change. Two devices at once would mean merging event sources and
keeping LED state per device, which is a different and much larger job.

**The standalone probe is finished**, on branch `claude/xtouch-probe`:
`xtouch_dump.py` for input, `xtouch_leds.py` for output, mirroring
`apc_dump.py` and `apc_leds.py` and depending on nothing in the project.
Every control was walked on the hardware, both layers, and the map below is
measured rather than read off a specification. It lives in `xtouch_dump.py`'s
header, the way `apc.py` carries the APC's.

**The whole surface is one sentence:** to drive a control, send the number
that control *sends*, on the layer currently showing. The other layer's
numbers are discarded — dropped at the moment they arrive, not queued.

| | layer A | layer B |
|---|---|---|
| buttons, in and out | notes 8–23 | notes 32–47 |
| encoders and their LED rings | CC 1–8 | CC 11–18 |
| encoder push | notes 0–7 | notes 24–31 |
| fader | CC 9 | CC 10 |

Everything on MIDI channel 10. Note the fader irregularity: layer B's is
CC 10, not the CC 18 a uniform offset would predict — that guess was made and
was wrong, so the block is not a pattern to extend.

**What the probe settled, and what each answer costs or saves:**

- **Encoders are absolute**, not relative — the unit is in Standard mode, and
  MC MODE is what would make them send deltas. This was the fork the probe
  existed to resolve and it fell the cheap way: `apply_fader(number, value)`
  already takes an absolute 0–127 position, so **encoders need no new binding
  kind**. The device also remembers a separate position per layer, so there
  are effectively sixteen absolute encoders, not eight.
- **Layers are handled inside the device** for input, as hoped: the layer
  button sends nothing at all and the device simply starts sending the other
  set of numbers. No page state machine. But **output is not free** — see
  below.
- **Button LEDs are binary.** Velocity 0 is off, every value 1–127 is plain
  on. No brightness steps, no blink, on any velocity. That costs something
  real: on the APC an idle-but-bound pad glows at 25% so you can see where
  your bindings live before pressing anything, and `--feedback` offers pulse
  and blink for active ones. **On the X-Touch a bound button looks exactly
  like an unbound one**, and the `FEEDBACK` table collapses to on/off. Worth
  knowing before laying a show out on this surface.
- **Ring values can be driven** by the controller, and the device keeps ring
  state per layer. But the ring's **display style** — travelling dot, fill,
  fan — is a device-side setting per encoder per layer, made in X-Touch
  Editor and not reachable over MIDI. The controller picks the value; the
  editor picks how it is drawn. A pan/tilt encoder wanting a single dot has
  to be configured on the device beforehand, which is a setup step to
  document rather than code to write.
- **The device holds two LED surfaces and shows one.** State survives a layer
  switch, so the paint policy is: keep desired and delivered state per layer,
  write only to the layer showing, and flush the difference when a layer
  appears — at most sixteen notes, usually none. The obvious alternative,
  `apc.py`'s `refresh()` (drop the cache, repaint everything), would send
  sixteen redundant messages per switch for nothing.

**A warning worth carrying forward.** Behringer's own X-Touch Editor
documents an RX map — LEDs on notes 0–15, program change to select the layer,
a separate behaviour CC and value CC per ring. **None of it is true of this
unit in Standard mode.** All three were tested and none worked, and believing
the editor for one commit put two correct hardware measurements in doubt. The
device is the source; the editor is not.

**Then the driver**, with three things to design. The list is shorter than it
was, because the probe removed one:

- **Control naming.** `showfile.parse_pad` hard-codes the APC's geometry — an
  8×8 grid, `t1`–`t8`, `s1`–`s8`, `f1`–`f9` — and computes MIDI note numbers
  directly. The X-Touch needs its own vocabulary for buttons, encoders,
  encoder pushes and its fader, resolved per device.
- **Layers.** `mapping.csv` already has a `shift` column for the APC's second
  layer. The X-Touch's A/B is the same user-facing idea reached by a different
  mechanism — held modifier versus latching hardware switch. One `layer`
  column can serve both: the binding says which layer a control lives on, and
  the device decides how a layer is reached.
- **Layer tracking, which is the one genuinely new thing.** The device never
  announces a switch and program change does not cause one, so the active
  layer can only be inferred from arriving notes: below 24 is layer A, 24 and
  above is layer B. At startup it is unknown, and by invariant 10 the right
  response is to paint nothing until the first press says where we are —
  guessing A would light a surface that may not be showing. After a switch
  made without touching anything the surface is not dark; it shows whatever
  that layer was last told, which may be stale until the first press.
- ~~Encoders as a new binding kind~~ — **not needed.** Absolute encoders
  reuse the fader path unchanged, `introduce()` included.

`xtouchsim` mirrors `apcsim`, so a show can still be built on a train.

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

## Note on the unmerged branches

`main` is what ran a six-hour show, and three branches sit off it unmerged by
deliberate choice.

**`claude/watcher-thread-race-hhbp9x`** carries the `REVIEW.md` fixes. One of
them matters to this roadmap: it consolidates the surface constant tables —
currently duplicated across `apc.py`, `virtualapc.py` and `apc_leds.py` — into
a single `surface_constants.py`. The X-Touch driver would otherwise add a
fourth copy. Worth merging before the device work starts, or repeating the
consolidation as part of it. That duplication is not hypothetical: the
X-Touch LED probe wrote the MIDI channel down a second time, as 0 instead of
10, and a whole hardware session was spent testing an LED protocol that was
working fine.

**`claude/xtouch-probe`** carries `xtouch_dump.py`, `xtouch_leds.py` and their
tests — the measured control map that section **3** rests on. It touches
nothing else in the project, so it is safe to merge on its own whenever the
driver work starts.

**`claude/roadmap-analysis`** is this file.
