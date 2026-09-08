#!/usr/bin/env python3
"""
Behringer X-Touch Mini LED output test.

    pip install mido python-rtmidi

    python3 xtouch_leds.py scan        # which LED note lights which button
    python3 xtouch_leds.py layer b     # select a layer (Program Change)
    python3 xtouch_leds.py layers      # does an LED reach the inactive layer?
    python3 xtouch_leds.py states 0    # what velocities does an LED accept?
    python3 xtouch_leds.py rings       # which ring belongs to which encoder
    python3 xtouch_leds.py ring 1      # every display mode and value, ring 1
    python3 xtouch_leds.py off         # everything dark

Options: --auto (run on timers), --port "NAME" (pick the MIDI output).
NOTE: --learn, --encoders and --list belong to xtouch_dump.py, not here.

WHAT THE DEVICE LISTENS ON is not what it sends. Behringer's X-Touch Editor
states it plainly, and it caught this script out twice:

    button LEDs     NOTE 0-15        but buttons SEND notes 8-23
    ring behaviour  CC 1-8           how a ring displays its value
    ring value      CC 9-16          the value itself
    layer select    Program Change 0 / 1
    mode select     CC 127, value 0 Standard / 1 MC

So a ring needs TWO messages, and an LED note is NOT the note the button
under it sends. The ranges live in xtouch_dump.py; this file imports them
rather than repeating them, because repeating the MIDI channel here once
already cost a whole hardware session.

What to write down while running it:

  scan    which physical button lights for which note -- the point of the
          mode. Row and position, not just "one of them lit".
  states  which velocities mean off, on, and blink. The earlier run said
          "every velocity is plain on", but it was sending note 8 believing
          that was the top-left button, and per the editor it is not. That
          measurement is suspect until this is redone on a note the manual
          actually assigns.
  rings   which CC pair drives which encoder's ring
  ring    what each behaviour value does -- the editor calls the modes
          single, pan, fan and spread, without saying which number is which

Run xtouch_dump.py --learn first, for the input side.

IF NOTHING LIGHTS AT ALL, in order of likelihood:

  1. Wrong MIDI channel. Everything here goes to channel 10 because that is
     what the device sends on; 'scan-channels' sweeps the other fifteen.
  2. The button's LED is set to local control. On this device each button
     can be configured to light itself when pressed rather than obey
     incoming MIDI, and that is set in X-Touch Editor, not over MIDI. A
     button in local mode will ignore everything sent here.
  3. MC MODE. The photos show it off, which is what makes the encoders
     absolute -- but the LED protocol may differ between the two modes, so
     it is worth knowing which one you are testing under.
"""

import sys
import time

# The channel comes from the map in xtouch_dump.py rather than being repeated
# here. It was repeated here once, as 0, and every LED test silently did
# nothing for it -- the same duplication-drift that REVIEW item 16 is about,
# committed twice in one project.
from xtouch_dump import (CHANNEL, LAYER_PROGRAM, LED_NOTE, RING_MODES,
                         ring_ccs)

AUTO = False


def wait(prompt="Enter for next", seconds=1.2):
    if AUTO:
        time.sleep(seconds)
        return
    try:
        input(f"  [{prompt}] ")
    except (EOFError, KeyboardInterrupt):
        raise KeyboardInterrupt


def _ports(direction):
    """Port names, or a plain message when the MIDI backend is missing.

    "pip install mido python-rtmidi" is two packages and forgetting the
    second is easy; mido then raises from inside a lazy backend import, deep
    in a traceback that says nothing useful about what to do.
    """
    import mido
    try:
        return (mido.get_input_names() if direction == "in"
                else mido.get_output_names())
    except (ImportError, OSError) as exc:
        sys.exit(f"No working MIDI backend: {exc}\n"
                 f"  mido needs one: pip install python-rtmidi")


def _open(direction, name):
    """Open a port, reporting a missing backend or a bad name plainly."""
    import mido
    try:
        return (mido.open_input(name) if direction == "in"
                else mido.open_output(name))
    except (ImportError, OSError) as exc:
        sys.exit(f"Cannot open '{name}': {exc}\n"
                 f"  Check the name with --list, and that "
                 f"python-rtmidi is installed.")


