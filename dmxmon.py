#!/usr/bin/env python3
"""Live view of patched DMX channels. Standalone -- no hardware needed.

    # terminal 1
    python3 controller.py --monitor --no-dmx     # or with the adapter
    # terminal 2
    python3 dmxmon.py

    python3 dmxmon.py --port 9001    # match controller's --monitor 9001
    python3 dmxmon.py --all          # include unpatched non-zero channels
    python3 dmxmon.py --swatches     # check your terminal renders colours
    python3 dmxmon.py --plain        # no colour, if your terminal mangles it

Reads the patch from show/ purely for labels, and the values from the UDP
tap. It never opens the serial port, so it cannot conflict with the
controller -- you can start and stop it freely mid-show.

Only patched channels are shown. A 512-row table is unreadable, and anything
outside the patch is noise you did not put there on purpose.
"""

import os
import sys
import time

import colours
import monitor
import showfile

SHOW_DIR = "show"
BAR_WIDTH = 24

# Truecolor gives exact RGB; xterm-256 is the fallback for terminals without
# it (macOS Terminal.app among them). iTerm2, WezTerm, Ghostty, Kitty and
# VS Code all advertise COLORTERM and get the exact values.
TRUECOLOR = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
PLAIN = False

LED_ON = "\u25cf"      # filled circle -- reads as a lit LED
LED_OFF = "\u25cb"     # hollow -- a named value that is not a colour

CLEAR = "\x1b[H\x1b[2J"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"
HILITE = "\x1b[7m"


def build_rows(patch):
    """One row per patched channel: (channel, fixture, Feature).

    Sorted by DMX channel rather than by fixture, so the display mirrors the
    universe. A gap in the numbers is then visibly a gap in the patch.
    """
    rows = []
    for fixture in patch.fixtures.values():
        for feature in fixture.profile.features.values():
            rows.append((fixture.address + feature.offset - 1,
                         fixture.name, feature))
    rows.sort(key=lambda r: r[0])
    return rows


def to_256(r, g, b):
    """Nearest xterm-256 index. Greys use the 24-step ramp, which is much
    closer than anything in the 6x6x6 colour cube."""
    if max(r, g, b) - min(r, g, b) < 12:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + round((r - 8) / 247 * 24)
    return (16 + 36 * round(r / 255 * 5)
            + 6 * round(g / 255 * 5) + round(b / 255 * 5))


def led(label):
    """Coloured glyph for a named value, or a blank of the same width.

    Driven by whether the NAME is a known colour, not by the channel being
    called 'colors'. So 'red' lights up wherever it appears, and 'slow' or
    'on' correctly do not. Same table that colours the APC pads, so the two
    can never disagree.
    """
    if label is None:
        return " "
    name = colours.find(label)
    if name is None:
        return f"{DIM}{LED_OFF}{RESET}"
    if PLAIN:
        return LED_ON
    r, g, b = colours.rgb(name)
    if TRUECOLOR:
        return f"\x1b[38;2;{r};{g};{b}m{LED_ON}{RESET}"
    return f"\x1b[38;5;{to_256(r, g, b)}m{LED_ON}{RESET}"


def show_swatches():
    """Print all sixteen so you can check what your terminal actually does."""
    mode = ("truecolor" if TRUECOLOR else "xterm-256 fallback")
    print(f"\n  Terminal colour mode: {mode}")
    if not TRUECOLOR:
        print(f"  {DIM}COLORTERM is not set to truecolor. Colours are "
              f"approximated.{RESET}")
        print(f"  {DIM}iTerm2, WezTerm, Ghostty and Kitty all support "
              f"exact RGB.{RESET}")
    print()
    for name in colours.ORDER:
        hexv = colours.NAMES[name][0]
        print(f"    {led(name)}  {name:<12} #{hexv}")
    print(f"\n  {DIM}Any that look wrong here will look wrong in the "
          f"monitor too.{RESET}\n")


def bar(value, mode):
    """Snap channels deliberately get no bar.

    A bar says "this is a level". On a colour wheel or gobo channel the value
    is an index into a lookup table, and drawing it as 40%% full would be
    actively misleading. Those get a marker instead.
    """
    if mode == showfile.SNAP:
        return f"{DIM}snap{RESET}"
    filled = round(value / 255 * BAR_WIDTH)
    return ("#" * filled) + (DIM + "." * (BAR_WIDTH - filled) + RESET)


