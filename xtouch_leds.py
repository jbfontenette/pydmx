#!/usr/bin/env python3
"""
Behringer X-Touch Mini LED output test.

    pip install mido python-rtmidi

    python3 xtouch_leds.py scan        # walk the button LEDs, both layers
    python3 xtouch_leds.py rings       # which CC drives which encoder ring
    python3 xtouch_leds.py ring 11     # sweep the values of one ring CC
    python3 xtouch_leds.py states 8    # what velocities does an LED accept?
    python3 xtouch_leds.py layers      # does an LED reach the inactive layer?
    python3 xtouch_leds.py off         # everything dark

Options: --auto (run on timers), --port "NAME" (pick the MIDI output).
NOTE: --learn, --encoders and --list belong to xtouch_dump.py, not here.

THE DEVICE LISTENS WHERE IT SPEAKS. To light a button, send the note that
button sends: 8-23 on layer A, 32-47 on layer B. Measured with 'scan', and
it matters because Behringer's X-Touch Editor says something else entirely
-- notes 0-15 for the LEDs, program change for the layer, a separate
behaviour and value CC per ring. None of that is true of this unit in
Standard mode; all three were tested and none worked. The ranges live in
xtouch_dump.py and are imported, so this file cannot drift from them.

Still open, and the reason 'rings' exists: which CC block drives the encoder
rings. CC 11-16 moved rings 1-6, but the layer A encoder CCs were only ever
set to 1 -- a ring's minimum, indistinguishable from its resting state -- so
that block has never really been tried.

What to write down while running it:

  rings   which CC moved which ring, AND which layer lamp was lit at the
          time. Without the second half the answer is ambiguous between
          "each layer has its own ring CCs" and "there is one ring block".
  ring    what a value looks like -- a single dot that tracks, or a fill
          from one end. Pan/tilt wants the first, a level the second.
  states  which velocities mean off, on, and blink.

Run xtouch_dump.py --learn first, for the input side.

IF NOTHING LIGHTS AT ALL, in order of likelihood:

  1. Wrong MIDI channel. Everything here goes to channel 10 because that is
     what the device sends on; 'scan-channels' sweeps the other fifteen.
  2. The wrong layer. A note for the inactive layer is discarded, not
     stored, so half the numbers here do nothing at any given moment.
  3. MC MODE. The photos show it off, which is what makes the encoders
     absolute -- but the LED protocol may differ between the two modes.
"""

import sys
import time