def find_port():
    names = _ports("out")
    if not names:
        sys.exit("No MIDI outputs found. Is the X-Touch plugged in?")
    for name in names:
        if "x-touch" in name.lower() or "xtouch" in name.lower():
            return name
    print("No port with 'X-Touch' in the name. Available outputs:")
    for name in names:
        print(f"  {name}")
    sys.exit("Pass one with --port \"NAME\".")


def note(out, number, velocity=127, channel=CHANNEL):
    import mido
    out.send(mido.Message("note_on", note=number, velocity=velocity,
                          channel=channel))


def cc(out, control, value, channel=CHANNEL):
    import mido
    out.send(mido.Message("control_change", control=control, value=value,
                          channel=channel))


def clear(out):
    """Everything off, on both layers.

    Broader than the documented ranges on purpose: notes 16-31 and CCs above
    16 are not supposed to do anything, but this is a discovery tool and
    anything it managed to light it should be able to put out.

    CC 127 is the one exception. It is the mode switch -- value 0 is
    Standard, 1 is MC -- so sending it "off" would silently reconfigure the
    device rather than clear an LED.
    """
    for layer in ("A", "B"):
        select_layer(out, layer)
        for number in range(48):
            note(out, number, 0)
        for control in range(127):          # not 127 itself: mode switch
            cc(out, control, 0)
    select_layer(out, "A")


def select_layer(out, layer):
    """Switch the device to layer A or B, from the controller side.

    Program Change, per the editor. Worth having on its own because it
    settles the question the input map could not: the device is silent when
    the user presses LAYER, but nothing stops the controller from deciding
    which layer is showing.
    """
    import mido
    out.send(mido.Message("program_change",
                          program=LAYER_PROGRAM[layer], channel=CHANNEL))


# --- discovery modes ------------------------------------------------------

def test_scan(out):
    """Which physical button each LED note lights.

    The whole point of the mode, and it has to be run before anything else
    is believed: the editor says LEDs listen on notes 0-15 while the buttons
    SEND 8-23, so the obvious assumption -- light a button with the note it
    sends -- is wrong for eight of the sixteen and off by a row for the
    rest. Only the device can say which way round it is.

    The walk runs past 15 deliberately. 16-31 should do nothing at all; if
    they light the second row, the documented range is wrong and the whole
    map needs re-reading rather than patching.
    """
    print("Lighting one note at a time.\n")
    print("Write down WHICH BUTTON lights -- top or bottom row, and how far")
    print("from the left. 'one of them lit' answers nothing.\n")
    print(f"Notes {LED_NOTE.start}-{LED_NOTE.stop - 1} are the documented")
    print("button LEDs. The rest are a check that nothing lies past them.\n")
    for number in range(0, 32):
        if number == LED_NOTE.stop:
            print("\n--- past the documented range; expect nothing below ---\n")
        note(out, number, 127)
        print(f"  note {number:<3} on")
        wait(f"note {number}: which button? -- Enter for next")
        note(out, number, 0)
    print("\nNothing lit at all? The device may want a different channel;")
    print("try 'scan-channels' to sweep those instead.")


def test_scan_channels(out):
    """Same note across all sixteen MIDI channels."""
    number = 8
    print(f"Sending note {number} on each channel in turn.\n")
    for channel in range(16):
        note(out, number, 127, channel)
        print(f"  note {number} ch{channel} on")
        wait(f"channel {channel} -- Enter for next")
        note(out, number, 0, channel)


def test_states(out, number):
    """What each Note On velocity does to a button LED.

    Every value is measured from OFF, which the first version did not do --
    it walked the velocities in sequence, so once one of them latched the LED
    on, every later reading said "on" whether that velocity did anything or
    not. Published notes for this device say velocities above 2 are IGNORED,
    which is exactly the case that mistake cannot see: an ignored value
    leaves the LED however the previous one left it.

    So: off, then the value, then look. Three distinct answers are possible
    and they mean different things --

        stays off   the velocity is ignored
        comes on    steady
        blinks      watch for a few seconds; a slow blink read as "on" is
                    the other way this test goes wrong
    """
    print(f"Note {number}. Each velocity is set from OFF, so what you see is")
    print("what THAT value does -- not what an earlier one left behind.\n")
    print("Answer each with: off (ignored), on, or blink.")
    print("Give it a couple of seconds before deciding -- a slow blink looks")
    print("like plain 'on' if you only glance.\n")
    for velocity in (1, 2, 3, 4, 5, 6, 15, 63, 64, 127):
        note(out, number, 0)
        time.sleep(0.25)
        note(out, number, velocity)
        print(f"  velocity {velocity:<3}  off -> {velocity}")
        wait(f"velocity {velocity}: off / on / blink? -- Enter for next",
             seconds=3.0)
    note(out, number, 0)


