#!/usr/bin/env python3
"""
APC mini mk2 LED output test.

    pip install mido python-rtmidi

    python3 apc_leds.py names      # the 16 named colours, with a legend
    python3 apc_leds.py contrast   # idle vs active brightness, side by side
    python3 apc_leds.py rgbtest    # is SysEx pad colouring supported here?

Options: --auto (run on timers), --port "NAME" (pick the MIDI output).
NOTE: --feedback belongs to controller.py, not this script.
    python3 apc_leds.py verify     # confirm note 0 is bottom-left
    python3 apc_leds.py layout     # map note numbers to physical positions
    python3 apc_leds.py palette    # browse the 128-colour palette
    python3 apc_leds.py behaviour  # brightness / pulse / blink channels
    python3 apc_leds.py buttons    # track + scene launch single-colour LEDs
    python3 apc_leds.py sysex      # arbitrary 24-bit RGB, bypassing the palette
    python3 apc_leds.py off        # clear everything

Every test waits for Enter between steps so you can actually look at the
device. Add --auto to run on timers instead.

All of it from the v1.0 communications protocol. The key idea for RGB pads:
a Note On where the CHANNEL sets the behaviour and the VELOCITY sets the
colour. So 96 00 05 == channel 6 (solid 100%), pad 0, colour 5 (red).
"""

import sys
import time

import mido

GRID = range(0x00, 0x40)          # pads 0-63
TRACK_BUTTONS = range(0x64, 0x6C)  # red single-colour LEDs
SCENE_BUTTONS = range(0x70, 0x78)  # green single-colour LEDs

# Behaviour is the MIDI channel on a Note On (protocol table, page 3).
SOLID_10, SOLID_25, SOLID_50, SOLID_65 = 0, 1, 2, 3
SOLID_75, SOLID_90, SOLID_100 = 4, 5, 6
PULSE_16, PULSE_8, PULSE_4, PULSE_2 = 7, 8, 9, 10
BLINK_24, BLINK_16, BLINK_8, BLINK_4, BLINK_2 = 11, 12, 13, 14, 15

BEHAVIOURS = [
    (SOLID_10, "solid 10%"), (SOLID_25, "solid 25%"), (SOLID_50, "solid 50%"),
    (SOLID_65, "solid 65%"), (SOLID_75, "solid 75%"), (SOLID_90, "solid 90%"),
    (SOLID_100, "solid 100%"),
    (PULSE_16, "pulse 1/16"), (PULSE_8, "pulse 1/8"),
    (PULSE_4, "pulse 1/4"), (PULSE_2, "pulse 1/2"),
    (BLINK_24, "blink 1/24"), (BLINK_16, "blink 1/16"), (BLINK_8, "blink 1/8"),
    (BLINK_4, "blink 1/4"), (BLINK_2, "blink 1/2"),
]

# Eight clearly distinguishable palette entries, for the layout map.
DISTINCT = [
    (5, "red"), (9, "orange"), (13, "yellow"), (21, "green"),
    (37, "cyan"), (45, "blue"), (49, "violet"), (53, "magenta"),
]

WHITE, OFF = 3, 0

AUTO = False  # --auto runs on timers instead of waiting for Enter


def wait(prompt="Enter for next", seconds=2.5):
    """Hold until the user has looked at the device."""
    if AUTO:
        time.sleep(seconds)
        return
    try:
        input(f"      -- {prompt}, Ctrl-C to stop --")
    except EOFError:
        time.sleep(seconds)


def find_port():
    names = mido.get_output_names()
    if not names:
        sys.exit("No MIDI outputs found. Is the APC plugged in?")
    for name in names:
        if "apc" in name.lower():
            return name
    print("No port with 'APC' in the name. Available outputs:")
    for name in names:
        print(f"  {name}")
    sys.exit("Pass one as an argument.")


