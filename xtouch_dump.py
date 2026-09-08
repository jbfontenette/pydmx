#!/usr/bin/env python3
"""
Behringer X-Touch Mini MIDI input monitor and control-map learner.

    pip install mido python-rtmidi

    python3 xtouch_dump.py --list         # show all MIDI ports
    python3 xtouch_dump.py                # scrolling log of everything
    python3 xtouch_dump.py --learn        # guided: build the control map
    python3 xtouch_dump.py --encoders     # absolute or relative? find out
    python3 xtouch_dump.py "X-TOUCH MINI" # open a named port

THE MAP IS ESTABLISHED. It lives in xtouch_constants.py and this script is
what established it, control by control on both layers. The three questions
it existed to answer are answered:

  1. The encoders send an ABSOLUTE position, not a relative delta -- the
     unit is in Standard mode, and MC MODE is what would change that. So
     encoders reuse controller.py's existing fader path and need no binding
     kind of their own. Use --encoders to re-check after any mode change.

  2. The layer button sends NOTHING. The device switches internally and
     starts sending the other set of numbers, so the controller needs no
     page state machine for input -- though its LED output does have to know
     which layer is showing. Use --learn, which walks both layers and diffs
     them.

  3. Buttons are notes 8-23 and encoder pushes 0-7 on layer A, both +24 on
     layer B; encoders are CC 1-8 and CC 11-18, with the two faders at CC 9
     and CC 10 in between. Use --learn.

It stays because a firmware or mode change could move any of it, and because
the wrong way to run each mode is written into that mode's docstring.
"""

import sys
import time

# mido is imported inside the functions that need it, so the classifier below
# can be imported and tested with nothing installed.

# The control map lives in xtouch_constants.py -- one copy, shared with
# xtouch_leds.py and the driver. It was in this file first, which is how
# xtouch_leds.py came to keep its own copy of the MIDI channel, as 0 instead
# of 10, and silently light nothing for a whole session.
from xtouch_constants import (               # noqa: F401 -- re-exported
    CHANNEL, ENCODER_PUSH, BUTTONS_TOP, BUTTONS_BOTTOM, ENCODER_CC, FADER_CC,
    LED_NOTE, RING_CC, RING_CC_CANDIDATES, led_note, name_for, ring_cc,
)



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
    names = _ports("in")
    if not names:
        sys.exit("No MIDI inputs found. Is the X-Touch plugged in?")
    for name in names:
        if "x-touch" in name.lower() or "xtouch" in name.lower():
            return name
    print("No port with 'X-Touch' in the name. Available inputs:")
    for name in names:
        print(f"  {name}")
    sys.exit("Pass one as an argument.")


def _label(kind, number):
    known = name_for(kind, number)
    return f"{known[0]} [{known[1]}]" if known else "?"


def describe(msg):
    """Decode a message, naming the control where the map knows it."""
    if msg.type in ("note_on", "note_off"):
        action = ("press" if msg.type == "note_on" and msg.velocity > 0
                  else "release")
        return (f"NOTE {msg.note:<3} {action:<8} "
                f"vel={msg.velocity:<3} ch={msg.channel}  "
                f"{_label('note', msg.note)}")
    if msg.type == "control_change":
        return (f"CC   {msg.control:<3} {'':8} "
                f"val={msg.value:<3} ch={msg.channel}  "
                f"{_label('cc', msg.control)}")
    if msg.type == "pitchwheel":
        return f"PITCH    {msg.pitch:>6}      ch={msg.channel}"
    if msg.type == "sysex":
        return "SYSEX    " + " ".join(f"{b:02X}" for b in msg.data)
    return str(msg)


# --- the question that everything else forks on ---------------------------

