"""The Behringer X-Touch Mini control map, and its surface vocabulary.

The counterpart of surface_constants.py, and it exists for the same reason:
these numbers were written down twice once already -- xtouch_leds.py kept its
own copy of the MIDI channel, as 0 instead of 10, and a whole hardware
session was spent testing an LED protocol that was working fine. One copy,
imported by the probes and the driver alike.

Nothing here imports mido, so showfile.py can resolve control names without
a MIDI backend installed.

THE WHOLE SURFACE IS ONE RULE: to drive a control, send the number that
control SENDS, on the layer currently showing. The other layer's numbers are
discarded -- dropped as they arrive, not queued. Buttons and rings alike.
Everything below is that rule spelled out, and the comments record how each
part of it was measured, including where the manufacturer's own
documentation says something different and is wrong.
"""

# --- control map, confirmed against hardware 2026-09-08 --------------------
#
# Every control on both layers sends on MIDI channel 10 (mido numbering).
# The layer button itself sends NOTHING: the device switches layers
# internally and simply starts sending the other set of numbers, so the
# controller never needs a page state machine for it.
#
# Notes are a clean +24 between layers. THE CCs ARE NOT. The two faders sit
# adjacent in the middle, with an encoder block on either side:
#
#     CC  1-8   encoders, layer A
#     CC  9     fader,    layer A
#     CC 10     fader,    layer B      <-- not 18, which +9 would predict
#     CC 11-18  encoders, layer B
#
# That irregularity was measured, not assumed -- the tidy offset guess put
# the layer B fader on CC 18 and was wrong. Anyone extending this should
# check against the device rather than continue the pattern.
CHANNEL = 10

ENCODER_PUSH = {"A": range(0, 8), "B": range(24, 32)}
BUTTONS_TOP = {"A": range(8, 16), "B": range(32, 40)}
BUTTONS_BOTTOM = {"A": range(16, 24), "B": range(40, 48)}
ENCODER_CC = {"A": range(1, 9), "B": range(11, 19)}
FADER_CC = {"A": 9, "B": 10}

