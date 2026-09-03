"""A stand-in for the physical APC mini mk2, talking to apcsim.py over UDP.

Deliberately mirrors apc.APC method for method -- poll, pad, button, clear,
refresh, introduce, close, plus input_name/output_name -- so controller.py
constructs one or the other and nothing else changes. apc.py is untouched:
the class that drives real hardware should not grow a "pretend" branch.

The LED diff cache is kept here too, for the same reason as on the real
device: a full repaint is 80 updates, and sending those on every state
change is wasteful whether the destination is MIDI or a socket.
"""

import time

import simlink

# The same table apc.py re-exports, from the same file -- so a caller holding
# this module sees exactly what it would see holding the real one. These used
# to be a hand-copied mirror, which is how the idle brightness drifted.
from surface_constants import (              # noqa: F401 -- re-exported
    GRID, TRACK_BUTTONS, SCENE_BUTTONS, SHIFT, FADER_CC,
    SOLID_10, SOLID_25, SOLID_50, SOLID_100, PULSE_4, BLINK_4, BLINK_2,
    OFF, IDLE, FEEDBACK,
)


class VirtualAPC:
    def __init__(self, led_addr=None, event_addr=None):
        self.led_addr = led_addr or simlink.LED_ADDR
        self.event_addr = event_addr or simlink.EVENT_ADDR
        self.input_name = f"simulator (events on {self.event_addr[1]})"
        self.output_name = f"simulator (LEDs to {self.led_addr[1]})"
        self.link = simlink.Endpoint(self.event_addr, self.led_addr)
        self._led = {}
        self._pending_faders = None

    # --- input ------------------------------------------------------------
    def poll(self):
        events = []
        for payload in self.link.drain():
            kind = payload[0]
            if kind == simlink.PRESS and len(payload) >= 2:
                events.append(("press", payload[1]))
            elif kind == simlink.RELEASE and len(payload) >= 2:
                events.append(("release", payload[1]))
            elif kind == simlink.FADER and len(payload) >= 3:
                events.append(("fader", payload[1], payload[2]))
            elif kind == simlink.INTRO and len(payload) >= 10:
                self._pending_faders = list(payload[1:10])
            elif kind == simlink.HELLO:
                self._resend_all()
        return events

    def _resend_all(self):
        """Replay every LED state we believe the surface should be in.

        Answers a simulator HELLO. The diff cache is exactly the record of
        what we think was delivered, so replaying it is the whole fix -- no
        cooperation needed from the controller, which cannot tell a real
        device from a simulated one and should not have to.
        """
        updates = []
        for note, key in self._led.items():
            if key[0] == "button":
                updates.append((note, key[1], 0))
            else:
                updates.append((note, key[0], key[1]))
        for i in range(0, len(updates), 200):     # keep inside one datagram
            self.link.send(simlink.encode_leds(updates[i:i + 200]))

    # --- output -----------------------------------------------------------
    def pad(self, note, colour, behaviour=SOLID_100, force=False):
        key = (colour, behaviour)
        if not force and self._led.get(note) == key:
            return
        self._led[note] = key
        self.link.send(simlink.encode_leds([(note, colour, behaviour)]))

    def button(self, note, state=1, force=False):
        key = ("button", state)
        if not force and self._led.get(note) == key:
            return
        self._led[note] = key
        self.link.send(simlink.encode_leds([(note, state, 0)]))

    def pads_rgb(self, entries):
        """No-op. The SysEx RGB path is unverified on real hardware, so
        simulating it would invite building on something that may not work."""
        return

    def clear(self):
        self._led.clear()
        self.link.send(bytes([simlink.CLEAR]))

    def refresh(self):
        self._led.clear()

    def introduce(self, timeout=1.5):
        """Ask the simulator where its faders are sitting.

        Same contract as the real device's Introduction message: nine values
        0-127, or None on no answer. The controller then treats a simulator
        exactly as it treats hardware.
        """
        self._pending_faders = None
        self.link.send(bytes([simlink.ENQUIRE]))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.poll()
            if self._pending_faders is not None:
                return self._pending_faders
            time.sleep(0.02)
        return None

    def close(self):
        try:
            self.clear()
        finally:
            self.link.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# controller.py does `apc_mod.APC()`. Aliasing here means the module itself
# is the drop-in, so no call site needs to know which one it holds.
APC = VirtualAPC