def pad(out, note, colour, behaviour=SOLID_100):
    """Light one RGB grid pad. Channel = behaviour, velocity = palette index."""
    out.send(mido.Message("note_on", channel=behaviour, note=note, velocity=colour))


def button(out, note, state=1):
    """Light one single-colour UI button. Always channel 0.

    state: 0 = off, 1 (or 3-127) = on, 2 = blink. Colour is fixed by the
    hardware -- track buttons red, scene launch green.
    """
    out.send(mido.Message("note_on", channel=0, note=note, velocity=state))


def sysex_rgb(out, start_pad, end_pad, r, g, b):
    """Set a pad range to an arbitrary 24-bit colour, bypassing the palette.

    MIDI data bytes are 7-bit, so each 8-bit component splits into MSB/LSB.
    """
    data = [start_pad, end_pad,
            r >> 7, r & 0x7F,
            g >> 7, g & 0x7F,
            b >> 7, b & 0x7F]
    n = len(data)
    # F0 47 7F 4F 24 <lenMSB> <lenLSB> <payload> F7 -- mido omits F0/F7.
    out.send(mido.Message("sysex",
                          data=[0x47, 0x7F, 0x4F, 0x24, n >> 7, n & 0x7F] + data))


def clear(out):
    for note in GRID:
        pad(out, note, OFF)
    for note in list(TRACK_BUTTONS) + list(SCENE_BUTTONS):
        button(out, note, 0)


# --- tests ------------------------------------------------------------------

