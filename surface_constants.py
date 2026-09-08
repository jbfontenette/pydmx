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