# The channel comes from the map in xtouch_dump.py rather than being repeated
# here. It was repeated here once, as 0, and every LED test silently did
# nothing for it -- the same duplication-drift that REVIEW item 16 is about,
# committed twice in one project.
from xtouch_dump import CHANNEL, LED_NOTE, RING_CC_CANDIDATES, name_for

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

    Broader than the measured ranges on purpose: this is a discovery tool,
    and anything it managed to light it should be able to put out. It cannot
    reach the inactive layer -- those notes are discarded rather than stored
    -- so a layer switch may reveal something still lit. Run it again there.

    CC 127 is the one exception. It is the Standard/MC mode switch, so
    sending it "off" would reconfigure the device rather than clear an LED,
    and MC mode is what makes the encoders relative.
    """
    for number in range(48):
        note(out, number, 0)
    for control in range(127):              # not 127 itself: mode switch
        cc(out, control, 0)


# --- discovery modes ------------------------------------------------------

def test_scan(out):
    """Which physical button each note lights.

    ANSWERED on 2026-09-08 and kept for the layer B half and for re-testing
    after any firmware or mode change. Notes 8-15 light the top row left to
    right, 16-23 the bottom row; 0-7 and 24-31 light nothing, being the
    encoder pushes. Which is to say the device listens where it speaks --
    the LED note is the note the button sends.

    That is worth re-running rather than assuming, because Behringer's
    editor says LEDs listen on notes 0-15 and this walk is what proved
    otherwise. The expected label is printed with each note, so a
    disagreement shows up as you go instead of in the analysis afterwards.
    """
    print("Lighting one note at a time.\n")
    print("Write down WHICH BUTTON lights -- row and position. The expected")
    print("answer is printed alongside; say so if the device disagrees.\n")
    for number in range(0, 48):
        known = name_for("note", number)
        expect = f"{known[0]} (layer {known[1]})" if known else "nothing"
        note(out, number, 127)
        print(f"  note {number:<3} on   expect: {expect}")
        wait(f"note {number}: which button? -- Enter for next")
        note(out, number, 0)
    print("\nNotes for the INACTIVE layer light nothing, so half of these")
    print("are expected to be dark. Switch layer by hand and run it again.")
    print("Nothing at all? Try 'scan-channels'.")


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
    """Does the device remember LED state across a layer switch?

    The addressing question is settled: each layer has its own note range
    (8-23 and 32-47), and a note for the INACTIVE layer is discarded rather
    than stored. What is left is what happens to a lamp that was already
    lit when you switch away and back -- and that decides how much the
    driver has to repaint.

      remembered   the controller paints each layer once and the device
                   shows the right picture on its own
      forgotten    every layer switch means a full repaint, which is the
                   refresh() pattern apc.py already has

    The switch is by HAND: program change does not move this device, so the
    prompts ask you to press LAYER yourself.
    """
    lit, other = LED_NOTE["A"].start, LED_NOTE["B"].start
    print(f"Notes {lit} (layer A button 1) and {other} (layer B button 1).\n")
    print("Start on LAYER A.")
    wait("on layer A -- Enter")

    note(out, lit, 127)
    print(f"  note {lit} sent. Top-left button should be lit.")
    print("  If it is NOT, stop -- basic output is broken, not layers.")
    wait("Enter")

    note(out, other, 127)
    print(f"\n  note {other} sent while on layer A. Expect no change:")
    print("  the inactive layer's notes are discarded, not queued.")
    wait("Enter")

    print("\n  Now press LAYER to switch to B.")
    wait("switched to B -- Enter")
    print("  Anything lit? If button 1 is on, the note sent while A was")
    print("  showing was stored after all, which contradicts the earlier")
    print("  measurement and needs writing down.")
    wait("Enter")

    print("\n  Press LAYER again, back to A.")
    wait("back on A -- Enter")
    print(f"  Is note {lit} still lit? Lit means LED state survives the")
    print("  switch; dark means the controller repaints on every change.")
    wait("Enter")
    note(out, lit, 0)
    note(out, other, 0)


def test_rings(out):
    """Which CC block drives the encoder rings.

    The open question, and the reason this walks three blocks instead of
    assuming one. What is known: 64 on CC 11-16 moved rings 1-6. What is
    not: whether CC 1-8 does the same on layer A. The previous run set that
    block to 1, which is a ring's minimum and looks exactly like its resting
    state, so it has never actually been tried.

    Two readings still fit. Either a ring answers its own encoder's
    transmit CC -- 1-8 on layer A, 11-18 on layer B, which is how the
    BUTTONS behave -- or there is a single ring block at 11-18 whichever
    layer is showing. Which one holds decides whether the driver has to
    know the layer to move a ring.

    So the layer matters as much as the CC, and the test asks for it: run
    it once on A and once on B, and note the lamp both times.
    """
    print("WHICH LAYER LAMP IS LIT? Write it down -- the answer is")
    print("meaningless without it. Do not press LAYER while this runs.\n")
    wait("layer noted -- Enter to start")

    for block in RING_CC_CANDIDATES:
        print(f"\n--- CC {block.start}-{block.stop - 1} ---")
        for control in block:
            cc(out, control, 64)
            print(f"  CC {control} = 64")
            wait(f"CC {control}: which ring moved? -- Enter for next")
            cc(out, control, 0)
    print("\nNow press LAYER and run it again. If the live block MOVES with")
    print("the layer, rings work like buttons. If it stays put, there is one")
    print("ring block and the driver need not know the layer to use it.")


def test_ring(out, control):
    """Sweep one ring CC through its values.

    Takes a CC, not an encoder number: until 'rings' says which block is
    live, an encoder does not have one CC that can be named. It is also
    what makes the useful observation possible -- whether a value draws a
    single dot that tracks it, or a fill from one end. Pan and tilt want the
    dot; a level wants the fill.
    """
    print(f"CC {control}, ramping. Watch what the ring DRAWS, not just")
    print("whether it moves: one dot, a fill, or a fan from the centre.\n")
    for value in (0, 1, 16, 32, 64, 96, 127):
        cc(out, control, value)
        print(f"  value {value:<3}")
        wait(f"value {value} -- Enter for next", seconds=0.8)
    cc(out, control, 0)


TESTS = {
    "layers": test_layers,
    "scan": test_scan,
    "scan-channels": test_scan_channels,
    "rings": test_rings,
}
# Modes taking a number, and the range that number may take. They are not
# the same range: 'states' names a button LED note, 'ring' a CC in one of
# the candidate ring blocks. Accepting 0-127 for both let 'ring 16' through
# when ring was still numbered by encoder, and it wrote to a real CC.
TAKES_NUMBER = {
    "states": (test_states, LED_NOTE["A"].start, LED_NOTE["B"].stop - 1),
    "ring": (test_ring, RING_CC_CANDIDATES[0].start,
             RING_CC_CANDIDATES[-1].stop - 1),
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
    known = set(TESTS) | set(TAKES_NUMBER) | {"off"}
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
