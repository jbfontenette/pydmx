"""Akai APC mini mk2 surface wrapper.

The control map and the LED channel table live in surface_constants.py, so
this module and virtualapc.py cannot disagree about them. They are re-exported
here because every call site reaches them through whichever surface module it
holds (`apc_mod.GRID`, `apc_mod.IDLE`) and should not have to know or care
which one that is.

We use brightness to show state: a mapped-but-idle pad sits dim, an active one
goes full. Same colour, so the grid layout stays readable.
"""

import time

import mido

from surface_constants import (              # noqa: F401 -- re-exported
    GRID, TRACK_BUTTONS, SCENE_BUTTONS, SHIFT, FADER_CC,
    SOLID_10, SOLID_25, SOLID_50, SOLID_100, PULSE_4, BLINK_4, BLINK_2,
    OFF, IDLE, FEEDBACK,
)


class APCError(RuntimeError):
    pass


def _find(names, kind):
    for name in names:
        if "apc" in name.lower():
            return name
    raise APCError(f"No APC {kind} port found. Available: "
                   + (", ".join(names) if names else "none")
                   + "\n  Check the USB cable.")


def note_at(row, col):
    """Note number for a grid position. Row 0 is the BOTTOM row."""
    return row * 8 + col


class APC:
    def __init__(self, input_name=None, output_name=None):
        self.input_name = input_name or _find(mido.get_input_names(), "input")
        self.output_name = output_name or _find(mido.get_output_names(), "output")
        self.inp = mido.open_input(self.input_name)
        self.out = mido.open_output(self.output_name)
        # Last (colour, behaviour) actually sent per note. Repainting the
        # whole surface is 80 Note On messages; doing that on every state
        # change floods the output queue, messages get dropped, and pads
        # freeze in stale states. Diffing against this cache turns a repaint
        # into the one or two messages that genuinely changed.
        self._led = {}

    # --- input ------------------------------------------------------------
    def poll(self):
        """Non-blocking. Yields ('press'|'release', note) and ('fader', n, v).

        Non-blocking on purpose: the main loop needs to keep ticking for
        chasers and LED refresh, so it must never sit waiting on MIDI.
        """
        events = []
        for msg in self.inp.iter_pending():
            if msg.type == "note_on" and msg.velocity > 0:
                events.append(("press", msg.note))
            elif msg.type in ("note_off", "note_on"):
                events.append(("release", msg.note))
            elif msg.type == "control_change" and msg.control in FADER_CC:
                events.append(("fader", msg.control - 0x30 + 1, msg.value))
        return events

    # --- output -----------------------------------------------------------
    def pad(self, note, colour, behaviour=SOLID_100, force=False):
        key = (colour, behaviour)
        if not force and self._led.get(note) == key:
            return
        self._led[note] = key
        self.out.send(mido.Message("note_on", channel=behaviour,
                                   note=note, velocity=colour))

    def pad_rgb(self, note, rgb):
        self.pads_rgb([(note, note, rgb)])

    def pads_rgb(self, entries):
        """Set arbitrary 24-bit colours via SysEx, bypassing the palette.

        entries: [(start_pad, end_pad, (r, g, b)), ...] -- a whole range per
        entry, and many entries per message, so the entire grid is one
        transmission rather than 64.

        This exists because the fixed palette cannot express some colours at
        all. A warm yellow needs zero blue and reduced green (#FFC000); the
        nearest palette entry is #FFE126, whose blue channel of 38
        desaturates it into something that reads lighter, not warmer.
        MIDI data bytes are 7-bit, so each 8-bit component splits MSB/LSB.
        """
        data = []
        for start, end, (r, g, b) in entries:
            data += [start, end,
                     r >> 7, r & 0x7F,
                     g >> 7, g & 0x7F,
                     b >> 7, b & 0x7F]
        if not data:
            return
        n = len(data)
        self.out.send(mido.Message(
            "sysex", data=[0x47, 0x7F, 0x4F, 0x24, n >> 7, n & 0x7F] + data))

    def button(self, note, state=1, force=False):
        """Single-colour UI buttons. Always channel 0.

        state 0 off, 1 on, 2 blink. Colour is fixed in hardware: track red,
        scene launch green. SHIFT has no LED and cannot be lit.
        """
        key = ("button", state)
        if not force and self._led.get(note) == key:
            return
        self._led[note] = key
        self.out.send(mido.Message("note_on", channel=0,
                                   note=note, velocity=state))

    def clear(self):
        for note in GRID:
            self.pad(note, OFF, force=True)
        for note in list(TRACK_BUTTONS) + list(SCENE_BUTTONS):
            self.button(note, 0, force=True)

    def refresh(self):
        """Forget cached LED state so the next paint re-sends everything.

        Call after a reload, or any time the device and the cache might have
        drifted apart -- a dropped message leaves the cache claiming a state
        the hardware never reached.
        """
        self._led.clear()

    def introduce(self, timeout=1.0):
        """Send the Introduction message; return the 9 fader positions.

        Solves a real startup problem: nothing tells the host where the
        physical faders are sitting, so without this the master would sit at
        whatever the software assumed until you happened to move it.

        Returns a list of 9 values 0-127, or None if the device does not
        answer within the timeout.
        """
        for _ in self.inp.iter_pending():
            pass                      # discard anything already queued
        # F0 47 7F 4F 60 00 04 <app id> <ver hi> <ver lo> <bugfix> F7
        self.out.send(mido.Message(
            "sysex",
            data=[0x47, 0x7F, 0x4F, 0x60, 0x00, 0x04, 0x00, 0x01, 0x00, 0x00]))

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for msg in self.inp.iter_pending():
                if msg.type != "sysex":
                    continue
                data = list(msg.data)
                # Response is message type 0x61, then the nine fader values.
                if data[:4] == [0x47, 0x7F, 0x4F, 0x61] and len(data) >= 15:
                    return data[6:15]
            time.sleep(0.01)
        return None

    def close(self):
        try:
            self.clear()
        finally:
            self.inp.close()
            self.out.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
