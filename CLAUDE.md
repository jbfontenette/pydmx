# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

A DMX lighting controller for live use. "Live use" is the whole design
constraint: a crash mid-set means a dark venue, and a subtly wrong colour
means a bad show. Failure modes matter more than features here.

Read `README.md` for the CSV formats and `REVIEW.md` for known bugs.

## Before you start

```bash
python3 -m unittest discover -s tests -t tests      # 119 tests, ~0.3s
python3 controller.py --check                        # validate CSVs
```

Both run with no hardware and no third-party packages. **Run the tests before
and after every change.** They exist because most of the invariants below are
invisible in the code and easy to "simplify" away.

To exercise the full system with no hardware, three terminals:

```bash
python3 controller.py --sim --no-dmx --monitor
python3 apcsim.py
python3 dmxmon.py
```

## Invariants — do not break these

**1. `mode` (fade/snap) governs both fading and merging.**
A snap channel is a *selector*: the value indexes the fixture's lookup table.
Fading one sweeps through every colour in between; scaling colour 42 gives
colour 21, a different colour rather than a dimmer one. So snap channels are
never faded, never scaled by the master, never driven by level or scale
faders, and merge LTP rather than HTP. If a change makes a number "just get
multiplied", check whether it can reach a snap channel.

**2. One thread transmits; everything else mutates state.**
`DmxSender.send()` is called from exactly one thread. MIDI events, beats and
timers only mutate engine state. Never write to the serial port from an input
handler — that is what causes flicker under CPU load.

**3. The engine has no locks, by design.**
All engine mutation happens on the main loop. Input sources (MIDI, OS2L,
internal clock) queue their events and the main loop drains them with
`poll()`. Do not add locks; keep the mutation on one thread. Reload obeys the
same rule: `--watch` runs `controller.watch_files()`, which only compares
mtimes and sets an event, and the main loop performs the reload. Its only
contact with the show is `Show.stamps()` — the one method safe to call from
another thread. If a reload ever needs to get faster, parse on a worker and
hand the finished objects to the main loop for the swap; do not lock.

**4. Beat-synced chaser position is derived, never counted.**
`chaser.step_at(pos)` maps the track's beat number to a step. A counter
drifts out of phase at every pause, seek and deck change and you only notice
mid-set. Deriving means those cases need no handling at all.

**5. `pos` can be negative.**
Virtual DJ sends negative beat positions before the beat-grid origin.
Python's `%` returns non-negative results, which is why `pos % 4` and
`pos % cycle` are correct. **Never "fix" this with `abs()`** — it would
invert the bar phase for the whole intro of every track.

**6. Grid row 0 is the BOTTOM row.** `note = row * 8 + col`. Terminals draw
top-down, so any visual rendering must iterate rows in reverse.

**7. Offsets in `profiles.csv` are 1-based.** `channel = address + offset − 1`.
This matches the fixture manual's own numbering so charts can be transcribed
without arithmetic.

**8. Scenes are sparse and merge HTP.** A scene lists only what it touches.
Consequently a scene of zeros cannot turn anything off — going dark means
deactivating sources. Do not "fix" this by making zero special.

**9. Reload is atomic.** `Show.reload()` parses everything into fresh objects
and only swaps them in if all files parsed. A typo mid-set must leave the
running show untouched. Anything added to the show files must be parsed
inside `_parse()`, never assigned directly.

**10. Fail safe on unknown state.** When the APC does not report its fader
positions, master starts at **0**, not 255. An unexpected blackout costs one
gesture; an unexpected full blast in a venue does not.

## Error policy

The distinction is deliberate and worth preserving:

- **Structural errors are fatal** — an unparseable pad spec, a bad number, an
  unknown profile. The file is not understood and guessing is worse than
  stopping.
- **Name drift warns and skips** — a pad pointing at a deleted scene, a glob
  matching a fixture that lacks the feature. These are ordinary drift between
  files edited separately, and should cost one pad, not the whole rig.

A warning that repeats per-line should be collected and reported once;
repeating a full list of valid names on every offending line buries the
summary under its own errors.

## Hard-won details

These were established by testing against real hardware. Changing them
without re-testing on hardware will regress things that took a while to find.

- A 513-byte DMX frame at 250k baud 8N2 occupies the wire for **22.6ms**.
  `write()` and `flush()` return well before that — `tcdrain` empties the
  kernel buffer, not the FTDI chip's FIFO. Asserting the next break early
  truncates the frame and shows as flicker. `_wire_free_at` is the guard.
  30Hz is the tested-stable rate.
- The FTDI latency timer is a *read*-path setting and irrelevant here.
- APC LED state is set by Note On where the **channel** encodes behaviour and
  the **velocity** encodes the palette colour.
- Repainting the whole APC surface is 80 MIDI messages. Doing that on every
  state change floods the output queue and freezes pads. `APC._led` diffs
  against the last sent state; `refresh()` drops the cache when the two might
  have diverged.
- The OS2L spec defines its messages but **not how they are delimited** on
  the stream. `os2l._Stream` decodes by object boundary. Do not switch to
  splitting on newlines.
- Virtual DJ will not connect until a DMX pad is pressed in it once per
  session. DNS-SD advertisement does not avoid this; it was tested.
- The APC's fixed palette cannot express some colours. `yellow_warm` and
  `yellow` are deliberately swapped relative to their nominal hex values
  because that is what reads correctly on the hardware; the two whites are
  knowingly near-identical and there is no better palette entry. See the
  docstring in `colours.py` before changing any of them.

## Style

- Comments explain **why**, especially where the code looks odd — most of the
  odd-looking code here is odd for a reason discovered by testing.
- Prefer failing loudly at load time over failing silently at showtime.
- New CSV columns need: parsing in `showfile.py`, a warning when set on a
  type that ignores them, documentation in `README.md`, and a test.
- Keep hardware modules free of "pretend" branches. `NullSender` and
  `VirtualAPC` are separate classes precisely so the real ones stay simple.

## When adding a feature

1. Check `REVIEW.md` first — the bug you are about to trip over may be known.
2. Build it standalone before wiring it into `controller.py`. Every subsystem
   here (`dmx`, `apc`, `os2l`) was proven with its own test script first.
3. Add tests for the invariant, not just the happy path.
4. Update `README.md` if it changes the CSV format or the flags.