def classify_encoder(values):
    """Guess how an encoder reports movement, from the CC values it sent.

    Returns (verdict, explanation). Pure function, so it can be tested
    without a device on the bench.

    An ABSOLUTE encoder behaves like a fader: it walks the whole 0-127 range
    and stops at the ends. A RELATIVE one sends the same one or two values
    over and over however far you turn, because each message means "one step
    that way" rather than "here is where I am".

    The three relative encodings differ in how they say "left":

      two's complement  right 1..63,  left 127 counting down
      signed bit        right 1..63,  left 65..127  (bit 0x40 = direction)
      binary offset     right 65..,   left ..63     (centred on 64)

    Two's complement and signed bit are IDENTICAL for single steps -- both
    send 1 and 127 for one click each way -- so a slow turn cannot tell them
    apart. Turning fast is what separates them, and this says so rather than
    guessing.
    """
    values = [v for v in values if v is not None]
    if len(values) < 4:
        return "unknown", "Not enough movement -- turn it more."

    distinct = sorted(set(values))
    span = distinct[-1] - distinct[0]
    # How often the same value came back. A relative encoder repeats a tiny
    # set forever; a position sweep mostly visits each value once, and that
    # is what separates a half-turn of an absolute encoder from a burst of
    # relative steps that happen to sit either side of 64.
    repeats = len(values) / len(distinct)

    # Absolute: many different values, spread widely. A relative encoder
    # never produces this no matter how long you turn.
    if len(distinct) > 12 and span > 40:
        return "absolute", (
            f"{len(distinct)} distinct values spanning {distinct[0]}.."
            f"{distinct[-1]}. It reports position, like a fader -- so it can "
            f"reuse apply_fader() in controller.py unchanged.")

    low = [v for v in distinct if 1 <= v <= 63]
    high = [v for v in distinct if 65 <= v <= 127]

    if distinct and max(distinct) >= 120 and low:
        detail = ("Right sends small values, left sends large ones near 127. "
                  "That is two's complement OR signed bit -- identical for "
                  "single clicks.")
        if any(2 <= v <= 63 for v in low) and any(66 <= v <= 126 for v in high):
            detail += (" Both mid-ranges appeared, which points at signed bit;"
                       " confirm by turning fast and watching whether left "
                       "counts DOWN from 127 (two's complement) or UP from 65"
                       " (signed bit).")
        else:
            detail += " Turn it fast in each direction to separate them."
        return "relative", detail

    if (low and high and repeats >= 2
            and 60 <= sum(distinct) / len(distinct) <= 68):
        return "relative", (
            "Values sit either side of 64 and keep repeating, so this is "
            "binary offset: 65+ is one way, 63- is the other, and 64 means "
            "no movement.")

    # A partial turn: not the full sweep the first test wants, but plainly a
    # position all the same -- many distinct values, each visited about once.
    # A relative encoder cannot look like this however far it turns, because
    # it only ever has a handful of values to send.
    if len(distinct) > 8 and repeats < 2 and span > 15:
        return "absolute", (
            f"{len(distinct)} distinct values over {distinct[0]}.."
            f"{distinct[-1]}, barely repeating. That is a position being "
            f"reported, not steps -- so it can reuse apply_fader() in "
            f"controller.py unchanged. Turn it end to end to see the full "
            f"0-127 range.")

    if repeats < 1.5 and len(distinct) >= 6:
        return "absolute", (
            f"Values barely repeat ({len(values)} messages, "
            f"{len(distinct)} distinct) which is what a position sweep looks "
            f"like -- but it only covered {distinct[0]}..{distinct[-1]}. Turn "
            f"it further end to end to be sure.")

    if len(distinct) <= 4:
        return "relative", (
            f"Only {len(distinct)} distinct values ({distinct}) however far "
            f"it turned, so it is sending steps rather than a position. "
            f"Encoding unclear -- turn further in both directions.")

    return "unknown", f"Distinct values seen: {distinct}"


