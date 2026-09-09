"""Behringer X-Touch Mini surface wrapper -- the MIDI half.

What the device DOES lives in xtouch_surface.py, which knows nothing about
how it is reached; this adds mido to it, and virtualxtouch.py adds a socket
instead. The split is not tidiness: the layer inference, the translation
both ways, the per-layer LED caches and the lamp put back on release were
all found the hard way against hardware, and a simulator that reimplemented
them would resemble the device rather than behave like it.

The control map lives in xtouch_constants.py, one copy, and both halves
import it. Its names are re-exported here because every call site reaches
them through whichever surface module it holds (`mod.BUTTONS`,
`mod.LAYER_AT_START`) and should not have to know or care which that is.
"""

import mido

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


def _find(names, kind):
    for name in names:
        if "x-touch" in name.lower() or "xtouch" in name.lower():
            return name
    raise XTouchError(f"No X-Touch {kind} port found. Available: "
                      + (", ".join(names) if names else "none")
                      + "\n  Check the USB cable, and that the device is in"
                      + "\n  Standard mode -- MC MODE changes every number.")


class XTouch(XTouchBase):
    """The real device, over MIDI."""

    def __init__(self, input_name=None, output_name=None):
        self.input_name = input_name or _find(mido.get_input_names(), "input")
        self.output_name = (output_name
                            or _find(mido.get_output_names(), "output"))
        self.inp = mido.open_input(self.input_name)
        self.out = mido.open_output(self.output_name)
        super().__init__()

    def _incoming(self):
        for msg in self.inp.iter_pending():
            if msg.type == "note_on":
                yield "note", msg.note, msg.velocity
            elif msg.type == "note_off":
                # Folded into a zero-velocity note_on, because the device
                # sends both and they mean the same thing. The base class is
                # then spared knowing MIDI has two ways to say "released".
                yield "note", msg.note, 0
            elif msg.type == "control_change":
                yield "cc", msg.control, msg.value

    def _send_note(self, note, velocity):
        self.out.send(mido.Message("note_on", channel=CHANNEL,
                                   note=note, velocity=velocity))

    def _send_cc(self, control, value):
        self.out.send(mido.Message("control_change", channel=CHANNEL,
                                   control=control, value=value))

    def _close(self):
        self.inp.close()
        self.out.close()


# controller.py builds `surface_module().Surface()`. The alias means the
# module itself is the contract, so no call site learns which device it holds.
Surface = XTouch