# From photographs of the device, 2026-09-08:
#
#   * MC MODE is OFF. That is what makes the encoders absolute -- the same
#     unit in Mackie Control mode would send relative deltas instead, and
#     the whole encoder binding design would change. Worth checking that
#     button before believing anything here.
#   * Each layer keeps its OWN encoder positions. The same physical knobs
#     show different ring values on A and B, so there are effectively 16
#     independent absolute encoders, not 8, each remembered by the device.
#   * The lit LAYER A / LAYER B button is the only indication of which layer
#     is active. Nothing says so over MIDI.
#
# --- RX: what the device LISTENS on, measured 2026-09-08 ------------------
#
# IT LISTENS WHERE IT SPEAKS. Lighting a button means sending the note that
# button SENDS -- notes 8-23 for the sixteen buttons on layer A, top row
# then bottom, left to right. Notes 0-7 and 24-31, the encoder-push numbers,
# light nothing, which is right: a push has no lamp.
#
# Worth stating flatly because Behringer's own X-Touch Editor says
# otherwise, and believing it cost a session. Its GLOBAL tab lists an
# RX MIDI CONTROL map:
#
#     LED Ring Behavior   CC 1-8
#     LED Ring Value      CC 9-16
#     Button LEDs         NOTE 0-15
#     Layer A / B select  Program Change 0 / 1
#     Standard / MC mode  CC 127 value 0 / 1
#
# Tested against this unit, Standard mode, channel 10:
#
#   * NOTE 0-15 lights nothing. Notes 8-23 light the buttons. The editor is
#     numbering the buttons 0-15 as an index, not as MIDI notes.
#   * PROGRAM CHANGE 0 and 1 do NOT switch the layer -- the lamp does not
#     move. The controller cannot select the layer and is back to inferring
#     it from arriving notes, exactly as the LED OUTPUT section below says.
#   * CC 9-16 is not the ring value. CC 9 and 10 are the two FADERS and
#     moved no ring at all.
#
# The likeliest explanation is that the editor describes MC mode, or a
# firmware other than this one. Either way it is not a source for this
# driver. Do not re-derive the map from it -- that is how the two measured
# facts at the bottom of this comment came to be doubted for a day.
#
# RINGS answer their own encoder's transmit CC, per layer, exactly as the
# buttons do. Measured on both layers, 2026-09-08:
#
#     layer A showing   CC 1-8 move rings 1-8; CC 11-18 do nothing
#     layer B showing   CC 11-18 move rings 1-8; CC 1-8 do nothing
#
# CC 21-28 does nothing on either layer, so the "transmit CC plus ten" guess
# is dead. There is no separate behaviour CC: the value is the whole message.
#
# So ONE RULE covers the whole surface, input and output, buttons and rings:
#
#     to drive a control, send the number that control SENDS on the layer
#     that is currently showing; the other layer's numbers are discarded.
#
# Which is why the layer has to be tracked -- see LAYER, below. It is the
# single fact the driver cannot do without.
#
# The RING DISPLAY STYLE is a device-side setting, not a MIDI one. The same
# value drew differently on layer A and layer B on this unit, and nothing in
# the CC selects that -- it is per encoder, per layer, and set in X-Touch
# Editor. So the controller chooses a ring's VALUE and the editor chooses
# how it is drawn; a pan/tilt encoder wanting a single travelling dot and a
# level wanting a fill have to be configured on the device beforehand.
#
# Rings also keep per-layer state, visibly: layer A's rings sat at their
# first LED at rest while layer B's sat dark. Same knobs, two remembered
# positions, which matches the encoders sending independent values per
# layer.
#
# WRITING A RING MOVES THE ENCODER, not just its lamps. Measured: with the
# knob sitting at 7, CC 1 was set to 110, and the next click right reported
# 111. So the host can PUT an encoder where it wants it.
#
# That is what cures the startup jump. This device has no Introduction
# message -- there is no asking it where its encoders are sitting, the way
# the APC can be asked -- so the controller starts blind, and a `scale`
# encoder resting at zero would slam its group from full to nothing on the
# first touch. It cannot be read, but it CAN be written: paint every bound
# encoder with the value the show implies and the knob physically starts
# where the show is. introduce() inverted.
#
# LED OUTPUT, tested 2026-09-08 (xtouch_leds.py layers):
#
#   * Lighting a note works, on channel 10. Channel 0 lights nothing, which
#     is worth stating because it looks exactly like a dead LED protocol.
#   * A note sent for the INACTIVE layer is DISCARDED, not stored. Sending
#     note 32 while the device is on layer A lights nothing at the time, and
#     nothing appears when you then switch to layer B. This stands: note 32
#     is layer B's first button, the correct address, so the test was aimed
#     at something real.
#
# THE DEVICE REMEMBERS EACH LAYER SEPARATELY. Note 8 lit on layer A stayed
# lit through a switch to B and back -- and layer B showed nothing in
# between, including the note 32 sent while A was showing. So the device
# holds two independent LED surfaces and shows one of them; what it never
# does is accept a write to the one it is not showing.
#
# That is the better of the two possible answers, and it decides the paint
# policy:
#
#   * Keep a DESIRED state and a DELIVERED state per layer. While a layer is
#     hidden, update desired only -- sending is pointless, the write is
#     dropped. On a layer change, flush the difference for the layer that
#     just appeared. At most sixteen notes, and usually none.
#   * Do NOT drop the whole cache and repaint on a switch. That was the plan
#     when the device was assumed to forget; it would now send sixteen
#     redundant messages per switch for nothing. The cache stays valid
#     precisely because we never write to a hidden layer.
#   * Painting BOTH numbers for a control still does not help. The inactive
#     write is dropped at the moment it is made, not stored for later.
#
# The layer must still be TRACKED, and input is the only source: the device
# never announces a switch, and program change does not cause one. An
# arriving note below 24 means layer A, 24 and above means layer B.
#
#   * At startup the layer is unknown. Invariant 10 says fail safe on
#     unknown state: paint nothing until the first press says where we are,
#     rather than guessing A and lighting a surface that may not be showing.
#   * After a switch made without touching anything, the surface is not
#     dark -- it shows whatever that layer was last told, which may be
#     stale. Self-correcting on the first press, and one gesture is the
#     whole cost.
#   * The encoder RINGS need no painting to stay right: the device drives
#     them from its own remembered per-layer values, so a knob the user
#     turns always reads correctly with the controller sending nothing. For
#     pan/tilt that native display is exactly what is wanted. They are NOT
#     exempt from the layer rule, though -- when the controller does drive a
#     ring, to show a value the software owns rather than one the user
#     turned, it must use the showing layer's CC like everything else.
#
# CONFIRMED control by control with --learn on 2026-09-08. Every inference
# above held: the row nearer the encoders is notes 8-15, both rows and the
# encoders run left to right, encoder N is CC N, and the fader irregularity
# is real. 33 of 33 controls send different numbers on layer B; the 34th,
# the layer button, sends nothing at all and so does not appear.
#
# Nothing about the INPUT side is inferred any more.
#
# BUTTON LEDs are BINARY (xtouch_leds.py states 8, channel 10): velocity 0
# is off and every value from 1 to 127 is plain on. No brightness steps and
# no blink anywhere in the range. Note 8 is the top-left button -- confirmed
# by the note walk, so this was measured on the right lamp.
#
# Measured twice. The first pass walked the velocities without resetting, so
# it could not tell an IGNORED value from one meaning "on" -- the lamp was
# simply still lit from the previous step. The second sets each value from
# off, which distinguishes the three outcomes, and every velocity 1-127 lit
# it steady. Nothing was ignored and nothing blinked.
#
# This CONTRADICTS the community/manual summaries in circulation, which say
# velocity 2 blinks and 3-127 are ignored (a QLC+ thread reports different
# numbers again, 4 for on and 6 for blink). The measurement is from this
# unit, in Standard mode, on channel 10, and is what the driver should
# believe until the actual Behringer document says otherwise.
#
# Blink may exist in MC MODE -- the Mackie protocol conventionally puts
# flashing on velocity 1 -- but reaching it would mean giving up Standard
# mode, and with it the absolute encoders and this entire control map. So
# blink is not available in the configuration this driver wants, whatever
# the manual turns out to say.
#
# That costs something the APC provides. On the APC an idle-but-BOUND pad
# glows at 25% so you can see where your bindings live before pressing
# anything, and --feedback offers pulse and blink for active ones. Here a
# bound-but-inactive button looks exactly like an unbound one, so the whole
# idle/active brightness scheme has no equivalent and the FEEDBACK table
# collapses to on/off. Worth knowing when laying a show out on this surface:
# the buttons cannot show you where anything is.
#
# Untested: whether another MIDI CHANNEL carries blink, the way the APC puts
# behaviour in the channel and colour in the velocity. Channel 0 lights
# nothing at all, channel 10 lights steady; the other fourteen are unknown.
# xtouch_leds.py scan-channels walks them. Also untested: whether X-Touch
# Editor can configure a button's LED behaviour in a way MIDI cannot reach.


