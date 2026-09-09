"""A stand-in for the X-Touch Mini, talking to xtouchsim.py over UDP.

The device's BEHAVIOUR is not here. It is in xtouch_surface.py, shared with
the real driver, so this file is only three I/O methods and a resync. That
matters more for this device than it did for the APC: which layer is showing
has to be inferred, control ids translate both ways, the LED cache is per
layer, a write to a hidden layer is discarded and must never be cached, and
a lamp the device turns off by itself has to be put back. Every one of those
was found against hardware. A simulator that reimplemented them would
resemble the device rather than behave like it, and would drift the first
time either copy was touched.

xtouch.py is untouched by this existing: the class that drives real hardware
should not grow a "pretend" branch, which is why these are separate classes
over a shared base rather than one class with a flag.

The QUIRKS -- discarding writes to the hidden layer, lighting a button while
it is held -- belong to xtouchsim.py, because they are things the device
does, not things a driver does.
"""

import simlink

from xtouch_surface import (                  # noqa: F401 -- re-exported
    XTouchBase, XTouchError,
    CHANNEL, ENCODER_PUSH, BUTTONS_TOP, BUTTONS_BOTTOM, ENCODER_CC, FADER_CC,
    LED_NOTE, RING_CC, NAME, MAPPING_NAMES,
    PADS, BUTTONS, PUSHES, RINGS, FADERS, FADER_LAYERS,
    LAYERS, LAYER_NAMES, LAYER_AT_START, LAYER_HINT, BUTTON_SHOWS,
    LAYER_FALLS_THROUGH, PAINT_HIDDEN_LAYERS,
    OFF, ON, IDLE, FEEDBACK, SOFT_BLINK,
    layer_index, layer_of, parse_control, describe_control,
    to_control, to_note, to_fader,
)


class VirtualXTouch(XTouchBase):
    """The simulator, over two UDP sockets."""

    def __init__(self, led_addr=None, event_addr=None):
        self.led_addr = led_addr or simlink.XTOUCH_LED_ADDR
        self.event_addr = event_addr or simlink.XTOUCH_EVENT_ADDR
        self.input_name = f"simulator (events on {self.event_addr[1]})"
        self.output_name = f"simulator (LEDs to {self.led_addr[1]})"
        self.link = simlink.Endpoint(self.event_addr, self.led_addr)
        super().__init__()

    def _incoming(self):
        """Datagrams, translated into the same shape MIDI arrives in.

        The wire carries RAW device numbers -- notes 8-23 and 32-47, CCs
        1-18 -- not control ids, so everything above this sees exactly what
        it would see from the hardware, translation included.
        """
        for payload in self.link.drain():
            kind = payload[0]
            if kind == simlink.PRESS and len(payload) >= 2:
                yield "note", payload[1], 127
            elif kind == simlink.RELEASE and len(payload) >= 2:
                yield "note", payload[1], 0
            elif kind == simlink.CC and len(payload) >= 3:
                yield "cc", *simlink.decode_cc(payload)
            elif kind == simlink.HELLO:
                self._resend_all()

    def _resend_all(self):
        """Replay every lamp and ring we believe the surface should show.

        Answers a simulator HELLO, and it has to be a replay rather than
        just dropping the cache: the controller only repaints when something
        changes, so a simulator started against a static show would stay
        blank until the next press. Sent for BOTH layers, since only one of
        the two writes lands and there is no telling which -- exactly what
        the device does with them.
        """
        for layer in range(LAYERS):
            for control, state in list(self._desired[layer].items()):
                if isinstance(control, tuple):          # ("ring", n)
                    self.ring(control[1], state, force=True, layer=layer)
                else:
                    self.button(control, state, force=True, layer=layer)

    def _send_note(self, note, velocity):
        # An LED update, not a press: PRESS and RELEASE travel the other way.
        # One triple per datagram is fine here -- the APC batches because a
        # repaint is eighty of them, and this surface has sixteen lamps.
        self.link.send(simlink.encode_leds([(note, velocity, CHANNEL)]))

    def _send_cc(self, control, value):
        self.link.send(simlink.encode_cc(control, value))

    def _close(self):
        self.link.close()


# controller.py builds `surface_module().Surface()`. The alias means the
# module itself is the contract, so no call site learns which device it holds.
Surface = VirtualXTouch
