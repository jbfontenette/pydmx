# Code Review — DMX Controller

Reviewed: controller.py, engine.py, dmx.py, apc.py, showfile.py, os2l.py,
tempo.py, and the support tools. No code was changed. Items are ordered by
how likely they are to hurt during an actual gig.

Recurring theme worth naming first: several of these bugs share one root
cause — **the file watcher thread and the reload pad both mutate shared
objects (`show`, `eng`) that the main loop reads mid-iteration.** The
architecture doctrine ("input events mutate state, one thread transmits")
was applied rigorously to the DMX path and not at all to the reload path.
Items 1, 2 and 6 are all instances of this.

**Update: items 1, 2 and 6 are FIXED.** Reload now runs only on the main
loop; the watcher thread detects and nothing else. What is left of this
theme is item 4, which is about *what* a reload reconciles rather than
*where* it runs.

---

## A. Bugs that can bite mid-set

### 1. The `--watch` thread races the main loop  (FIXED)
`watcher()` ran `apply_reload(show, eng)` on its own thread every 0.5s
while the main loop is iterating the same structures. Concretely:

- `build_leds` iterates `show.layer(shift)` while reload replaces
  `show.bindings` → possible `RuntimeError: dict changed size during
  iteration`, which kills the main loop. The DMX thread keeps running, so
  the rig freezes at its last look with no input — the worst failure mode.
- `engine.output()` iterates `eng.active` while reload filters it.
- `eng.running` values are re-pointed mid-`tick()`.

The reload *pad* was safe (it runs on the main loop); only `--watch` was
dangerous.

**Fix as applied:** `controller.watch_files()` compares successive
`Show.stamps()` and sets a `threading.Event`; the main loop drains that flag
between input and output and calls `do_reload()` — now the single call site
of `apply_reload`, shared with the reload pad. The watcher is passed only the
show, the stop event and the request event, so it cannot reach the engine
even by accident. `Show.stamps()` is documented as the one method safe to
call off-thread.

The watcher keeps its own copy of the stamps rather than calling
`changed_on_disk()`, which compares against the last *successful* reload:
a file that will not parse would otherwise re-request a doomed parse on the
main loop twice a second. Each save is now reported once, and the save that
fixes the typo is reported like any other.

Two silent defects in the old watcher path went with it: it never called
`surface.refresh()` (so the LED diff cache still described the old layout
after a watched edit) and never printed `show.warnings`. Sharing the pad's
code path fixed both.

### 2. `flash_pad` writes to the surface from the watcher path  (FIXED)
Related: `reload_action` → `flash_pad` → `surface.pad()` could run on the
watcher thread while the main loop was also mid-`build_leds`. mido output
ports are not documented thread-safe, and the LED diff cache (`_led`) is a
plain dict written from two threads. Worst case is a corrupt cache leaving
pads stuck — the exact symptom the cache was built to fix. Dissolved by the
#1 fix: with reload on the main loop there is only ever one thread painting.
`flash_pad` is now reached only from `do_reload(note=...)`, and a
watcher-triggered reload passes no note, so it flashes nothing.

### 3. `solo` stops chasers, and nothing said so  (DOCUMENTED — still open)
The original heading here was about chaser LTP order, which turned out to be
fine: `start_chaser` on a running chaser resets its state and `_add` moves it
to the end of `active`, and toggle mode stops rather than restarts it. The
real finding is one line further down.

**`solo` and `solo_chaser` both call `eng.clear()`, which wipes `running` as
well as `active`** — so a solo *scene* pad silently stops every chaser,
beat-synced ones included. Defensible (solo means "the only live source",
literally) but easy to meet by accident mid-set, and nothing in
`mapping.csv` or the README said it.

**Decision: keep the behaviour, document it.** Changing the scoping would
alter what existing solo pads do the next time they are played, which is a
worse trade for a rig in use than one documented surprise. The note now
appears in `README.md` under `mode` and in the `show/mapping.csv` header,
and `TestSoloScope` in `tests/test_engine.py` pins it so the documentation
cannot quietly stop being true.

Recorded while confirming it, and now documented alongside: **`flash` never
stops anything but its own target.** Its release does stop that target
whoever started it, so flashing a chaser already running from another pad
stops it on release (`TestFlashRelease`).

**Left open deliberately.** If this is revisited, the two alternatives are:

- *Solo within its own kind* — a scene solo clears scenes only, a chaser
  solo clears chasers only, `clear` stays the way to drop everything. The
  most intuitive reading; the cost is that existing solo pads change
  behaviour under the user's fingers.