# --- what the device LISTENS on -------------------------------------------
#
# Measured, not read off the editor. The RX numbers are the TX numbers: to
# light a button, send the note it sends. So these are aliases of the input
# ranges rather than a second map, and they exist to say so at the point of
# use -- the alternative is every LED call site quietly assuming it.
LED_NOTE = {layer: range(BUTTONS_TOP[layer].start, BUTTONS_BOTTOM[layer].stop)
            for layer in ("A", "B")}

# A ring listens on the CC its encoder transmits, so this is an alias of the
# input map rather than a second one. Named separately all the same, because
# a call site setting a ring should not read as one reading an encoder.
RING_CC = ENCODER_CC

# What xtouch_leds.py rings walks. The third block is kept in the walk after
# being ruled out: it costs eight prompts and it is the cheapest way to
# notice a firmware that moved things.
RING_CC_CANDIDATES = (ENCODER_CC["A"], ENCODER_CC["B"], range(21, 29))


def ring_cc(layer, index):
    """CC that drives one encoder's LED ring. index is 1-8."""
    if not 1 <= index <= 8:
        raise ValueError(f"encoder {index} is out of range 1-8")
    return RING_CC[layer][index - 1]


def led_note(layer, row, index):
    """Note that lights one button. row is 'top' or 'bottom', index 1-8."""
    block = BUTTONS_TOP if row == "top" else BUTTONS_BOTTOM
    if not 1 <= index <= 8:
        raise ValueError(f"button {index} is out of range 1-8")
    return block[layer][index - 1]


def name_for(kind, number):
    """Label a note or CC, or None if it is not a control we know.

    Returns e.g. ("encoder 3 push", "A") so the raw log can say what was
    touched and which layer it came from -- the layer is implied by the
    number, since the device never announces the switch.
    """
    for layer in ("A", "B"):
        if kind == "note":
            if number in ENCODER_PUSH[layer]:
                n = number - ENCODER_PUSH[layer].start + 1
                return f"encoder {n} push", layer
            if number in BUTTONS_TOP[layer]:
                n = number - BUTTONS_TOP[layer].start + 1
                return f"button top {n}", layer
            if number in BUTTONS_BOTTOM[layer]:
                n = number - BUTTONS_BOTTOM[layer].start + 1
                return f"button bottom {n}", layer
        elif kind == "cc":
            if number == FADER_CC[layer]:
                return "fader", layer
            if number in ENCODER_CC[layer]:
                n = number - ENCODER_CC[layer].start + 1
                return f"encoder {n}", layer
    return None