def test_layout(out):
    """Work out which physical pad each note number is."""
    clear(out)

    print("Step 1: pad 0 alone, in white. Note where it is.\n")
    pad(out, 0, WHITE)
    wait("got it?")

    print("Step 2: notes 0-7 lighting one at a time. Watch the direction.\n")
    for note in range(8):
        clear_grid(out)
        pad(out, note, WHITE)
        print(f"  note {note}")
        wait("next note", 0.7)

    print("\nStep 3: whole grid, one colour per group of 8. Photograph this.\n")
    for note in GRID:
        colour, name = DISTINCT[note // 8]
        pad(out, note, colour)
    for i, (_, name) in enumerate(DISTINCT):
        print(f"  notes {i*8:>2}-{i*8+7:<2}  {name}")
    print("\nThat photo is your definitive layout map. Ctrl-C when done.")
    idle()


def clear_grid(out):
    for note in GRID:
        pad(out, note, OFF)


def note_at(row, col):
    """Note number for a grid position. Row 0 is the BOTTOM row, col 0 left."""
    return row * 8 + col


def test_verify(out):
    """Confirm the documented layout: note 0 bottom-left, rows running up.

    Each step is unambiguous -- if the layout were flipped or transposed,
    every one of these would light the wrong edge.
    """
    checks = [
        ("note 0 alone, white -- expect BOTTOM-LEFT corner", [0], WHITE),
        ("notes 0-7 red -- expect BOTTOM row", list(range(0, 8)), 5),
        ("notes 56-63 blue -- expect TOP row", list(range(56, 64)), 45),
        ("notes 0,8,16.. green -- expect LEFT column",
         list(range(0, 64, 8)), 21),
        ("notes 7,15,23.. yellow -- expect RIGHT column",
         list(range(7, 64, 8)), 13),
        ("diagonal magenta -- expect BOTTOM-LEFT to TOP-RIGHT",
         [note_at(i, i) for i in range(8)], 53),
    ]
    for label, notes, colour in checks:
        clear_grid(out)
        for n in notes:
            pad(out, n, colour)
        print(f"  {label}")
        wait("correct?")
    print("\nAll six correct means the layout is confirmed.")


def _sysex_entries(out, entries):
    data = []
    for start, end, (r, g, b) in entries:
        data += [start, end, r >> 7, r & 0x7F, g >> 7, g & 0x7F,
                 b >> 7, b & 0x7F]
    n = len(data)
    out.send(mido.Message("sysex",
                          data=[0x47, 0x7F, 0x4F, 0x24, n >> 7, n & 0x7F] + data))


def test_names(out):
    """The 16 named colours on the bottom two rows, with a legend.

    Uses the palette (Note On), which is the path the controller actually
    uses. Anything that reads wrong, change its palette index in colours.py.
    """
    import colours
    clear_grid(out)
    print("Bottom two rows, left to right, bottom row first:\n")
    for i, name in enumerate(colours.ORDER):
        index = colours.palette(name)
        pad(out, i, index)
        row, col = divmod(i, 8)
        print(f"  r{row}c{col}  {name:<12} palette {index:>3}")
    print("\nAnything that does not match its name, change colours.py.")
    idle()


def test_rgbtest(out):
    """Is SysEx pad colouring supported on this unit? One thing at a time.

    Built as a ladder because a bulk SysEx write failed here in a way that
    lit only part of the grid -- which could mean unsupported, malformed,
    or simply too much at once. Each step narrows that down.
    """
    clear_grid(out)
    time.sleep(0.2)

    print("Step 1: pad 0 to red via SysEx only (single pad, one message).")
    _sysex_entries(out, [(0, 0, (255, 0, 0))])
    wait("did pad 0 light red?")

    print("Step 2: pads 0-7 as one range, green.")
    _sysex_entries(out, [(0, 7, (0, 255, 0))])
    wait("did the whole bottom row go green?")

    print("Step 3: 8 separate entries in ONE message, one per pad.")
    _sysex_entries(out, [(i, i, (0, 0, 255)) for i in range(8, 16)])
    wait("did all 8 of row 1 go blue?")

    print("Step 4: same 8, but as 8 separate messages with a small gap.")
    for i in range(16, 24):
        _sysex_entries(out, [(i, i, (255, 255, 0))])
        time.sleep(0.02)
    wait("did all 8 of row 2 go yellow?")

    print("\nIf 1 and 2 worked but 3 failed, the device wants one entry per")
    print("message. If 3 failed and 4 worked, it needs pacing. If 1 failed,")
    print("SysEx pad colour is not usable and the palette is the only path.")
    idle()


def test_contrast(out):
    """Idle vs active brightness, side by side, so you can judge the gap."""
    import colours
    clear_grid(out)
    print("Left half of each row = IDLE (25%), right half = ACTIVE (100%).\n")
    for i, name in enumerate(colours.ORDER[:8]):
        index = colours.palette(name)
        for col in range(4):
            # Must stay whatever apc.IDLE is, or this test previews a
            # contrast the controller does not actually produce.
            pad(out, note_at(i, col), index, SOLID_25)
        for col in range(4, 8):
            pad(out, note_at(i, col), index, SOLID_100)
        print(f"  row {i}  {name}")
    print("\nIf the gap still is not obvious, try the pulse/blink styles:")
    print("  python3 controller.py --feedback pulse")
    idle()


def test_palette(out):
    """Show the 128-colour palette, 64 pads at a time."""
    for page in (0, 1):
        clear_grid(out)
        base = page * 64
        for i, note in enumerate(GRID):
            pad(out, note, base + i)
        print(f"Page {page}: pad N shows palette colour {base}+N "
              f"({base}-{base + 63})")
        print("Press Enter for the next page...")
        input()


def test_behaviour(out):
    """One column per behaviour, so you can see brightness and blink rates."""
    clear_grid(out)
    print("Lighting all 64 pads red, cycling through each behaviour.\n")
    for channel, label in BEHAVIOURS:
        for note in GRID:
            pad(out, note, 5, channel)
        print(f"  channel {channel:>2}  {label}")
        wait("next behaviour")
    print("\nNote: pulse and blink rates sync to an external clock, so with no")
    print("clock running they use the device default.")


def test_buttons(out):
    """The peripheral single-colour LEDs.

    Colour is fixed in hardware: track buttons red, scene launch green.
    SHIFT (0x7A) has no LED at all -- the protocol lists it as None -- so it
    is input-only and cannot be lit. Confirm that here rather than wondering
    later why it never responds.
    """
    clear(out)

    print("Track buttons (red), accumulating left to right...")
    for i, note in enumerate(TRACK_BUTTONS):
        button(out, note, 1)
        print(f"  track {i+1}  (note 0x{note:02X})")
        wait("next", 0.5)

    print("\nScene launch buttons (green), accumulating top to bottom...")
    for i, note in enumerate(SCENE_BUTTONS):
        button(out, note, 1)
        print(f"  scene {i+1}  (note 0x{note:02X})")
        wait("next", 0.5)

    print("\nVelocity 2 -- all sixteen should blink...")
    for note in list(TRACK_BUTTONS) + list(SCENE_BUTTONS):
        button(out, note, 2)
    wait("blinking?")

    print("\nVelocity 3-127 should behave the same as 1 (solid on)...")
    for note in list(TRACK_BUTTONS) + list(SCENE_BUTTONS):
        button(out, note, 127)
    wait("solid again?")

    print("\nNow trying to light SHIFT (0x7A). Expect nothing to happen --")
    print("it has no LED. If it does light, the doc is wrong.")
    button(out, 0x7A, 1)
    wait("anything?")


def test_sysex(out):
    """Arbitrary RGB via SysEx -- not limited to the 128 palette entries."""
    clear_grid(out)
    print("SysEx RGB: a red-to-blue ramp across the 64 pads.")
    print("These are true 24-bit colours, not palette indices.\n")
    for note in GRID:
        t = note / 63
        sysex_rgb(out, note, note, int(255 * (1 - t)), 0, int(255 * t))
        time.sleep(0.01)
    wait("ramp look right?")
    print("Whole grid to one colour in a single message (pads 0-63)...")
    sysex_rgb(out, 0, 63, 255, 140, 0)
    print("Done. Ctrl-C to clear and exit.")
    idle()


def idle():
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass


TESTS = {
    "names": test_names,
    "rgbtest": test_rgbtest,
    "contrast": test_contrast,
    "verify": test_verify,
    "layout": test_layout,
    "palette": test_palette,
    "behaviour": test_behaviour,
    "behavior": test_behaviour,
    "buttons": test_buttons,
    "sysex": test_sysex,
}


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    global AUTO
    AUTO = "--auto" in args

    port_name = None
    if "--port" in args:
        index = args.index("--port")
        if index + 1 >= len(args):
            sys.exit("--port needs a MIDI output name")
        port_name = args[index + 1]
        del args[index:index + 2]

    known_flags = {"--auto"}
    unknown = [a for a in args if a.startswith("-") and a not in known_flags]
    if unknown:
        sys.exit(f"Unknown option: {unknown[0]}\n"
                 "  Options here are --auto and --port.\n"
                 "  --feedback belongs to controller.py, not this script.")
    args = [a for a in args if not a.startswith("-")]

    if len(args) > 1:
        sys.exit(f"Unexpected argument '{args[1]}'.\n"
                 "  To choose a MIDI port use: --port \"NAME\"")

    mode = args[0]
    if mode not in TESTS and mode != "off":
        sys.exit(f"Unknown mode '{mode}'. "
                 f"Try: {', '.join(sorted(set(TESTS)))}, off")
    port_name = port_name or find_port()

    with mido.open_output(port_name) as out:
        print(f"Port: {port_name}\n")
        try:
            if mode == "off":
                clear(out)
                print("Cleared.")
                return
            if mode not in TESTS:
                sys.exit(f"Unknown mode '{mode}'. "
                         f"Try: {', '.join(sorted(set(TESTS)))}, off")
            TESTS[mode](out)
        except KeyboardInterrupt:
            pass
        finally:
            if mode != "off":
                print("\nClearing...")
                clear(out)


if __name__ == "__main__":
    main()
