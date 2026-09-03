# DMX Controller — Parked Items

Things deliberately deferred, with enough context to pick up cold.

Known bugs and review findings live in `REVIEW.md`, not here. This file is
for hardware, validation and things the code cannot tell you.

The "design decisions to carry into the scene engine" that used to sit here
have all landed: snap-vs-fade is the `mode` column in `profiles.csv`
(CLAUDE.md invariant 1), and named values resolving to the middle of their
range is `Feature.resolve` in `showfile.py`.

## Validation — DONE

**Full-load timing test: PASSED.** A six-hour live show ran without flicker
or dropout, with the full rig on the real cable run and the 120Ω termination
fitted. That was the open question: the Python send loop is not real-time, so
if another process steals CPU, `time.sleep()` overshoots and the next DMX
break can land while the FTDI FIFO is still transmitting the previous frame,
truncating it. Six hours is a long soak, and the `_wire_free_at` guard in
`DmxSender.send()` held.

Still worth confirming for the record: whether Virtual DJ was driving beats
over OS2L for the whole set. The mapping binds a bpm fader and a tap pad as
well, so the set could have run on the internal clock — in which case the
"another process competing for CPU" half of the test is softer than it looks.

If flicker ever appears: drop `DEFAULT_REFRESH_HZ` (dmx.py) to 25, then 20.
Receivers hold their last value indefinitely, so a low refresh rate costs
nothing until fast chases are in play. If that isn't enough, the hardware fix
is an Enttec DMX USB Pro, which does frame timing in firmware.

**Fixture count is not a timing risk.** DMX is broadcast — the same 513 bytes
go out regardless of how many fixtures listen. Adding fixtures is an
electrical question, not a software one.

## Hardware

**120Ω termination resistor: FITTED.** Across pins 2 and 3 at the last
fixture in the chain. Missing termination shows as reflection-induced flicker
that worsens with cable length — easy to misdiagnose as a software timing
problem, so if flicker ever returns, confirm the resistor is still in place
before touching `dmx.py`.

**RS485 unit load limit** is around 32 devices on one line. Beyond that, or
for long runs, a DMX splitter/booster is needed. Not close to it yet.

## Notes from testing so far

- `write()` and `flush()` return well before the frame is on the wire —
  `tcdrain` only drains the kernel buffer, not the FTDI chip's FIFO. Measured
  loop cycle was ~8ms against 22.6ms of actual wire time. Do not trust
  `flush()` as a transmission barrier.
- A 513-byte frame at 250k baud, 8N2 occupies the wire for 22.6ms. That caps
  the theoretical frame rate near 44Hz; 40Hz left too little slack and
  flickered. 30Hz is stable.
- The FTDI latency timer is a *read*-path setting. Irrelevant to this
  transmit-only application. (Chased this early; it was a dead end.)
- Low-level flicker on an intensity channel at value 11 was the fixture's own
  PWM, not the DMX signal. Confirmed by testing the same channels at 255.