def test_layers(out):
    """Do button LEDs have per-layer state, and can the controller switch?

    Rewritten. The first version sent notes 8 and 32, on the assumption that
    LED numbers mirrored input numbers (+24 for layer B). The editor says
    they do not: LEDs listen on notes 0-15 and there is no second range at
    all. So the earlier answer -- "a note for the inactive layer is
    discarded" -- was measuring note 32, which is not an LED note on either
    layer, and proved nothing about layers.

    The real question is now smaller and sharper: with ONE note range for
    sixteen buttons, does the device keep a separate LED state per layer, or
    is the LED surface shared and only the button NUMBERS change?

      shared   the controller repaints on every layer change, because what
               a button means changed even though its lamp did not
      per-layer  it can paint both and let the device show the right one

    Run 'scan' first. This test lights note 0, and until scan says which
    button that is, "nothing lit" and "something lit somewhere you were not
    looking" are the same observation.
    """
    led = LED_NOTE.start
    print(f"Sending on MIDI channel {CHANNEL}. Note {led} -- which by 'scan'")
    print("should be a button you have already identified.\n")

    select_layer(out, "A")
    wait("on layer A, Enter to light it")
    note(out, led, 127)
    print(f"  note {led} lit on layer A. Is a button on?")
    print("  If NOTHING lights here, stop: the layer question is moot until")
    print("  basic LED output works. Try 'scan-channels', and see the note")
    print("  about X-Touch Editor at the top of this file.")
    wait("Enter")

    print("\n  Switching to layer B by program change, leaving it lit.")
    select_layer(out, "B")
    print("  Did the LAYER lamp move? And is the button still lit?")
    print("  still lit  -> the LED surface is shared across layers")
    print("  went dark  -> the device keeps LED state per layer")
    wait("Enter")

    note(out, led, 0)
    note(out, led + 1, 127)
    print(f"\n  note {led} off, note {led + 1} on, while on layer B.")
    print("  Now switch BACK to A and see what layer A remembers.")
    select_layer(out, "A")
    wait("Enter")
    print(f"  If note {led} is lit again, state is per layer and the two")
    print("  surfaces are independent. If you see the layer B picture, it")
    print("  is one shared surface and the controller owns every repaint.")
    wait("Enter")
    note(out, led, 0)
    note(out, led + 1, 0)


def test_rings(out):
    """Which CC pair drives which encoder's ring.

    Two messages per ring, not one. The first version of this mode set a
    single CC to 64 and waited for a ring to show half -- which it never
    could, because CC 1-8 select the DISPLAY MODE and only CC 9-16 carry a
    value. Sending the value alone leaves the ring in whatever mode it was
    already in; sending the mode alone gives it nothing to draw.
    """
    print("Each ring gets a behaviour and then a value. Note which ring.\n")
    for index in range(1, 9):
        behaviour_cc, value_cc = ring_ccs(index)
        cc(out, behaviour_cc, 1)
        cc(out, value_cc, 64)
        print(f"  encoder {index}: CC {behaviour_cc} = 1 (mode), "
              f"CC {value_cc} = 64 (value)")
        wait(f"encoder {index}: which ring lit? -- Enter for next")
        cc(out, value_cc, 0)


def test_ring(out, index):
    """One ring: every display mode, then a value sweep in each.

    The editor names four modes -- single, pan, fan, spread -- without
    saying which number selects which, so the numbers are walked and you
    say what you see. For a pan/tilt encoder the useful answer is which mode
    draws a single dot that tracks the value; for a level, the one that
    fills from one end.
    """
    behaviour_cc, value_cc = ring_ccs(index)
    print(f"Encoder {index}: mode on CC {behaviour_cc}, "
          f"value on CC {value_cc}.\n")
    print("Modes the editor names, in no known order: "
          f"{', '.join(RING_MODES)}.\n")

    for behaviour in range(0, 5):
        cc(out, behaviour_cc, behaviour)
        cc(out, value_cc, 64)
        print(f"  mode {behaviour}, value 64 -- dot, fan, fill, or nothing?")
        wait(f"mode {behaviour} -- Enter to sweep it", seconds=2.0)
        for value in (0, 16, 32, 48, 64, 80, 96, 112, 127):
            cc(out, value_cc, value)
            print(f"    value {value:<3}")
            wait(f"mode {behaviour} value {value} -- Enter", seconds=0.5)
        cc(out, value_cc, 0)
    cc(out, behaviour_cc, 0)


