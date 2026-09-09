#!/usr/bin/env python3
"""
Behringer X-Touch Mini LED output test.

    pip install mido python-rtmidi

    python3 xtouch_leds.py scan        # walk the button LEDs, both layers
    python3 xtouch_leds.py press 8     # does pressing a lit button darken it?
    python3 xtouch_leds.py rings       # which CC drives which encoder ring
    python3 xtouch_leds.py ring 11     # sweep the values of one ring CC
    python3 xtouch_leds.py seed 1      # can the host SET an encoder's value?
    python3 xtouch_leds.py states 8    # what velocities does an LED accept?
    python3 xtouch_leds.py layers      # does an LED reach the inactive layer?
    python3 xtouch_leds.py scan-channels   # sweep the other MIDI channels
    python3 xtouch_leds.py off         # everything dark

Options: --auto (run on timers), --port "NAME" (pick the MIDI output).
NOTE: --learn, --encoders and --list belong to xtouch_dump.py, not here.

THE DEVICE LISTENS WHERE IT SPEAKS. To drive a control, send the number
that control SENDS on the layer currently showing -- notes 8-23 and CC 1-8
on layer A, notes 32-47 and CC 11-18 on layer B. The other layer's numbers
are discarded, not stored. One rule, input and output, buttons and rings,
measured on both layers.

It matters because Behringer's X-Touch Editor says something else entirely:
notes 0-15 for the LEDs, program change for the layer, a behaviour CC and a
value CC per ring. None of that is true of this unit in Standard mode; all
three were tested and none worked. The numbers live in xtouch_constants.py
and are imported, so this file cannot drift from them.

EVERY MODE HERE HAS BEEN RUN AND ANSWERED; see xtouch_dump.py for the map
they produced. They stay because the answers are the driver's foundation and
a firmware or mode change could move any of them -- and because two of them
only gave the right answer on the second attempt, so the wrong way to run
each one is written into its docstring.

The one thing still worth looking at rather than re-confirming is 'ring':
what a value DRAWS -- a single travelling dot, or a fill from one end. That
style is a device-side setting per encoder per layer, made in X-Touch Editor
and not reachable over MIDI, so the mode reports how the unit is configured
rather than testing the protocol. Pan and tilt want the dot; a level wants
the fill.

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

# The map comes from xtouch_constants.py rather than being repeated here. It
# was repeated here once -- the channel, as 0 instead of 10 -- and every LED
# test silently did nothing for a whole session. Same duplication-drift as
# REVIEW item 16, committed twice in one project.
from xtouch_constants import (CHANNEL, LED_NOTE, RING_CC, RING_CC_CANDIDATES,
                              name_for)

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


def test_press(out, number):
    """Does the device change a lamp by itself when the button is pressed?

    ANSWERED on the first run of the driver: yes. A button lights while held
    and goes dark on release, of the device's own accord -- the controller
    sends one Note On and never turns it off, and the lamp goes out anyway.

    Which matters more than it looks, because of the diff cache every
    surface here keeps: the controller would still believe the lamp is lit,
    so it would never re-send, and a scene could run all night behind a dark
    button. xtouch.py re-asserts the value on release for exactly this.

    Kept to re-check after any X-Touch Editor change. The editor can
    reportedly set a button's behaviour to Toggle, which may hand the lamp
    to the host outright and make the re-assert unnecessary -- it would
    still be harmless, and the driver does not depend on it either way.
    """
    print(f"Note {number}. Run 'scan' first if you do not know which one.\n")
    note_on = number
    note(out, note_on, 127)
    print("  Lit. It should be ON now, with nothing touching it.")
    wait("is it lit? -- Enter")

    print("\n  Now PRESS AND HOLD that button.")
    wait("holding -- Enter")
    print("  Lit while held? That is the device lighting itself.")
    wait("Enter")

    print("\n  Now RELEASE it.")
    wait("released -- Enter")
    print("  If it is now DARK, the device turned off a lamp the host set,")
    print("  and any diff-based LED cache is stale from this moment on.")
    print("  If it is still lit, this device does not do that -- write that")
    print("  down, because xtouch.py works around it on every release.")
    wait("Enter")
    note(out, note_on, 0)


def test_seed(out, index):
    """Does writing a ring also set the encoder's POSITION, or only its lamps?

    THE QUESTION THIS ANSWERS is what to do about the startup jump. This
    device cannot be asked where its encoders are sitting -- there is no
    Introduction message here, the way the APC has one -- so the controller
    starts not knowing, and the first touch of a `scale` encoder slams the
    group from full down to wherever the knob happens to be.

    If a ring write moves the device's own idea of the value, the fix is
    exact: at startup, push the show's value out to each bound encoder, and
    the knob IS where the show is. If it only lights LEDs, the software has
    to do soft takeover instead -- ignore the encoder until it passes
    through the value already in force.

    SELF-CALIBRATING, because the first version was not and told us nothing.
    It wrote layer A's ring CC and listened for layer A's encoder CC, so on
    layer B the ring did not move AND every message was filtered away: two
    symptoms, one cause, and no data either way. It now works out the layer
    from what the encoder sends, and reports every CC that arrives rather
    than only the one it expected.

    Needs the input port too, which is why this is the one mode here that
    opens both.
    """
    import xtouch_dump

    port_name = xtouch_dump.find_port()
    print(f"Encoder {index}. Watching '{port_name}'.\n")
    print("Either layer is fine -- which one you are on is worked out from")
    print("what the encoder sends. Do not press LAYER while this runs.\n")

    with _open("in", port_name) as inp:

        def turned(prompt):
            """Drain, prompt, then report every CC that arrived."""
            for _ in inp.iter_pending():
                pass
            wait(prompt)
            seen = [(m.control, m.value) for m in inp.iter_pending()
                    if m.type == "control_change"]
            for control, value in seen:
                print(f"    CC {control} = {value}")
            if not seen:
                print("    nothing arrived")
            return seen

        seen = turned(f"turn encoder {index} a few clicks RIGHT -- Enter")
        if not seen:
            print("\n  No CC at all. That is the encoder not reaching this")
            print("  script, which is not an answer to the question -- check")
            print("  the port and that MC MODE is off, then run it again.")
            return

        control, before = seen[-1]
        layer = "A" if control in RING_CC["A"] else "B"
        print(f"\n  Encoder on layer {layer}, CC {control}, now at {before}.")

        # Far from where it is sitting, so a move cannot be mistaken for the
        # knob's own position, and away from the ends where a ring at 0 or
        # 127 looks the same as one that ignored the write.
        target = 10 if before > 64 else 110
        cc(out, control, target)
        print(f"\n  Ring set to {target}. Does the RING itself show that?")
        print("  (a dot or a fill about that far round, depending on the")
        print("  display style set for this encoder in X-Touch Editor)")
        wait("Enter")

        seen = turned("now turn ONE CLICK RIGHT -- Enter")
        if not seen:
            print("\n  Nothing arrived, so this run says nothing either way.")
            return
        after = seen[-1][1]
        print(f"\n  Now at {after}.")
        if abs(after - target) <= 3:
            print(f"  It followed the write ({target}): the host CAN set an")
            print("  encoder's position. The controller can seed every bound")
            print("  encoder at startup and there is no jump, ever.")
        elif abs(after - before) <= 3:
            print(f"  It carried on from {before} and ignored the write: the")
            print("  position is the device's alone. Soft takeover is then")
            print("  the only fix -- ignore the encoder until it passes")
            print("  through the value already in force.")
        else:
            print("  Neither. Write down what you saw rather than concluding")
            print("  from it, and run it once more.")


def test_layers(out):
    """Does the device remember LED state across a layer switch?

    ANSWERED on 2026-09-08: yes, and independently per layer. Note 8 lit on
    layer A survived a switch to B and back, and layer B showed nothing in
    between -- including the note 32 sent while A was showing, which was
    dropped at the moment it was sent rather than stored for later.

    So the device holds two LED surfaces and shows one. The driver keeps a
    desired and a delivered state per layer, writes only to the layer that
    is showing, and flushes the difference when a layer appears. What it
    must NOT do is repaint everything on a switch, which is what would be
    right if the device forgot.

    Kept for re-testing after a firmware change, since the whole paint
    policy rests on it. The switch is by HAND: program change does not move
    this device.
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
    """Which CC block drives the rings, and does it follow the layer?

    ANSWERED on 2026-09-08, on both layers: a ring listens on the CC its own
    encoder transmits. Layer A showing, CC 1-8 move rings 1-8 and CC 11-18
    do nothing; layer B showing, the two swap. CC 21-28 is dead either way.
    The same rule as the buttons, which is what makes the surface one map
    instead of two.

    Kept because it is the test that answers it, and because a run on one
    layer proves nothing -- the first attempt looked like "there is a single
    ring block at 11-18" purely because the other block had only ever been
    set to 1, which is a ring's minimum and indistinguishable from its
    resting state. Everything here is set to 64 for that reason.
    """
    print("WHICH LAYER LAMP IS LIT? Write it down -- the answer is")
    print("meaningless without it. Do not press LAYER while this runs.\n")
    print("Expected: the showing layer's block moves rings 1-8 in order,")
    print("the other block does nothing at all.\n")
    wait("layer noted -- Enter to start")

    for block in RING_CC_CANDIDATES:
        owner = [layer for layer, ccs in RING_CC.items() if ccs == block]
        label = f"layer {owner[0]} encoders" if owner else "not a ring block"
        print(f"\n--- CC {block.start}-{block.stop - 1} ({label}) ---")
        for control in block:
            cc(out, control, 64)
            print(f"  CC {control} = 64")
            wait(f"CC {control}: which ring moved? -- Enter for next")
            cc(out, control, 0)
    print("\nRun it again on the other layer. The live block should follow.")