# --- the surface vocabulary -----------------------------------------------
#
# What controller.py and showfile.py are allowed to know about this device.
# Both reach it through whichever surface module they hold, so the names here
# match surface_constants.py even where the values could not be more
# different.
#
# A CONTROL ID IS LAYER-INDEPENDENT. Buttons and pushes are named by their
# LAYER A numbers, and the driver translates: a press of note 34 arrives as
# control 10 on layer 1. mapping-xtouch.csv therefore never mentions a layer
# B number, and moving a binding between layers is a one-column edit.

NAME = "xtouch"

# One name only, and deliberately not mapping.csv. An APC layout loaded here
# would be sixteen unparseable tokens and a fatal error at startup -- which is
# the right outcome, but a confusing way to reach it. A surface that is not
# the default has to be given a file written for it.
MAPPING_NAMES = ("mapping-xtouch.csv",)

LAYERS = 2
LAYER_NAMES = ("a", "b")

# Which layer the device is showing at startup: UNKNOWN. It does not say, and
# it cannot be asked -- program change does not move it either. Invariant 10
# says fail safe on unknown state, so the controller paints nothing until the
# first press reveals where we are. Guessing 'a' would light a surface that
# may not be the one in front of you.
LAYER_AT_START = None

# No fall-through. This layer button LATCHES: layer B is a second page you
# stay on, not a modifier you hold, and a page that silently inherits the
# other page's bindings wherever it is blank is a trap. Pressing an unbound
# button on B fired A's reload, which is exactly the kind of surprise a page
# should not have. If a control should work on both layers, bind it on both
# -- one extra row, and the file then says what the surface does.
LAYER_FALLS_THROUGH = False

# Each layer has its OWN lamps, and a write to the layer that is not showing
# is discarded rather than stored -- both measured. So painting every layer
# every time is free of consequence, and the device keeps the one that
# matters. That is what removes the need to know which layer is showing in
# order to paint it correctly: right at startup instead of dark until
# something is pressed, and right again after the layer is switched by hand,
# which this device never reports.
PAINT_HIDDEN_LAYERS = True

# These sixteen lamps ARE the surface: there is no grid behind them. So they
# show what is ACTIVE rather than what is bound. A binary lamp cannot do
# both, and mid-set you need to see what is running -- the layout you learn.
BUTTON_SHOWS = "active"

LAYER_HINT = ("Press LAYER for the second layer. The surface stays dark until the\n"
              "first press -- the device does not say which layer it is showing.")

PADS = ()                       # no colour LEDs anywhere on this device
BUTTONS = range(8, 24)          # the 16 binary lamps, layer A numbering
PUSHES = range(0, 8)            # encoder pushes: real controls, no lamps
RINGS = range(1, 9)             # encoder LED rings, by encoder number
FADERS = range(1, 10)           # 8 encoders then the fader, as f9

# Unlike the APC's, these really are layered: the device remembers a separate
# position per layer for every encoder and for the fader, so there are 18
# independent continuous controls, not 9.
FADER_LAYERS = 2

OFF = 0
ON = 127

# Idle has no expression here. On the APC a bound-but-inactive pad glows at
# 25% so you can see where your bindings live before pressing anything; these
# lamps are BINARY, measured velocity by velocity from off, so a bound button
# looks exactly like an unbound one. IDLE is OFF because that is the only
# thing it can be, and the honest way to say so is to let the name stay and
# the value collapse.
IDLE = OFF

# The device cannot blink. Velocity 0 is off and every value from 1 to 127 is
# plain steady on, measured from off each time, on channel 10. Blink DOES
# exist at velocity 1 -- but in MC MODE, where the buttons are notes 40-45
# and 84-95 and the encoders send relative deltas, which would cost the
# absolute encoders and this entire control map for a flashing lamp.
#
# So the blinking styles are done in SOFTWARE: the controller toggles the
# lamp and the device just holds whatever it was last told. That is the same
# place the rest of this surface's state already lives, and it means a style
# behaves identically on every button without any per-button setting in
# X-Touch Editor to keep in step with the show files.
#
# FEEDBACK still maps style -> what a lit lamp is sent, so that build_leds
# reads the same on both surfaces. What differs is SOFT_BLINK.
FEEDBACK = {"intensity": ON, "blink": ON, "fast-blink": ON}

# style -> blinks per second, for styles this surface animates itself.
# Empty on a device that blinks in hardware. Deliberately slow: the repaint
# is diff-based, so a blinking lamp costs one MIDI message per half-cycle
# and nothing else on the surface is re-sent.
SOFT_BLINK = {"blink": 2.0, "fast-blink": 4.0}


