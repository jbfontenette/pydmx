"""The APC mini mk2 control map and LED channel table. One copy, imported.

This exists because the numbers were previously written out three times --
apc.py, virtualapc.py and apc_leds.py -- and they drifted: changing the idle
brightness from 10% to 25% needed three edits, and the third was missed until
someone noticed the contrast test previewing a gap the controller no longer
produced. Nothing here depends on mido or on a socket, so every surface
module can import it and the values cannot disagree.

Control map confirmed against hardware:

    grid pads      notes 0-63     RGB, note 0 is BOTTOM-LEFT, note = row*8+col
    track buttons  notes 100-107  single-colour red
    scene launch   notes 112-119  single-colour green, 112 is the TOP one
    shift          note  122      no LED at all
    faders         CC    48-56    absolute position, full 0-127 travel

LED output is a Note On where the CHANNEL encodes behaviour and the VELOCITY
encodes the palette colour. Channels 0-6 are brightness steps, 7-10 pulse,
11-15 blink.
"""

GRID = range(0x00, 0x40)
TRACK_BUTTONS = range(0x64, 0x6C)
SCENE_BUTTONS = range(0x70, 0x78)
SHIFT = 0x7A
FADER_CC = range(0x30, 0x39)

# Note On channels = LED behaviour (protocol doc, page 3). Only the ones the
# controller uses are named here; apc_leds.py carries the full sixteen-entry
# table because listing every behaviour is that tool's whole job.
SOLID_10 = 0
SOLID_25 = 1
SOLID_50 = 2
SOLID_100 = 6
PULSE_4 = 9
BLINK_4 = 14
BLINK_2 = 15

OFF = 0          # velocity 0 = unlit

# Idle sits well below active, not near it. At 50% an idle pad and an active
# one look nearly identical -- the eye compares ratios, and 50:100 is only one
# stop apart. 25:100 is four times the light, still unmistakable across a dark
# room, and unlike 10% the idle pad is actually readable at a glance: at 10%
# you could not see where the unlit bindings were. Keep this well under 50%.
IDLE = SOLID_25

# How an active binding is shown. Intensity is the default because it is the
# only one guaranteed to work: pulse and blink rates sync to an external MIDI
# clock, and with no clock running the device falls back to its own default,
# which may not animate at all.
FEEDBACK = {
    "intensity": SOLID_100,
    "pulse": PULSE_4,
    "blink": BLINK_4,
    "fast-blink": BLINK_2,
}

# Nothing: this device animates its own pads, and the rates above are the
# hardware's. A surface whose lamps can only be on or off animates them from
# the main loop instead -- see xtouch_constants.
SOFT_BLINK = {}


# --- the surface vocabulary -----------------------------------------------
#
# What controller.py and showfile.py are allowed to know about a surface.
# xtouch_constants.py carries the same names for a device with no grid, no
# colour and a latching layer button, so every call site can hold either one
# and never ask which it got.

NAME = "apc"

# Mapping files this surface will use, in order. The bare name comes second
# so an existing show keeps working untouched, and first place is left for a
# layout written specifically for this device.
MAPPING_NAMES = ("mapping-apc.csv", "mapping.csv")

LAYERS = 2
LAYER_NAMES = ("", "shift")

# The APC's second layer is a HELD modifier, so it is unambiguously off at
# startup -- unlike a latching layer button, which leaves the surface's state
# unknowable until someone touches it.
LAYER_AT_START = 0

# A control with nothing bound on the shift layer falls through to its base
# binding. Right for a HELD modifier: SHIFT is a way to reach a few extra
# things, and losing every other pad for as long as you hold it would be
# absurd. Wrong for a latching one -- see xtouch_constants.
LAYER_FALLS_THROUGH = True

# The APC's track and scene buttons show which controls are BOUND, and its
# grid -- where the show actually lives -- shows active state in colour and
# brightness. So these lamps are a map of the surface, not a state display.
BUTTON_SHOWS = "bound"

LAYER_HINT = "Hold SHIFT for the second layer."

PADS = GRID                                     # colour + behaviour LEDs
BUTTONS = tuple(TRACK_BUTTONS) + tuple(SCENE_BUTTONS)   # binary LEDs
RINGS = ()                                      # no value displays
FADERS = range(1, 10)

# The APC's faders send one CC whether or not SHIFT is held, so a fader
# binding on the second layer could never be reached. Saying so here lets
# showfile.py reject it at load time instead of leaving a dead row.
FADER_LAYERS = 1


def layer_index(name):
    """'shift' (or 0/1, or blank) -> layer index. Raises on anything else."""
    if name is None or name == "":
        return 0
    if isinstance(name, int):
        if name in (0, 1):
            return name
        raise ValueError(f"layer {name}: this surface has one shift layer")
    token = str(name).strip().lower()
    if token in LAYER_NAMES:
        return LAYER_NAMES.index(token)
    raise ValueError(f"'{name}': the APC's second layer is 'shift'")


def parse_control(token):
    """mapping.csv's vocabulary -> a note number, or ("fader", n).

    Accepts a raw note number, or friendlier position notation:

        12      raw note number
        r1c4    grid row 1, column 4 -- row 0 is the BOTTOM row
        t3      track button 3
        s2      scene launch button 2 (s1 is the TOP one)
        f8      fader 8 -- a separate key space, because fader CCs 48-56
                would otherwise collide with grid notes 48-56
    """
    token = str(token).strip().lower()
    if token.isdigit():
        note = int(token)
        if not 0 <= note <= 127:
            raise ValueError(f"note {note} out of range 0-127")
        return note
    if token.startswith("r") and "c" in token:
        row, _, col = token[1:].partition("c")
        row, col = int(row), int(col)
        if not (0 <= row <= 7 and 0 <= col <= 7):
            raise ValueError(f"'{token}': row and col must be 0-7")
        return row * 8 + col
    if token.startswith("f") and token[1:].isdigit():
        n = int(token[1:])
        if not 1 <= n <= 9:
            raise ValueError(f"'{token}': faders are f1-f9")
        return ("fader", n)
    if token.startswith("t"):
        n = int(token[1:])
        if not 1 <= n <= 8:
            raise ValueError(f"'{token}': track buttons are t1-t8")
        return 0x64 + n - 1
    if token.startswith("s"):
        n = int(token[1:])
        if not 1 <= n <= 8:
            raise ValueError(f"'{token}': scene buttons are s1-s8")
        return 0x70 + n - 1
    raise ValueError(f"cannot parse pad '{token}'")


def describe_control(control):
    """A control back to its token, for log lines about unbound presses."""
    if isinstance(control, tuple):
        return f"f{control[1]}"
    if control in GRID:
        row, col = divmod(control, 8)
        return f"r{row}c{col}"
    if control in TRACK_BUTTONS:
        return f"t{control - TRACK_BUTTONS.start + 1}"
    if control in SCENE_BUTTONS:
        return f"s{control - SCENE_BUTTONS.start + 1}"
    if control == SHIFT:
        return "shift"
    return f"note {control}"