def test_ring(out, control):
    """Sweep one ring CC through its values.

    Takes a CC rather than an encoder number because an encoder has two,
    one per layer, and which of them is live depends on what the device is
    showing -- naming the encoder would hide exactly the thing that has to
    be got right.

    What it is for now that the addressing is settled: seeing what a value
    DRAWS. A single travelling dot, a fill from one end, a fan from the
    centre. That style is configured per encoder per layer in X-Touch
    Editor and cannot be set over MIDI, so this reports how the unit is set
    up rather than testing the protocol. Pan and tilt want the dot; a level
    wants the fill.
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
def _numbers(*ranges):
    return sorted(set().union(*(set(r) for r in ranges)))


# Modes taking a number, and which numbers actually mean something. A range
# is not enough: the ring CCs are 1-8 and 11-18, and 9 and 10 in the gap are
# the FADERS. Accepting the span would let a typo move a fader instead of a
# ring, which looks like nothing happening and reads as a dead protocol --
# the failure this whole file exists to stop making.
TAKES_NUMBER = {
    "states": (test_states, _numbers(LED_NOTE["A"], LED_NOTE["B"]),
               "a button LED note"),
    "press": (test_press, _numbers(LED_NOTE["A"], LED_NOTE["B"]),
              "a button LED note"),
    "ring": (test_ring, _numbers(RING_CC["A"], RING_CC["B"]), "a ring CC"),
    "seed": (test_seed, list(range(1, 9)), "an encoder 1-8"),
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
        _, allowed, what = TAKES_NUMBER[mode]
        listed = ", ".join(str(n) for n in allowed)
        if len(args) < 2:
            sys.exit(f"'{mode}' needs {what}: python3 xtouch_leds.py "
                     f"{mode} {allowed[0]}\n  One of: {listed}")
        try:
            argument = int(args[1])
        except ValueError:
            sys.exit(f"'{args[1]}' is not a number.")
        if argument not in allowed:
            sys.exit(f"{argument} is not {what}.\n  One of: {listed}")

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
