# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

A DMX lighting controller for live use. "Live use" is the whole design
constraint: a crash mid-set means a dark venue, and a subtly wrong colour
means a bad show. Failure modes matter more than features here.

Read `README.md` for the CSV formats, `REVIEW.md` for known bugs, and
`ROADMAP.md` for work not started -- what each planned feature costs
against the code that exists, and which invariants it puts at risk.

## Before you start

```bash
python3 -m unittest discover -s tests -t tests      # 281 tests, ~0.7s
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

`--surface apc|apcsim|xtouch` picks the control surface. A surface is a
module carrying a fixed set of names -- `PADS`, `BUTTONS`, `RINGS`,
`LAYER_AT_START`, `parse_control` and the rest -- and a `poll()` emitting
four event shapes. `tests/test_surface.py` lists the whole contract, so
adding a third device starts from a failing test rather than a surprise.

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
mid-set. Deriving means those cases need no handling at all -- and freeze is
the same trick again: `ChaserState.frozen` skips the update, so releasing a
held chaser re-derives from `pos` and rejoins the music rather than resuming
however many steps late. A snapshot is not a counter.

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

**10. Fail safe on unknown state.** When the surface does not report its
fader positions, master starts at **0**, not 255. An unexpected blackout
costs one gesture; an unexpected full blast in a venue does not.

The X-Touch's unknown LAYER used to be handled the same defensive way --
paint nothing until a press says where we are -- but it does not have to be.
Each layer has its own lamps and the device DISCARDS writes to the one it is
not showing, so `PAINT_HIDDEN_LAYERS` paints every layer every time and the
device keeps the one that matters. Unknown state with no bad outcome does
not need a defensive answer; look for the fact that removes the uncertainty
before settling for the safe-but-worse behaviour.

**11. A control id is layer-independent; the binding key is
`(control, layer)`, and whether a layer falls through to the base one is the
surface's business.** A HELD modifier falls through -- losing every pad for
as long as SHIFT is down would be absurd. A LATCHING layer does not: it is a
page you stay on, and inheriting the other page wherever it is blank fires
the wrong thing. `LAYER_FALLS_THROUGH` says which, and `Show.layer()` must
agree with `binding_for()` or a lamp advertises a binding that does nothing. Which physical numbers a layer uses is the surface's
business alone. The X-Touch sends different notes and CCs per layer and its
driver translates both ways, so `mapping-xtouch.csv` never mentions a layer B
number and moving a binding between layers is a one-column edit. Anything in
`controller.py` that learns a raw MIDI number has broken this.

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
- The X-Touch Mini listens where it speaks, per layer: to light a button you
  send the note that button sends on the layer showing, and the other
  layer's numbers are discarded as they arrive rather than queued. Its layer
  button sends nothing at all, and program change does not switch it, so the
  layer can only be inferred from arriving numbers. Behringer's X-Touch
  Editor documents a different RX map -- LEDs on notes 0-15, program change
  for the layer, separate behaviour and value CCs per ring -- and **none of
  it is true of this unit in Standard mode**. All three were tested.
- The X-Touch's button LEDs are binary: velocity 0 off, every value 1-127
  plain on, measured from off each time. No brightness, no blink. Its
  encoders are absolute *because* it is in Standard mode; MC MODE makes them
  relative and moves every number. Blink is real but MC-only (velocity 1,
  buttons at notes 40-45 and 84-95), which is why `--feedback blink` is done
  in software here: `SOFT_BLINK` on a surface module says which styles the
  main loop animates itself, and the phase is derived from the clock rather
  than counted, for the same reason as invariant 4.
- **Writing an X-Touch ring MOVES that encoder**, not just its lamps.
  Measured: knob at 7, ring set to 110, next click reported 111. There is no
  Introduction message on this device, so its encoder positions cannot be
  READ -- but they can be written, which is what cures the startup jump. A
  `scale` encoder resting at zero would otherwise slam its group from full
  to nothing on first touch. `controller.fader_value` answers with the
  neutral value for an unset level or scale precisely so the first paint
  puts every bound knob where the show already is: `introduce()` inverted.
- **The X-Touch changes its own lamps and rings.** A button lights while held
  and goes dark on release whatever the host set, and a ring follows its knob
  as you turn it. So a diff-based LED cache goes stale on every press and
  every turn, and would then suppress the very repaint that would fix it --
  a scene running all night behind a dark button. `xtouch._restore` re-sends
  the show's value on release, and turning a knob drops that ring's cache
  entry. Not needed on the APC, which never touches its own LEDs.
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
  type that ignores them, documentation in `README.md`, and a test. "Type"
  now includes the surface: `colour` on a device with no colour LEDs is the
  same mistake as `mode` on a fader, and warns the same way.
- Surface constants live in `surface_constants.py` and `xtouch_constants.py`,
  one copy each, and the drivers re-export them. They were duplicated twice
  before -- the APC's idle brightness drifted across three files, and the
  X-Touch's MIDI channel was written down as 0 in a second place and cost a
  whole hardware session testing an LED protocol that was working fine.
- Keep hardware modules free of "pretend" branches. `NullSender` and
  `VirtualAPC` are separate classes precisely so the real ones stay simple.

## When adding a feature

1. Check `REVIEW.md` first — the bug you are about to trip over may be known.
2. Build it standalone before wiring it into `controller.py`. Every subsystem
   here (`dmx`, `apc`, `os2l`) was proven with its own test script first.
3. Add tests for the invariant, not just the happy path.
4. Update `README.md` if it changes the CSV format or the flags.