def layer_index(name):
    """'a'/'b' (or 0/1, or '') -> layer index. Raises on anything else."""
    if name is None or name == "":
        return 0
    if isinstance(name, int):
        if name in (0, 1):
            return name
        raise ValueError(f"layer {name}: this surface has layers a and b")
    token = str(name).strip().lower()
    if token in LAYER_NAMES:
        return LAYER_NAMES.index(token)
    raise ValueError(f"'{name}': layer must be a or b")


def layer_of(kind, number):
    """Which layer a raw MIDI number belongs to, or None if it is not ours."""
    found = name_for(kind, number)
    return None if found is None else LAYER_NAMES.index(found[1].lower())


def to_control(note, layer):
    """Raw note -> layer-independent control id."""
    return note - (0 if layer == 0 else 24)


def to_note(control, layer):
    """Layer-independent control id -> the raw note for that layer."""
    return control + (0 if layer == 0 else 24)


def to_fader(cc, layer):
    """Raw CC -> fader number 1-9, or None. Encoders are 1-8, the fader 9."""
    name = "A" if layer == 0 else "B"
    if cc == FADER_CC[name]:
        return 9
    if cc in ENCODER_CC[name]:
        return cc - ENCODER_CC[name].start + 1
    return None


def parse_control(token):
    """mapping-xtouch.csv's vocabulary -> a control id or ("fader", n).

    Deliberately not the APC's. The two devices have nothing in common
    geometrically, and a shared spelling would only invite a layout written
    for one to half-work on the other.

        bt3     button, top row, 3rd from the left
        bb8     button, bottom row, 8th
        b11     button 11 counting the top row then the bottom (bt1..bb8)
        p4      push switch of encoder 4
        e2      encoder 2, a continuous control
        f1      the fader, also reachable as f9 so 'master' spells the same
                way as it does on the APC
        34      a raw note number, for a control this table cannot name

    The layer comes from the row's own column, never from the token, which
    is what keeps a binding movable between layers by editing one cell.
    """
    token = str(token).strip().lower()
    if not token:
        raise ValueError("no control given")

    if token.isdigit():
        note = int(token)
        if note not in BUTTONS and note not in PUSHES:
            raise ValueError(
                f"note {note} is not a control on this surface. Buttons are "
                f"{BUTTONS.start}-{BUTTONS.stop - 1} and encoder pushes "
                f"{PUSHES.start}-{PUSHES.stop - 1}, in layer A numbering")
        return note

    prefix = token[0]
    rest = token[1:]

    if token[:2] in ("bt", "bb") and token[2:].isdigit():
        index = int(token[2:])
        if not 1 <= index <= 8:
            raise ValueError(f"'{token}': there are 8 buttons in a row")
        row = BUTTONS.start if token[:2] == "bt" else BUTTONS.start + 8
        return row + index - 1

    if prefix == "b" and rest.isdigit():
        index = int(rest)
        if not 1 <= index <= 16:
            raise ValueError(f"'{token}': buttons are b1-b16, or bt1-bt8 and "
                             f"bb1-bb8 by row")
        return BUTTONS.start + index - 1

    if prefix == "p" and rest.isdigit():
        index = int(rest)
        if not 1 <= index <= 8:
            raise ValueError(f"'{token}': encoder pushes are p1-p8")
        return PUSHES.start + index - 1

    if prefix == "e" and rest.isdigit():
        index = int(rest)
        if not 1 <= index <= 8:
            raise ValueError(f"'{token}': encoders are e1-e8")
        return ("fader", index)

    if prefix == "f" and rest.isdigit():
        index = int(rest)
        # f9 as well as f1: on the APC the master lives on f9, and a show
        # moved between surfaces should not have to rename it.
        if index not in (1, 9):
            raise ValueError(f"'{token}': this surface has one fader, "
                             f"f1 (also spelled f9 to match the APC)")
        return ("fader", 9)

    raise ValueError(f"cannot parse control '{token}'")


def describe_control(control):
    """A control id back to its token, for log lines about unbound presses."""
    if isinstance(control, tuple):
        number = control[1]
        return "f1" if number == 9 else f"e{number}"
    if control in PUSHES:
        return f"p{control - PUSHES.start + 1}"
    if control in BUTTONS:
        index = control - BUTTONS.start
        row, position = divmod(index, 8)
        return f"{'bt' if row == 0 else 'bb'}{position + 1}"
    return f"note {control}"