- *A second mode* — `solo` keeps meaning everything, a new `solo_scenes`
  gives the scoped version per pad. No change to existing shows, at the
  price of new CSV vocabulary to parse, warn about, document and test.

### 4. `apply_reload` does not reconcile fader channel bindings  (FIXED)
Reload swapped `show.faders` (new channel lists resolved against the new
patch), but `eng.levels` and `eng.scales` still held the **old** channel
tuples captured at the last fader move. Re-patch a fixture and reload, and
a level fader kept driving the *old* address until the fader was physically
moved. This is the same class of bug as the stale `st.chaser` reference
that reload already fixes for chasers.

**Fix as applied:** `apply_reload` now rebuilds both dicts from
`show.faders` after the chaser re-pointing, in the same style. The stored
value is the fader's physical position (both kinds arrive through the same
conversion in `apply_fader`), so it carries across a change of binding: a
fader re-typed from `level` to `scale` keeps its position and only changes
job. Entries whose fader is gone from `mapping.csv`, re-typed to
`master`/`bpm`, or dropped by `_level_channels` because its glob no longer
matches are dropped, and the reload message names them
(`faders dropped: f1`) beside the existing `dropped active:`.

Known edge, decided deliberately: `master` is **not** reconciled. It is one
scalar with no per-fader memory, so a fader newly typed `master` has no
unambiguous claim on it, and a master that moves on its own during a reload
is the surprise item 10 of the invariants list exists to prevent. It keeps
its value until a fader is moved.

### 5. `NullSender.send()` sleeps 22.6ms while holding nothing back  (FIXED)
`run_until` never calls it (correct), but any tool driving the sender by
hand was blocked for a frame time for nothing. Now a `pass` with a comment
saying why: there is no wire to keep clear, and a caller that wants a
realistic frame rate should sleep for it visibly.

One correction to the original note: `play_scene.py` was named as a caller
and is not one — it only uses `apply()` and `run_until`. The only direct
`send()` callers are in `dmx_cycle.py`, against a real adapter, which is
why nothing depended on the pacing.

### 6. `Show.reload()` mutates in place; readers see mixed generations  (MOOT)
`reload()` is atomic with respect to *parse failure* (good) but not with
respect to *readers*: it rebinds `self.profiles`, `self.patch`,
`self.scenes`… in one tuple assignment, which is still several stores. A
reader on another thread could see new patch with old scenes. With #1 fixed
there is no such reader — the only thread that touches parsed show objects is
the main loop — so no code change was made here. Noted so nobody "fixes" #1
by adding locks around each field instead.

---

## B. Robustness gaps (won't crash, will confuse)

### 7. `introduce()` failure path sets master to 0 even in `--sim`  (FIXED)
If apcsim.py wasn't running yet when the controller started, `introduce()`
timed out and master went to 0 — correct, per invariant 10 — but the
simulator's HELLO resync then repainted LEDs **without** replaying fader
positions, so the rig stayed dark until a fader was physically moved.

**Fix as applied**, entirely controller-side, since apcsim already answers
`ENQUIRE` at any time: `virtualapc.poll()` sends an `ENQUIRE` when it sees a
HELLO, and turns an `INTRO` arriving outside `introduce()` into ordinary
`("fader", n, value)` events. `handle()` already routes those through
`apply_fader`, so the positions arrive by the same path a physical move
takes and no new machinery exists. `introduce()` discards `poll()`'s return
value, so the startup path cannot double-apply.

Verified by starting a real controller with no simulator, letting the
Introduction time out, then announcing a simulator with HELLO: the
controller asks for the positions, master goes 0 → 255, and a pad press
lights channels that were dark before. Confirmed against the pre-fix code
that the same sequence leaves the rig at nothing.