def run_encoders(port_name):
    """Turn one encoder, get an answer about the whole encoder design."""
    import mido
    print(f"Listening on: {port_name}\n")
    print("Turn ONE encoder slowly right for a couple of seconds, then")
    print("slowly left, then give it a fast spin each way. Ctrl-C when done.\n")

    seen = {}
    try:
        with _open("in", port_name) as port:
            for msg in port:
                if msg.type != "control_change":
                    continue
                seen.setdefault(msg.control, []).append(msg.value)
                values = seen[msg.control]
                print(f"  CC {msg.control:<3} val={msg.value:<3} "
                      f"({len(values)} messages)", end="\r", flush=True)
    except KeyboardInterrupt:
        pass

    if not seen:
        print("\nNothing received. Wrong port, or the encoders are not")
        print("sending CC -- run without --encoders to see raw messages.")
        return

    print("\n\n--- encoder verdict ---")
    for control in sorted(seen):
        values = seen[control]
        verdict, detail = classify_encoder(values)
        print(f"\nCC {control}  ({len(values)} messages)")
        print(f"  -> {verdict.upper()}")
        print(f"     {detail}")
        distinct = sorted(set(values))
        shown = distinct if len(distinct) <= 24 else (
            distinct[:12] + ["..."] + distinct[-12:])
        print(f"     values: {shown}")


# --- guided map building --------------------------------------------------

def checklist():
    """What to ask for, in the order the prompts run.

    Named the way name_for() names things, so the prompt and the echoed
    answer speak the same language. "button 9" was ambiguous -- the whole
    point of the walk is to remove that ambiguity, not add to it.
    """
    items = [f"button top {n}" for n in range(1, 9)]
    items += [f"button bottom {n}" for n in range(1, 9)]
    items += [f"encoder {n} PUSH" for n in range(1, 9)]
    items += [f"encoder {n} turn" for n in range(1, 9)]
    items += ["fader", "layer button"]
    return items


def collect_until_enter(port, prompt):
    """Show what arrives while waiting for Enter, then hand it back.

    The first version simply blocked on input() and drained the port
    afterwards. That works -- the port buffers while the prompt waits -- but
    it looks broken: you press a button, nothing prints, and there is no way
    to tell whether the tool heard you or whether the whole thing is dead.
    Someone at a bench should not have to take that on faith.

    So poll stdin alongside the port and echo each message as it lands.
    Falls back to plain input() where stdin cannot be polled, which keeps
    the old behaviour rather than failing.
    """
    try:
        import select
        select.select([sys.stdin], [], [], 0)
    except Exception:
        input(prompt)
        return list(port.iter_pending())

    sys.stdout.write(prompt)
    sys.stdout.flush()
    seen = []
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], 0.05)
        for msg in port.iter_pending():
            seen.append(msg)
            print(f"\n    heard  {describe(msg)}")
            sys.stdout.write(prompt)
            sys.stdout.flush()
        if ready:
            if sys.stdin.readline() == "":
                raise EOFError
            return seen


def learn_layer(port, label):
    """Prompt through every control, reading what each one sends.

    Messages arrive while the prompt waits and the port buffers them, so
    pressing the control and THEN confirming is what makes this work without
    threads. collect_until_enter echoes them as they land so the waiting
    does not look like nothing happening.
    """
    found = {}
    print(f"\n=== layer {label} ===")
    print("The 16 buttons are two rows of 8 below the encoders. Rows are")
    print("counted left to right, and 'top' is the row nearer the encoders.")
    print("Encoders are numbered left to right too. If the echoed name does")
    print("not match the control in your hand, THAT is the finding -- the")
    print("map's row and order are inferred, not yet confirmed.\n")
    print("Press or move each control -- what it sends is echoed as it")
    print("arrives -- then hit Enter to move on.")
    print("Blank Enter with nothing touched = skip. Ctrl-C = stop early.\n")

    for item in checklist():
        try:
            messages = collect_until_enter(port, f"  {item:<20} > ")
        except (EOFError, KeyboardInterrupt):
            print("\n  (stopped early)")
            break
        if not messages:
            continue
        first = messages[0]
        if first.type in ("note_on", "note_off"):
            found[item] = ("note", first.note, first.channel)
        elif first.type == "control_change":
            found[item] = ("cc", first.control, first.channel)
        else:
            found[item] = (first.type, None, getattr(first, "channel", None))
        kind, number, channel = found[item]
        extra = "" if len(messages) == 1 else f"  (+{len(messages)-1} more)"
        print(f"  {'':20}   {kind} {number} ch{channel}{extra}")
    return found


