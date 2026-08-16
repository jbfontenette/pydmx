# DMX Controller — Parked Items

Things deliberately deferred, with enough context to pick up cold.

## Validation still owed

**Full-load timing test.** So far only validated with one fixture on a short
cable, with nothing else running. Need to confirm the output holds up with:

- all fixtures connected on the real cable run
- Virtual DJ running simultaneously

Why it matters: the Python send loop is not real-time. If another process
steals CPU, `time.sleep()` overshoots and the next DMX break can land while
the FTDI FIFO is still transmitting the previous frame — which truncates it
and shows as flicker. The `_wire_free_at` guard in `DmxSender.send()` is the
defence; this test is what proves it works under contention.

What to watch: reported fps holding near target, and no visible flicker on a
fixture held at a static level.

If it fails: drop `REFRESH_HZ` to 25, then 20. Receivers hold their last
value indefinitely, so a low refresh rate costs nothing until fast chases are
in play. If that isn't enough, the hardware fix is an Enttec DMX USB Pro,
which does frame timing in firmware instead of on the host.

**Fixture count is not a timing risk.** DMX is broadcast — the same 513 bytes
go out regardless of how many fixtures listen. Adding fixtures is an
electrical question, not a software one.

## Hardware

**120Ω termination resistor**, across pins 2 and 3 at the *last* fixture in
the chain. Currently missing. Getting away with it on one fixture and a short
cable; will not get away with it on a longer run with several fixtures.
Symptom of missing termination is reflection-induced flicker that gets worse
with cable length — easy to misdiagnose as a software timing problem.

**RS485 unit load limit** is around 32 devices on one line. Beyond that, or
for long runs, a DMX splitter/booster is needed.

## Design decisions to carry into the scene engine

**Function channels must snap, not fade.** Colour wheel, gobo, strobe and
mode channels are selectors — the value is an index into the fixture's
lookup table, not a level. Crossfading one sweeps through every intervening
colour or mode. Intensity and RGB channels fade; function channels jump.

Implication: the scene CSV needs a per-channel flag for whether a channel is
fade-able. Decide the column name when building the loader.

**Use mid-range values for function channels.** Fixture charts specify ranges
(e.g. 8–15 = red). Target the middle of the range, not an edge.

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