### 8. The OS2L `_beats` deque can drop beats silently  (FIXED)
`deque(maxlen=64)` discards the *oldest* on overflow. 64 beats is ~28s at
138bpm, so this only fires if the main loop stalls badly — but the one
thing that stalls it is a big synchronous reload (item under D-14), and the
result would be a beat-synced chaser jumping. Phase-derivation means it
lands correctly afterwards, so severity is low, but a counter of dropped
beats reported once (like the monitor's stats) would make the invisible
visible.

**Fix as applied:** `BeatClock.dropped_beats` counts every overflow and the
first one is reported through `on_status`, once, in the same shape as the
malformed-message report. Deliberately still a report rather than a bigger
queue: if this fires, something stalled the main loop for ~28s and *that* is
the bug — a deeper queue would only hide it for longer.

### 9. `tempo.InternalClock.poll()` catch-up guard hides its own reports  (FIXED)
The `> period * 4` re-anchor is correct — a burst of catch-up beats would
race a chaser through several steps — but it was silent, so a tapped downbeat
could move without a word. `InternalClock` now takes an `on_status` callback,
the same pattern `DmxSender` and `BeatClock` use, counts re-anchors, and the
controller prints them alongside the other clock messages.

Verified on a live `--sim` controller by arming the bpm fader and stopping
the process with SIGSTOP for five seconds:

    [clock: re-anchored after a 5.0s stall -- phase shifted, re-tap if it
     has drifted]

Matters more here than the original note suggested: this show runs 34
beat-synced chasers, and pos keeps counting through a re-anchor, so nothing
jumps a step — what moves is the phase against the music, which only an ear
can catch. Now the terminal says it happened.

### 10. Fader 1-step diff suppression can strand the last position  (FIXED)
`set_level`/`set_scale` skipped when the value was unchanged — but they
compared only the value, not the channels. After a reload changed a fader's
channel list (#4), a fader at the same physical value would *never* update
the new channels because the value-equality check short-circuited. Both now
diff on `(channels, value)`. #4 means the engine no longer depends on this,
but the guard is one line and stops the bug arriving through a future
caller. A genuine no-op — same channels, same value — is still suppressed,
which is what keeps a 127-message sweep cheap.

### 11. `os2l._dispatch` trusts `pos` fits in an int forever  (FIXED)
`strength` was parsed with `float(strength)` inside a try-less path, so a
malformed `"strength": "loud"` from a future VDJ build killed the listener
thread with ValueError — silently, since the accept loop went with it and
`connected` stayed True. `bpm` had the same exposure, and a bare JSON value
(`_Stream` decodes any, not only objects) reached `msg.get` and raised
AttributeError.

**Fix as applied:** decoding moved to `_decode`, and `_dispatch` is now a
guard around it that counts the message and reports it through `on_status`.
Clock state is only touched once the whole beat is built, so a message that
fails halfway leaves the tempo where it was. Each distinct fault is reported
once — VirtualDJ sends two messages a second, and if one is malformed the
rest usually are — but a *different* fault reports again, so a second
problem is not hidden by the first. `bad_messages` counts them all.

Verified over a real socket: with a good/bad/good sequence the bad ones are
dropped with a reason, the good beats still arrive, the thread stays alive
and the tempo holds. Before the change the same sequence killed the thread
and the next write got a broken pipe.

### 12. `check_adapter` + `DmxSender` race  (FIXED)
`check_adapter` found the port, then `DmxSender.__init__` globbed again
because the controller passed nothing, so the port checked and the port
opened could differ if devices changed in between. The controller now
passes what preflight found: `dmx.DmxSender(port=port, ...)`.

---

## C. Design observations (sound, but worth writing down)

### 13. The engine's thread-safety contract is implicit  (FIXED)
Engine has no locks *by design* — everything mutates on the main loop.
That contract was stated in os2l.py's docstring but not in engine.py itself,
which is where the next contributor looks. engine.py now carries a THREADING
section saying it plainly: every method is called from the main loop and only
from the main loop, every other thread hands its work over instead (MIDI and
the simulator queue events, OS2L queues beats, the watcher sets a flag, the
DMX thread transmits what `output()` already returned), and the fix for a
cross-thread call is to move the call, not to add a lock — which `output()`
would have to take on every frame.

### 14. Reload is synchronous in the main loop
Measured: ~50ms at 300 scenes, ~490ms at 2000. During that, beats queue
(#8), MIDI queues, DMX holds last frame (safe). Acceptable at your scale;
becomes user-visible around 1000+ scenes. If it ever matters, parse on a
worker thread and hand the finished object set to the main loop for the
swap — the `_parse()` structure already supports exactly that.

### 15. `handle()`'s held-binding capture is correct and subtle  (TESTED)
Release uses the binding captured at press (`state["held"]`), so releasing
SHIFT before a flash pad still stops the right target. This is right, and
easy to break in refactoring.

`TestHeldBindingCapture` now pins it: press a flash pad on the SHIFT layer,
release SHIFT, release the pad, and the scene captured at press is the one
that stops. Confirmed the test fails when the release path is "simplified"
into a fresh `binding_for()` lookup — which strands the held scene on, with
no pad left that turns it off.

### 16. Two sources of truth for the surface constant tables  (FIXED)
apc.py and virtualapc.py duplicated GRID/TRACK/SCENE/FEEDBACK — and it was
three copies, not two, counting apc_leds.py. They did **not** stay in
agreement: moving idle from 10% to 25% took three edits and the third was
missed, leaving the contrast test previewing a gap the controller no longer
produced.

Now `surface_constants.py`, which depends on nothing, holds the table; apc.py
and virtualapc.py import the names explicitly and re-export them, so
`apc_mod.GRID` and `apc_mod.IDLE` still work for every call site. apc_leds.py
keeps its own sixteen-entry protocol table on purpose — listing every
behaviour is that tool's job — but takes `IDLE` from the shared file and
derives its printed label from it.

`TestSurfaceConstants` checks both surfaces against the shared table name for
name (the real one where mido is installed), which is the test that would
have caught the original drift.

### 17. dmxmon reads the patch once at startup
Already documented in conversation, restated for the file: a reload in the
controller does not re-label the monitor. Fine as a known limitation;
the monitor could watch mtimes the way the controller does if it ever
becomes annoying.

---

## D. Small correctness nits

18. **(FIXED)** controller.py docstring: the usage block had been split by a
    later insert, leaving `--check`/`--feedback` below the tempo-fallback
    paragraph as if they belonged to it. The flags are one block again, with
    the tempo paragraph after them — and `--watch`, which was never listed
    at all, is now in it.
19. **(FIXED)** `if True:` in the main loop (beat drain), a leftover from a
    patch. Flattened.
20. **(NOT REPRODUCIBLE)** The claim was that `describe_active` calls
    `chaser_position` twice per chaser in the step-all log path.
    `chaser_position` has exactly one call site in the whole of
    controller.py — inside `describe_active`, reached once per chaser per
    log line. Either this was fixed by an earlier change or the reading was
    wrong. No code change.
21. `flash_pad`'s grid-pad branch paints colour but never repaints after
    `flash_until` expiry if `eng.dirty` never goes true in between — the
    expiry sets `relayout`, which does handle it. Verified fine; noted so
    the double mechanism (relayout flag vs dirty) is understood as
    intentional.
22. **(FIXED)** `os2l.BeatClock.stop()` closed zeroconf before joining the
    thread. Now: set the stop event, join, then close.
23. **(NOT REPRODUCIBLE)** The claim was that a tapped 119.9 and a fader 120
    "read differently". They do not: `set_bpm` stores `float(bpm)`, so the
    fader path prints `120.0` and the tap path `119.8` — both one decimal,
    through the same f-string. What differs is input resolution (the fader
    has 127 steps across 60-180 BPM, so it can only land on whole numbers;
    a tap can land anywhere), and that is inherent, not a display bug. No
    code change.

---

## What I would do first

1. ~~Move reload execution onto the main loop (fixes 1, 2, 6 in one change;
   the watcher thread becomes a change-*detector* only).~~ **Done.**
2. ~~Reconcile `eng.levels`/`eng.scales` in `apply_reload` (fixes 4, 10).~~
   **Done.**
3. ~~Wrap `os2l._dispatch` in a protective try/except (fixes 11).~~ **Done.**
4. ~~Add the engine thread-contract docstring (13) while the reasoning is
   fresh.~~ **Done.**

Everything else was optional polish, and has since been done too. The core
architecture — one transmit thread, state mutation on the main loop, queues
at every thread boundary, phase-derived beat sync — held up well under
reading; the bugs were all at the seams where later features (watch, faders,
reload) were bolted on without re-checking the threading doctrine.

## Where this stands

Every actionable item is closed. What is left is deliberate:

- **3** — `solo` stopping chasers is documented and pinned, not changed. The
  decision is open to revisit; the two alternatives are written out under
  the item. The show's 32 `solo` chaser pads are an argument for leaving it.
- **14** — reload is synchronous in the main loop. Measured ~50ms at 300
  scenes; this show has 201, so it is not user-visible. The `_parse()`
  structure already supports moving it to a worker if it ever is.
- **17** — dmxmon reads the patch once at startup, so a reload does not
  re-label it. Known limitation, not worth the mtime watching it would need.
- **20, 23** — checked and not reproducible. See the notes on each.
- **21** — verified correct when the review was written; still is.

The live validation that matters: a six-hour show with the full rig, real
cable run, 120Ω termination, and Virtual DJ driving the beat throughout, with
no flicker and no dropout.
