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

### 3. Chaser LTP order is lost on restart of a running chaser
`engine.start_chaser` on an already-running chaser replaces its state
(index resets — fine, matches scene re-press semantics) and `_add` moves it
to the end of `active`. But `handle()` for a `chaser` binding in toggle
mode calls `toggle_chaser`, which for a running chaser calls
`stop_chaser`. So far so good. The subtle one: **`solo_chaser` and `solo`
clear level/scale-unaffected `active` but also wipe `running` — including
beat-synced chasers the user meant to keep**. `solo` on a *scene* stopping
every chaser is defensible but surprising; nothing in mapping.csv warns
that a solo scene pad kills chasers. Consider documenting or splitting
"solo among scenes" from "solo everything".

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

### 5. `NullSender.send()` sleeps 22.6ms while holding nothing back
`run_until` in NullSender never calls `send()` (correct), but
`play_scene.py` and any code calling `send()` directly on a NullSender
blocks the caller for a frame time for no reason. Harmless today; a trap
for the next tool that calls `sender.send()` in a loop expecting it to be
cheap. A `pass` with a comment would do.

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

### 7. `introduce()` failure path sets master to 0 even in `--sim`
If apcsim.py isn't running yet when the controller starts, `introduce()`
times out and master goes to 0 — then the simulator's HELLO resync repaints
LEDs but **does not replay fader positions**, so the rig stays dark until
a fader is moved in the simulator. The HELLO handler could re-trigger the
enquiry (`self.link.send(ENQUIRE)`) or apcsim could volunteer an INTRO on
connect. Minor, but it's the first thing a new user of `--sim` hits.

### 8. The OS2L `_beats` deque can drop beats silently
`deque(maxlen=64)` discards the *oldest* on overflow. 64 beats is ~28s at
138bpm, so this only fires if the main loop stalls badly — but the one
thing that stalls it is a big synchronous reload (item under D-14), and the
result would be a beat-synced chaser jumping. Phase-derivation means it
lands correctly afterwards, so severity is low, but a counter of dropped
beats reported once (like the monitor's stats) would make the invisible
visible.

### 9. `tempo.InternalClock.poll()` catch-up guard hides its own reports
The `> period * 4` guard silently re-anchors after a stall (laptop sleep).
Correct behaviour, but it also resets after a *blocking reload*, so a
tapped phase quietly shifts. One `on_status`-style callback or a returned
flag ("re-anchored, phase lost") would let the controller print it.

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

### 12. `check_adapter` + `DmxSender` race
`check_adapter` finds the port, then `DmxSender.__init__` globs again via
`find_port()` only if `port=None` — controller passes nothing, so the port
found in preflight and the port opened can differ if devices change in
between. Cosmetic in practice (both pick `sorted()[0]`), but passing the
checked port into `DmxSender(port=port)` costs one word and removes the
window.

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

### 15. `handle()`'s held-binding capture is correct and subtle
Release uses the binding captured at press (`state["held"]`), so releasing
SHIFT before a flash pad still stops the right target. This is right, and
easy to break in refactoring. Worth a test — it's the kind of correctness
that survives only as long as nobody "simplifies" it.

### 16. Two sources of truth for the surface constant tables
apc.py and virtualapc.py duplicate GRID/TRACK/SCENE/FEEDBACK constants.
They agree today. A `surface_constants.py` both import from would make
drift impossible; alternatively a startup assert comparing them.

### 17. dmxmon reads the patch once at startup
Already documented in conversation, restated for the file: a reload in the
controller does not re-label the monitor. Fine as a known limitation;
the monitor could watch mtimes the way the controller does if it ever
becomes annoying.

---

## D. Small correctness nits

18. controller.py docstring: the usage block got split by a later insert —
    `--check`/`--feedback` lines now sit *below* the tempo-fallback
    paragraph, reading as if they belong to it.
19. `if True:` block in the main loop (beat drain) is a leftover from a
    patch — harmless, should be flattened for readability.
20. `describe_active` calls `chaser_position` twice per chaser in the
    step-all log path in `handle()` (minor, log-path only).
21. `flash_pad`'s grid-pad branch paints colour but never repaints after
    `flash_until` expiry if `eng.dirty` never goes true in between — the
    expiry sets `relayout`, which does handle it. Verified fine; noted so
    the double mechanism (relayout flag vs dirty) is understood as
    intentional.
22. `os2l.BeatClock.stop()` closes zeroconf before joining the thread; if
    the thread is inside `_advertise` teardown this could race. Order:
    set stop event, join, then close zeroconf.
23. `bpm_from_fader` rounds to int; `TapTempo.bpm` rounds to 0.1. The
    display code prints both through the same path, so a tapped 119.9
    and a fader 120 read differently. Cosmetic.

---

## What I would do first

1. ~~Move reload execution onto the main loop (fixes 1, 2, 6 in one change;
   the watcher thread becomes a change-*detector* only).~~ **Done.**
2. ~~Reconcile `eng.levels`/`eng.scales` in `apply_reload` (fixes 4, 10).~~
   **Done.**
3. ~~Wrap `os2l._dispatch` in a protective try/except (fixes 11).~~ **Done.**
4. ~~Add the engine thread-contract docstring (13) while the reasoning is
   fresh.~~ **Done.**

Everything else is optional polish. The core architecture — one transmit
thread, state mutation on the main loop, queues at every thread boundary,
phase-derived beat sync — held up well under reading; the bugs are all at
the seams where later features (watch, faders, reload) were bolted on
without re-checking the threading doctrine.
