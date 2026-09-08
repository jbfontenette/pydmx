#!/usr/bin/env python3
"""
Behringer X-Touch Mini MIDI input monitor and control-map learner.

    pip install mido python-rtmidi

    python3 xtouch_dump.py --list         # show all MIDI ports
    python3 xtouch_dump.py                # scrolling log of everything
    python3 xtouch_dump.py --learn        # guided: build the control map
    python3 xtouch_dump.py --encoders     # absolute or relative? find out
    python3 xtouch_dump.py "X-TOUCH MINI" # open a named port

NOTHING ABOUT THIS DEVICE IS CONFIRMED YET. apc_dump.py can decode the APC
because apc.py's control map was established against hardware first; this
script is how the equivalent gets established for the X-Touch, so it
deliberately assumes nothing and reports raw MIDI. What it learns belongs in
the driver's header the way apc.py carries the APC's map.

Three questions it exists to answer, in rough order of how much depends on
them:

  1. Do the encoders send an ABSOLUTE position (0-127, like a fader) or a
     RELATIVE delta? The device can be configured either way, and the answer
     decides whether encoders can reuse the existing fader path in
     controller.py or need a binding kind of their own. Use --encoders.

  2. What does the A/B layer button do? If the device swaps the note and CC
     numbers itself, the controller never needs a page state machine for it.
     Use --learn, which walks both layers and diffs them.

  3. Which notes are the 16 buttons and the 8 encoder pushes, and which CCs
     are the 8 encoders and the fader? Use --learn.
"""

import sys
import time

# mido is imported inside the functions that need it, so the classifier below
# can be imported and tested with nothing installed.

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
# So the controller cannot paint blindly: to light a button it must send the
# ACTIVE layer's note number, and the device never says which layer that is.
# The only source of that knowledge is input -- an arriving note below 24 is
# layer A, 24 and above is layer B. Which means:
#
#   * Track a believed layer, update it from every incoming note, and on a
#     change drop the LED cache and repaint the whole surface. That is the
#     refresh() pattern apc.py already has, and a layer switch is a
#     deliberate gesture rather than a per-frame event, so the cost is fine.
#   * Painting BOTH numbers for a control does not help. The inactive one is
#     dropped, so the other layer is still stale when it becomes active.
#   * At startup the layer is unknown, and after a switch made without
#     touching anything the surface stays dark until the first press tells
#     the controller where it is. Self-correcting, but visible: a switch
#     mid-set means one dark gesture. Invariant 10 says fail safe on unknown
#     state, and dark-until-touched is the safe direction.
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
