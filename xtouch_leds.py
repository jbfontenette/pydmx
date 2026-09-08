#!/usr/bin/env python3
"""
Behringer X-Touch Mini LED output test.

    pip install mido python-rtmidi

    python3 xtouch_leds.py scan        # walk the note range, find the buttons
    python3 xtouch_leds.py layers      # does an LED reach the inactive layer?
    python3 xtouch_leds.py states 8    # what velocities does note 8 accept?
    python3 xtouch_leds.py rings       # walk the CC range, find the LED rings
    python3 xtouch_leds.py ring 1      # sweep every value on CC 1
    python3 xtouch_leds.py off         # everything dark

Options: --auto (run on timers), --port "NAME" (pick the MIDI output).
NOTE: --learn, --encoders and --list belong to xtouch_dump.py, not here.

Like xtouch_dump.py, this assumes NOTHING. apc_leds.py can drive the APC
because the APC's LED scheme was established first -- Note On where the
channel is the behaviour and the velocity is the colour. The equivalent for
this device is unknown, so these modes sweep and let you watch, rather than
claiming to light a particular control.

What to write down while running it:

  scan    which physical button lights for which note number
  states  which velocities mean off, on, and blink -- there may be more
  rings   which CC drives which encoder's LED ring
  ring    what the ring does across 0-127: a position, a fan, a pan, a
          count -- the X-Touch rings are documented to have several display
          modes, and this is how you see which value selects which

Run xtouch_dump.py --learn first. Knowing the note a button SENDS usually
tells you the note it LISTENS on, and this confirms it.

IF NOTHING LIGHTS AT ALL, in order of likelihood:

  1. Wrong MIDI channel. Everything here goes to channel 10 because that is
     what the device sends on; 'scan-channels' sweeps the other fifteen.
  2. The button's LED is set to local control. On this device each button
     can be configured to light itself when pressed rather than obey
     incoming MIDI, and that is set in Behringer's X-Touch Editor, not over
     MIDI. A button in local mode will ignore everything sent here.
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
from xtouch_dump import CHANNEL

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
    """Everything off. Broad on purpose -- we do not know the real ranges."""
    for number in range(128):
        note(out, number, 0)
    for control in range(128):
        cc(out, control, 0)


# --- discovery modes ------------------------------------------------------

def test_scan(out):
    """One note at a time, so you can see which control answers."""
    print("Lighting one note at a time. Note which button responds.\n")
    for number in range(0, 48):
        note(out, number, 127)
        print(f"  note {number:<3} on")
        wait(f"note {number} -- Enter for next")
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
    """Does an LED sent to one layer show while the other layer is active?

    ANSWERED 2026-09-08: no, and it is not stored either. A note sent for
    the inactive layer is discarded -- nothing lights at the time, and
    nothing appears on switching. So the controller must send the ACTIVE
    layer's number, and since the device never announces the layer, it has
    to infer it from incoming notes and repaint on a change.

    Kept as the reproduction. Watch the SAME physical button throughout;
    the expected answers are now yes, no, no.
    """
    a_note, b_note = 8, 32          # "button top 1" on each layer
    print(f"Sending on MIDI channel {CHANNEL}, which is where the device")
    print("sends. Put it on LAYER A and watch the top-left button.\n")
    wait("on layer A, Enter to light the layer A note")

    note(out, a_note, 127)
    print(f"  note {a_note} (layer A) lit -- is the button on?")
    print("  If NOTHING lights here, stop: the layer question is moot until")
    print("  basic LED output works. Try 'scan-channels', and see the note")
    print("  about X-Touch Editor at the top of this file.")
    wait("Enter")
    note(out, a_note, 0)

    note(out, b_note, 127)
    print(f"  note {b_note} (layer B) lit -- while still on layer A.")
    print("  Does anything light? It should NOT, if state is per layer.")
    wait("Enter")

    print("\n  Now switch to LAYER B, leaving that note lit.")
    wait("switched to B, Enter")
    print(f"  note {b_note} was set while you were on A. Is it lit now?")
    print("  If yes, the device holds per-layer LED state and the controller")
    print("  can light both numbers blindly. If no, it must track the layer.")
    wait("Enter")
    note(out, b_note, 0)


def test_rings(out):
    """Which CC drives which encoder ring."""
    print("Setting one CC at a time to a mid value. Note which ring lights.\n")
    for control in range(0, 32):
        cc(out, control, 64)
        print(f"  CC {control:<3} = 64")
        wait(f"CC {control} -- Enter for next")
        cc(out, control, 0)


def test_ring(out, control):
    """Sweep one CC to see what the ring does across the whole range."""
    print(f"Sweeping CC {control} through 0-127.\n")
    for value in (0, 1, 2, 3, 6, 11, 16, 27, 32, 43, 48, 59, 64,
                  75, 80, 91, 96, 107, 112, 123, 127):
        cc(out, control, value)
        print(f"  CC {control} = {value:<3}")
        wait(f"value {value} -- Enter for next")
    cc(out, control, 0)


TESTS = {
    "layers": test_layers,
    "scan": test_scan,
    "scan-channels": test_scan_channels,
    "rings": test_rings,
}
TAKES_NUMBER = {"states": test_states, "ring": test_ring}


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
    if mode not in TESTS and mode not in TAKES_NUMBER and mode != "off":
        sys.exit(f"Unknown mode '{mode}'. Try: "
                 f"{', '.join(sorted(set(TESTS) | set(TAKES_NUMBER)))}, off")

    number = None
    if mode in TAKES_NUMBER:
        if len(args) < 2:
            sys.exit(f"'{mode}' needs a number: python3 xtouch_leds.py "
                     f"{mode} 8")
        try:
            number = int(args[1])
        except ValueError:
            sys.exit(f"'{args[1]}' is not a number.")
        if not 0 <= number <= 127:
            sys.exit(f"{number} is out of range 0-127.")

    port_name = port_name or find_port()
    with _open("out", port_name) as out:
        print(f"Port: {port_name}\n")
        try:
            if mode == "off":
                clear(out)
                print("Cleared.")
                return
            if mode in TAKES_NUMBER:
                TAKES_NUMBER[mode](out, number)
            else:
                TESTS[mode](out)
        except KeyboardInterrupt:
            pass
        finally:
            if mode != "off":
                print("\nClearing...")
                clear(out)


if __name__ == "__main__":
    main()