def test_layer(out, which):
    """Select a layer with Program Change, and see whether it takes.

    This is the mode that decides how the driver handles layers. If the
    device obeys, the controller owns the layer: it can set it at startup,
    know it without guessing, and only has to watch incoming notes to notice
    a switch made by hand. If it does not, the earlier conclusion stands and
    the layer can only ever be inferred.
    """
    print(f"Selecting layer {which} (program change "
          f"{LAYER_PROGRAM[which]} on channel {CHANNEL}).\n")
    print("Watch the LAYER A / LAYER B lamps on the device.")
    select_layer(out, which)
    wait(f"did it switch to {which}? -- Enter")
    print("\nIf the lamp moved, the controller can drive the layer and does")
    print("not have to infer it. If it did not, note that here and in")
    print("xtouch_dump.py: the editor would then be describing MC mode only.")


TESTS = {
    "layers": test_layers,
    "scan": test_scan,
    "scan-channels": test_scan_channels,
    "rings": test_rings,
}
# Modes taking a number, and the range that number may take. 'ring' is 1-8
# because it names an ENCODER, not a CC -- a ring is two CCs and picking one
# of them is what the old design got wrong.
TAKES_NUMBER = {"states": (test_states, LED_NOTE.start, 127),
                "ring": (test_ring, 1, 8)}
TAKES_LAYER = {"layer": test_layer}


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

    unknown = [a for a in args if a.startswith("-") and a != "--auto"]
    if unknown:
        hint = ""
        if unknown[0] in ("--learn", "--encoders", "--list"):
            hint = (f"\n  {unknown[0]} belongs to xtouch_dump.py, not this "
                    f"script:\n      python3 xtouch_dump.py {unknown[0]}")
        sys.exit(f"Unknown option: {unknown[0]}\n"
                 f"  Options here are --auto and --port.{hint}")
    args = [a for a in args if not a.startswith("-")]

    mode = args[0]
    known = set(TESTS) | set(TAKES_NUMBER) | set(TAKES_LAYER) | {"off"}
    if mode not in known:
        sys.exit(f"Unknown mode '{mode}'. Try: {', '.join(sorted(known))}")

    argument = None
    if mode in TAKES_NUMBER:
        _, low, high = TAKES_NUMBER[mode]
        if len(args) < 2:
            sys.exit(f"'{mode}' needs a number {low}-{high}: "
                     f"python3 xtouch_leds.py {mode} {low}")
        try:
            argument = int(args[1])
        except ValueError:
            sys.exit(f"'{args[1]}' is not a number.")
        # Each mode has its own range, and they are not the same: 'ring'
        # names an encoder 1-8 while 'states' names an LED note. Accepting
        # 0-127 for both let 'ring 16' through, which sent a value to a CC
        # that means something else entirely.
        if not low <= argument <= high:
            sys.exit(f"{argument} is out of range {low}-{high} for "
                     f"'{mode}'.")
    elif mode in TAKES_LAYER:
        argument = (args[1] if len(args) > 1 else "").strip().upper()
        if argument not in LAYER_PROGRAM:
            sys.exit(f"'{mode}' needs a layer: python3 xtouch_leds.py "
                     f"{mode} a")

    port_name = port_name or find_port()
    with _open("out", port_name) as out:
        print(f"Port: {port_name}\n")
        try:
            if mode == "off":
                clear(out)
                print("Cleared.")
                return
            if mode in TAKES_NUMBER:
                TAKES_NUMBER[mode][0](out, argument)
            elif mode in TAKES_LAYER:
                TAKES_LAYER[mode](out, argument)
            else:
                TESTS[mode](out)
        except KeyboardInterrupt:
            pass
        finally:
            # 'layer' is the exception: it is meant to LEAVE the device on
            # the layer you asked for, and clear() ends on A.
            if mode not in ("off", "layer"):
                print("\nClearing...")
                clear(out)


if __name__ == "__main__":
    main()