def report_layers(a, b):
    print("\n\n--- control map ---")
    print(f"  {'control':<20} {'layer A':<16} {'layer B':<16} same?")
    for item in checklist():
        left = a.get(item)
        right = b.get(item)
        if not left and not right:
            continue
        fmt = lambda e: "-" if not e else f"{e[0]} {e[1]} ch{e[2]}"
        same = "yes" if left == right else "NO"
        print(f"  {item:<20} {fmt(left):<16} {fmt(right):<16} {same}")

    if not b:
        print("\nOnly one layer captured, so nothing to compare.")
        return

    differing = [i for i in checklist()
                 if i in a and i in b and a[i] != b[i]]
    print()
    if differing:
        print(f"{len(differing)} of {len(set(a) & set(b))} shared controls "
              f"send DIFFERENT numbers on layer B.")
        print("The device handles the layer itself: mapping.csv needs to")
        print("address both sets, and the controller needs no page logic.")
    else:
        print("Every control sends the SAME numbers on both layers.")
        print("The device does not distinguish them over MIDI, so a layer")
        print("would have to be tracked in software -- watch what the layer")
        print("button itself sends, above.")


def run_learn(port_name):
    import mido
    print(f"Listening on: {port_name}")
    with _open("in", port_name) as port:
        list(port.iter_pending())            # discard anything already queued
        layer_a = learn_layer(port, "A")

        print("\nNow switch the device to layer B, then press Enter.")
        print("(Blank Enter to skip layer B entirely.)")
        try:
            answer = input("  > ")
        except (EOFError, KeyboardInterrupt):
            answer = "skip"
        layer_b = {}
        if answer.strip().lower() not in ("skip", "s", "n", "no"):
            list(port.iter_pending())
            layer_b = learn_layer(port, "B")

    report_layers(layer_a, layer_b)


# --- raw log --------------------------------------------------------------

def summarise(seen):
    print("\n\n--- controls seen this session ---")
    notes = sorted(n for kind, n in seen if kind == "note")
    ccs = sorted(n for kind, n in seen if kind == "cc")
    if notes:
        print(f"Notes ({len(notes)}): {notes}")
    if ccs:
        print(f"CCs   ({len(ccs)}): {ccs}")
    if not seen:
        print("Nothing received. Wrong port, or the device is in a mode that")
        print("routes elsewhere -- try --list.")
        return
    print("\nRun --learn to attach names to these, and --encoders to find")
    print("out whether the encoders send positions or steps.")


def run_log(port_name):
    import mido
    print(f"Listening on: {port_name}")
    print("Press buttons, push and turn encoders, move the fader, and press")
    print("the layer button. Ctrl-C to stop.\n")

    seen = {}
    try:
        with _open("in", port_name) as port:
            for msg in port:
                if msg.type in ("note_on", "note_off"):
                    key = ("note", msg.note)
                elif msg.type == "control_change":
                    key = ("cc", msg.control)
                else:
                    key = (msg.type, None)
                seen[key] = seen.get(key, 0) + 1
                print(f"{time.strftime('%H:%M:%S')}  {describe(msg)}")
    except KeyboardInterrupt:
        summarise(seen)


def main():
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return

    if "--list" in args:
        print("Inputs:")
        for name in _ports("in"):
            print(f"  {name}")
        print("\nOutputs:")
        for name in _ports("out"):
            print(f"  {name}")
        return

    mode = ("learn" if "--learn" in args else
            "encoders" if "--encoders" in args else "log")
    known = {"--learn", "--encoders", "--list"}
    unknown = [a for a in args if a.startswith("-") and a not in known]
    if unknown:
        sys.exit(f"Unknown option: {unknown[0]}\n"
                 "  Options here are --list, --learn and --encoders.")

    args = [a for a in args if not a.startswith("-")]
    port_name = args[0] if args else find_port()

    if mode == "learn":
        run_learn(port_name)
    elif mode == "encoders":
        run_encoders(port_name)
    else:
        run_log(port_name)


if __name__ == "__main__":
    main()