def render(rows, frame, stats, changed, show_all):
    out = [CLEAR]
    live = stats["fps"] > 0
    status = (f"{stats['fps']:.0f} fps" if live
              else f"{DIM}waiting for frames...{RESET}")
    out.append(f"  {BOLD}DMX monitor{RESET}   {stats['addr']}   {status}"
               f"   {len(rows)} patched channels\n")

    current = None
    for channel, fixture, feature in rows:
        if fixture != current:
            current = fixture
            out.append(f"  {BOLD}{fixture}{RESET}")
        value = frame[channel] if frame else 0
        mark = HILITE if channel in changed else ""
        end = RESET if mark else ""

        # The plain-text name is the point of the display: "42" tells you
        # nothing, "green" tells you what the fixture is actually doing.
        # An unnamed value inside a feature that HAS names is worth
        # flagging -- it usually means a scene wrote a number by hand that
        # falls between the documented bands.
        name = feature.label(value)
        if name:
            tag = f"  {led(name)} {BOLD}{name}{RESET}"
        elif feature.values:
            tag = f"    {DIM}(unnamed){RESET}"
        else:
            tag = ""

        out.append(f"    {DIM}{channel:>3}{RESET}  {feature.name:<12} "
                   f"{mark}{value:>3}{end}  {bar(value, feature.mode)}{tag}")

    if show_all and frame:
        patched = {r[0] for r in rows}
        extra = [(c, frame[c]) for c in range(1, 513)
                 if c not in patched and frame[c]]
        if extra:
            out.append(f"\n  {BOLD}Unpatched but non-zero{RESET}"
                       f"  {DIM}(nothing should be here){RESET}")
            for channel, value in extra:
                out.append(f"    {DIM}{channel:>3}{RESET}  {'?':<12} {value:>3}")

    out.append(f"\n  {DIM}Ctrl-C to stop{RESET}")
    print("\n".join(out), flush=True)


def main():
    global PLAIN
    args = sys.argv[1:]
    show_all = "--all" in args
    PLAIN = "--plain" in args

    if "--swatches" in args:
        show_swatches()
        return

    spec = None
    if "--port" in args:
        index = args.index("--port")
        if index + 1 >= len(args):
            sys.exit("--port needs a value")
        spec = args[index + 1]

    try:
        show = showfile.Show(SHOW_DIR)
        show.load()
    except (OSError, ValueError, KeyError) as exc:
        sys.exit(f"Show file error: {exc}")

    rows = build_rows(show.patch)
    if not rows:
        sys.exit("No patched channels. Check show/fixtures.csv.")

    try:
        addr = monitor.parse_addr(spec)
    except ValueError as exc:
        sys.exit(f"--port: {exc}")
    try:
        receiver = monitor.Receiver(addr)
    except OSError as exc:
        sys.exit(f"Cannot listen on {addr[0]}:{addr[1]}: {exc}\n"
                 "  Another dmxmon may already be running.")

    stats = {"fps": 0.0, "addr": f"{addr[0]}:{addr[1]}"}
    frame = None
    changed = set()
    frames = 0
    last_tick = time.monotonic()
    last_draw = 0.0

    print(f"{CLEAR}  Listening on {stats['addr']}...")
    print(f"  Start the controller with --monitor")

    try:
        while True:
            packet = receiver.recv()
            if packet:
                # Highlight what moved since the last redraw, so a single
                # changed channel is findable in a long list.
                if frame:
                    changed |= {c for c in range(1, 513)
                                if frame[c] != packet[c]}
                frame = packet
                frames += 1

            now = time.monotonic()
            if now - last_tick >= 1.0:
                stats["fps"] = frames / (now - last_tick)
                frames = 0
                last_tick = now

            if now - last_draw >= 1 / 15:      # redraw cap, not data cap
                last_draw = now
                render(rows, frame, stats, changed, show_all)
                changed = set()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        receiver.close()


if __name__ == "__main__":
    main()
