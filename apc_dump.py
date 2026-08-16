#!/usr/bin/env python3
"""
APC mini mk2 MIDI input monitor.

    pip install mido python-rtmidi

    python3 apc_dump.py                 # scrolling log, with fader deltas
    python3 apc_dump.py --grid          # live view of the whole surface
    python3 apc_dump.py --list          # show all MIDI ports
    python3 apc_dump.py "APC mini mk2"  # open a named port

The APC mini mk2 is USB class-compliant -- no driver needed on macOS.
"""

import sys
import time

import mido

# --- control map, confirmed against hardware -------------------------------
GRID = range(0x00, 0x40)            # 0-63,    RGB pads, note 0 = bottom-left
TRACK_BUTTONS = range(0x64, 0x6C)   # 100-107, red LED
SCENE_BUTTONS = range(0x70, 0x78)   # 112-119, green LED
SHIFT = 0x7A                        # 122,     no LED
FADER_CC = range(0x30, 0x39)        # 48-56,   absolute position, 0-127


def find_port():
    names = mido.get_input_names()
    if not names:
        sys.exit("No MIDI inputs found. Is the APC plugged in?")
    for name in names:
        if "apc" in name.lower():
            return name
    print("No port with 'APC' in the name. Available inputs:")
    for name in names:
        print(f"  {name}")
    sys.exit("Pass one as an argument.")


def describe(msg, prev_fader):
    """Decode a message into something readable."""
    if msg.type in ("note_on", "note_off"):
        note = msg.note
        # note_on with velocity 0 is the conventional note-off
        action = "press" if (msg.type == "note_on" and msg.velocity > 0) else "release"

        if note in GRID:
            # Origin is bottom-left. note = row * 8 + col, row 0 = BOTTOM.
            row, col = divmod(note, 8)
            where = f"GRID pad {note} (r{row} c{col})"
        elif note in TRACK_BUTTONS:
            where = f"TRACK button {note - 0x64 + 1}"
        elif note in SCENE_BUTTONS:
            where = f"SCENE LAUNCH {note - 0x70 + 1}"
        elif note == SHIFT:
            where = "SHIFT"
        else:
            where = f"unknown note {note}"

        return f"{where:<24} {action:<8} vel={msg.velocity:<3} ch={msg.channel}"

    if msg.type == "control_change":
        if msg.control in FADER_CC:
            # Fader 9 is "master" only by Ableton convention. The hardware
            # sends absolute position on CC 0x38 exactly like the other eight
            # and does not scale them. It is master iff your code makes it so.
            n = msg.control - 0x30 + 1
            bar = "#" * round(msg.value / 127 * 20)
            last = prev_fader.get(msg.control)
            delta = "" if last is None else f" d{msg.value - last:+d}"
            return (f"FADER {n:<18} {msg.value:>3} "
                    f"|{bar:<20}|{delta}")
        return f"CC {msg.control:<21} {msg.value:>3} ch={msg.channel}"

    if msg.type == "sysex":
        hexed = " ".join(f"{b:02X}" for b in msg.data)
        return f"SYSEX                    F0 {hexed} F7"

    return str(msg)


def render(held, faders):
    """Redraw the surface.

    Row 7 prints first because the terminal draws top-down while the pad
    numbering runs bottom-up -- note 0 is the BOTTOM-left pad, so the visual
    top row is notes 56-63. Scene Launch 1 is the topmost of the right-hand
    column, so it sits beside row 7.
    """
    out = ["\x1b[H\x1b[2J", "  APC mini mk2 -- live view (Ctrl-C to stop)\n"]

    for row in range(7, -1, -1):
        cells = "".join("[##]" if row * 8 + c in held else "[  ]"
                        for c in range(8))
        scene_note = 0x70 + (7 - row)
        scene = "(**)" if scene_note in held else "(  )"
        out.append(f"  r{row} {cells}  {scene} S{7 - row + 1}"
                   f"   {row*8:>2}-{row*8+7}")

    track = "".join("[##]" if 0x64 + c in held else "[  ]" for c in range(8))
    shift = "[SHIFT]" if SHIFT in held else "[     ]"
    out.append(f"\n      {track}  {shift}")
    out.append("       T1  T2  T3  T4  T5  T6  T7  T8\n")

    for i in range(9):
        v = faders.get(0x30 + i, 0)
        filled = round(v / 127 * 24)
        label = f"F{i+1}" if i < 8 else "F9"
        out.append(f"  {label:<3} {v:>3} |" + "#" * filled
                   + " " * (24 - filled) + "|")

    print("\n".join(out), flush=True)


def run_grid(port_name):
    held, faders = set(), {}
    render(held, faders)
    watched = set(GRID) | set(TRACK_BUTTONS) | set(SCENE_BUTTONS) | {SHIFT}

    with mido.open_input(port_name) as port:
        for msg in port:
            if msg.type in ("note_on", "note_off") and msg.note in watched:
                if msg.type == "note_on" and msg.velocity > 0:
                    held.add(msg.note)
                else:
                    held.discard(msg.note)
            elif msg.type == "control_change" and msg.control in FADER_CC:
                faders[msg.control] = msg.value
            else:
                continue
            render(held, faders)


def summarise(seen, fader_stats):
    print("\n\n--- controls seen this session ---")
    notes = sorted(k[1] for k in seen if k[0] == "note")
    ccs = sorted(k[1] for k in seen if k[0] == "cc")
    if notes:
        missing = sorted(set(GRID) | set(TRACK_BUTTONS)
                         | set(SCENE_BUTTONS) | {SHIFT} - set(notes))
        print(f"Notes ({len(notes)}): {notes}")
        if missing:
            print(f"Not yet pressed: {missing}")
    if ccs:
        print(f"CCs ({len(ccs)}):   {ccs}")

    if fader_stats:
        print("\n--- fader resolution ---")
        for cc in sorted(fader_stats):
            st = fader_stats[cc]
            n = cc - 0x30 + 1
            steps = st["steps"]
            ones = steps.count(1)
            verdict = ("clean 1-step" if steps and max(steps) == 1
                       else f"max jump {max(steps)}" if steps else "-")
            print(f"  F{n} (CC{cc})  range {st['min']:>3}..{st['max']:<3}  "
                  f"{len(steps):>4} moves, {ones} of them 1-step  -> {verdict}")
        print("\nJumps bigger than 1 usually just mean you moved it fast.")
        print("Move one fader very slowly end to end for a true reading.")

    if not seen:
        print("Nothing received. Wrong port, or the APC is in a mode that")
        print("routes to a different port -- try --list.")


def main():
    args = sys.argv[1:]
    if "--list" in args:
        print("Inputs:")
        for name in mido.get_input_names():
            print(f"  {name}")
        print("\nOutputs:")
        for name in mido.get_output_names():
            print(f"  {name}")
        return

    grid_mode = "--grid" in args
    args = [a for a in args if not a.startswith("--")]
    port_name = args[0] if args else find_port()

    if grid_mode:
        try:
            run_grid(port_name)
        except KeyboardInterrupt:
            print("\nStopped.")
        return

    print(f"Listening on: {port_name}")
    print("Press pads and buttons, move faders. Ctrl-C to stop.\n")

    seen, fader_stats, prev_fader = {}, {}, {}
    try:
        with mido.open_input(port_name) as port:
            for msg in port:
                if msg.type in ("note_on", "note_off"):
                    seen[("note", msg.note)] = seen.get(("note", msg.note), 0) + 1
                elif msg.type == "control_change":
                    seen[("cc", msg.control)] = seen.get(("cc", msg.control), 0) + 1
                    if msg.control in FADER_CC:
                        st = fader_stats.setdefault(
                            msg.control, {"min": 127, "max": 0, "steps": []})
                        st["min"] = min(st["min"], msg.value)
                        st["max"] = max(st["max"], msg.value)
                        last = prev_fader.get(msg.control)
                        if last is not None and last != msg.value:
                            st["steps"].append(abs(msg.value - last))

                print(f"{time.strftime('%H:%M:%S')}  {describe(msg, prev_fader)}")

                if msg.type == "control_change":
                    prev_fader[msg.control] = msg.value

    except KeyboardInterrupt:
        summarise(seen, fader_stats)


if __name__ == "__main__":
    main()
