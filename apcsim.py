#!/usr/bin/env python3
"""On-screen APC mini mk2. Work with no controller plugged in.

    # terminal 1
    python3 controller.py --sim --no-dmx --monitor
    # terminal 2
    python3 apcsim.py
    # terminal 3 (optional)
    python3 dmxmon.py

Keys
    Tab          move between GRID / TRACK / SCENE / FADERS
    arrows       move within a section (up/down adjusts a fader)
    space        tap: press and release
    enter        hold / release -- what flash-mode bindings need
    s            toggle SHIFT
    < >          nudge the selected fader by 1
    0 / f        selected fader to 0 / to full
    q            quit

Direct shortcuts (no tabbing) -- laid out for AZERTY, where these are the
same physical keys unshifted and shifted:

    & e " ' ( S e !   track buttons T1-T8   (Mac French AZERTY top row)
    1 2 3 4 5 6 7 8   scene launch  S1-S8

So with the default mapping, "&" is the clear button and "1" is reload.
Pass --qwerty for the equivalent keys on that layout, or run --keys to see
what your own keyboard actually emits.

Renders LED state faithfully: brightness channels are scaled, and pulse and
blink channels animate at roughly their documented rates.
"""

import os
import select
import sys
import termios
import time
import tty

import colours
import simlink

GRID = range(0x00, 0x40)
TRACK = range(0x64, 0x6C)
SCENE = range(0x70, 0x78)
SHIFT_NOTE = 0x7A

TRUECOLOR = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")

CLEAR = "\x1b[H\x1b[2J"
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"

SECTIONS = ("grid", "track", "scene", "faders")

# Direct shortcuts for the peripheral buttons, so the ones you hit constantly
# -- clear, reload -- do not need tabbing to.
#
# Laid out for MAC French AZERTY, whose top row unshifted is & e " ' ( S e !
# and shifted is 1-8. Same physical key: unshifted is the track button,
# shifted is the scene button.
#
# Note this differs from PC AZERTY at two positions -- PC gives "-" and "_"
# where the Mac gives the section sign and "!". If a key does nothing, run
# `python3 apcsim.py --keys` to see exactly what your keyboard emits and
# edit the row below to match; nothing else needs to change.
TRACK_KEYS_AZERTY = "".join(["&", "é", '"', "'", "(", "§", "è", "!"])
SCENE_KEYS_AZERTY = "12345678"
TRACK_KEYS_QWERTY = "12345678"
SCENE_KEYS_QWERTY = "".join(["!", "@", "#", "$", "%", "^", "&", "*"])


def to_256(r, g, b):
    if max(r, g, b) - min(r, g, b) < 12:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + round((r - 8) / 247 * 24)
    return (16 + 36 * round(r / 255 * 5)
            + 6 * round(g / 255 * 5) + round(b / 255 * 5))


def paint(text, rgb):
    r, g, b = rgb
    if TRUECOLOR:
        return f"\x1b[38;2;{r};{g};{b}m{text}{RESET}"
    return f"\x1b[38;5;{to_256(r, g, b)}m{text}{RESET}"


class Surface:
    """LED state as the controller has set it."""

    def __init__(self):
        self.pads = {}       # note -> (velocity, channel)
        self.buttons = {}    # note -> velocity
        self.faders = [0] * 9
        self.faders[8] = 100      # master starts part-way, not silent
        self.shift = False
        self.held = set()

    def apply(self, note, velocity, channel):
        if note in GRID:
            self.pads[note] = (velocity, channel)
        else:
            self.buttons[note] = velocity

    def clear(self):
        self.pads.clear()
        self.buttons.clear()

    def pad_rgb(self, note, now):
        """Colour for a pad right now, honouring brightness and animation."""
        entry = self.pads.get(note)
        if not entry:
            return None
        velocity, channel = entry
        if velocity == 0:
            return None
        rgb = colours.palette_rgb(velocity)

        if channel in colours.BRIGHTNESS:
            scale = colours.BRIGHTNESS[channel]
        else:
            # Pulse fades in and out; blink is hard on/off. Both are timed
            # from the wall clock rather than a frame counter, so the rate
            # stays right regardless of how fast this redraws.
            hz = colours.ANIM_HZ.get(channel, 2.0)
            phase = (now * hz) % 1.0
            if channel in colours.PULSE_CHANNELS:
                scale = 0.15 + 0.85 * abs(1 - 2 * phase)
            else:
                scale = 1.0 if phase < 0.5 else 0.08
        return tuple(max(0, min(255, round(c * scale))) for c in rgb)


class Keys:
    """Raw-mode keyboard reader with escape-sequence decoding."""

    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)

    def read(self):
        keys = []
        while select.select([sys.stdin], [], [], 0)[0]:
            data = os.read(self.fd, 64).decode(errors="ignore")
            if not data:
                break
            i = 0
            while i < len(data):
                if data[i] == "\x1b" and data[i:i + 3] in (
                        "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"):
                    keys.append({"A": "up", "B": "down",
                                 "C": "right", "D": "left"}[data[i + 2]])
                    i += 3
                else:
                    keys.append(data[i])
                    i += 1
        return keys

    def restore(self):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)


def probe_keys(track_keys, scene_keys):
    """Print what each keypress emits and what it maps to.

    Keyboard layouts vary more than seems reasonable -- Mac and PC AZERTY
    disagree on two keys of the same row -- so rather than guess, this shows
    the truth for whatever is actually plugged in.
    """
    print("\n  Press keys to see what they emit. Ctrl-C to stop.\n")
    print(f"  Expected track row: {' '.join(track_keys)}")
    print(f"  Expected scene row: {' '.join(scene_keys)}\n")
    keys = Keys()
    try:
        while True:
            for key in keys.read():
                note = shortcut_note(key, track_keys, scene_keys)
                if note is None:
                    where = "no shortcut"
                elif note < 0x70:
                    where = f"TRACK {note - 0x64 + 1}"
                else:
                    where = f"SCENE {note - 0x70 + 1}"
                codes = " ".join(f"U+{ord(c):04X}" for c in key)
                print(f"    {key!r:<10} {codes:<10} -> {where}")
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        keys.restore()
        print()


def shortcut_note(key, track_keys, scene_keys):
    """Note number for a direct shortcut, or None.

    Track is checked first: on QWERTY '&' is shift-7 and appears in both
    tables, and the track button is the one you actually want from a key
    that is unshifted on the layout this was designed for.
    """
    if key and key in track_keys:
        return 0x64 + track_keys.index(key)
    if key and key in scene_keys:
        return 0x70 + scene_keys.index(key)
    return None


def note_for(section, row, col):
    if section == "grid":
        return row * 8 + col
    if section == "track":
        return 0x64 + col
    if section == "scene":
        return 0x70 + row
    return None


def render(surface, section, row, col, fader, connected,
           track_keys, scene_keys):
    now = time.monotonic()
    status = ("" if connected
              else f"{DIM}waiting for controller...{RESET}")
    out = [CLEAR, f"  {BOLD}APC mini mk2 simulator{RESET}   {status}\n"]

    # Row 7 prints first: note 0 is the BOTTOM-left pad, and the terminal
    # draws downward, so the visual top row is notes 56-63.
    for r in range(7, -1, -1):
        cells = []
        for c in range(8):
            note = r * 8 + c
            rgb = surface.pad_rgb(note, now)
            glyph = "\u25cf" if rgb else "\u00b7"
            cell = paint(glyph, rgb) if rgb else f"{DIM}{glyph}{RESET}"
            if note in surface.held:
                cell = f"\x1b[4m{cell}\x1b[24m"
            cursor = (section == "grid" and r == row and c == col)
            cells.append(f"[{cell}]" if cursor else f" {cell} ")
        index = 7 - r
        scene_note = 0x70 + index
        lit = surface.buttons.get(scene_note, 0)
        s_glyph = paint("\u25cf", (0, 255, 0)) if lit else f"{DIM}\u00b7{RESET}"
        s_cur = (section == "scene" and index == row)
        key = scene_keys[index] if index < len(scene_keys) else " "
        out.append(f"   r{r} " + "".join(cells)
                   + ("  [" + s_glyph + "]" if s_cur else "   " + s_glyph + " ")
                   + f" {DIM}S{index + 1}{RESET} {BOLD}{key}{RESET}")

    track_cells = []
    for c in range(8):
        lit = surface.buttons.get(0x64 + c, 0)
        glyph = paint("\u25cf", (255, 0, 0)) if lit else f"{DIM}\u00b7{RESET}"
        cursor = (section == "track" and c == col)
        track_cells.append(f"[{glyph}]" if cursor else f" {glyph} ")
    out.append("\n       " + "".join(track_cells)
               + f"   {BOLD if surface.shift else DIM}SHIFT{RESET} {BOLD}s{RESET}")
    out.append(f"       {DIM} T1  T2  T3  T4  T5  T6  T7  T8{RESET}")
    out.append("       " + "".join(f" {BOLD}{k}{RESET}  " for k in track_keys)
               + "\n")

    for i in range(9):
        value = surface.faders[i]
        filled = round(value / 127 * 22)
        mark = ">" if (section == "faders" and i == fader) else " "
        label = "F9 master" if i == 8 else f"F{i + 1}"
        out.append(f"  {mark} {label:<10}{value:>4} |"
                   + "#" * filled + DIM + "." * (22 - filled) + RESET + "|")

    out.append(f"\n  {DIM}tab section | arrows move | space tap | enter hold"
               f" | s shift | q quit{RESET}")
    out.append(f"  {DIM}bold keys above fire that button directly, "
               f"no tabbing needed{RESET}")
    print("\n".join(out), flush=True)


def main():
    args = sys.argv[1:]
    try:
        led_addr = simlink.parse_addr(
            args[args.index("--led") + 1] if "--led" in args else None,
            simlink.LED_ADDR)
        event_addr = simlink.parse_addr(
            args[args.index("--event") + 1] if "--event" in args else None,
            simlink.EVENT_ADDR)
    except (ValueError, IndexError) as exc:
        sys.exit(f"Bad address: {exc}")

    try:
        link = simlink.Endpoint(led_addr, event_addr)
    except OSError as exc:
        sys.exit(f"Cannot listen on {led_addr[0]}:{led_addr[1]}: {exc}\n"
                 "  Another apcsim may already be running.")

    qwerty = "--qwerty" in args
    track_keys = TRACK_KEYS_QWERTY if qwerty else TRACK_KEYS_AZERTY
    scene_keys = SCENE_KEYS_QWERTY if qwerty else SCENE_KEYS_AZERTY

    if "--keys" in args:
        probe_keys(track_keys, scene_keys)
        return

    surface = Surface()
    section, row, col, fader = "grid", 0, 0, 8
    keys = Keys()
    last_draw = 0.0
    last_hello = 0.0
    last_led = 0.0

    def send_press(note):
        link.send(bytes([simlink.PRESS, note]))

    def send_release(note):
        link.send(bytes([simlink.RELEASE, note]))

    def send_fader(index):
        link.send(bytes([simlink.FADER, index + 1, surface.faders[index]]))

    try:
        while True:
            now = time.monotonic()

            # Announce ourselves, repeatedly. UDP has no connection, so
            # neither side can know the other is there; the controller
            # replays its whole cached LED state each time it hears this.
            # Cheap enough to just keep doing, and it self-heals whichever
            # process was restarted.
            if now - last_hello >= 2.0:
                last_hello = now
                link.send(bytes([simlink.HELLO]))

            for payload in link.drain():
                last_led = now
                kind = payload[0]
                if kind == simlink.LED:
                    for note, velocity, channel in simlink.decode_leds(payload):
                        surface.apply(note, velocity, channel)
                elif kind == simlink.CLEAR:
                    surface.clear()
                elif kind == simlink.ENQUIRE:
                    link.send(bytes([simlink.INTRO] + surface.faders))

            for key in keys.read():
                if key == "q":
                    raise KeyboardInterrupt

                # Direct shortcuts first: on AZERTY the scene keys are the
                # digits, which the faders section would otherwise consume.
                direct = shortcut_note(key, track_keys, scene_keys)
                if direct is not None:
                    send_press(direct)
                    send_release(direct)
                    continue

                if key == "\t":
                    section = SECTIONS[(SECTIONS.index(section) + 1)
                                       % len(SECTIONS)]
                elif key == "s":
                    surface.shift = not surface.shift
                    (send_press if surface.shift else send_release)(SHIFT_NOTE)
                elif key in ("up", "down", "left", "right"):
                    if section == "faders":
                        if key in ("left", "right"):
                            fader = (fader + (1 if key == "right" else -1)) % 9
                        else:
                            step = 8 if key == "up" else -8
                            surface.faders[fader] = max(
                                0, min(127, surface.faders[fader] + step))
                            send_fader(fader)
                    elif section == "grid":
                        if key == "up":
                            row = min(7, row + 1)
                        elif key == "down":
                            row = max(0, row - 1)
                        elif key == "right":
                            col = min(7, col + 1)
                        else:
                            col = max(0, col - 1)
                    elif section == "track":
                        col = max(0, min(7, col + (1 if key == "right" else
                                                   -1 if key == "left" else 0)))
                    elif section == "scene":
                        row = max(0, min(7, row + (1 if key == "down" else
                                                   -1 if key == "up" else 0)))
                elif key in ("<", ">") and section == "faders":
                    surface.faders[fader] = max(0, min(
                        127, surface.faders[fader] + (1 if key == ">" else -1)))
                    send_fader(fader)
                elif key in ("0", "f") and section == "faders":
                    surface.faders[fader] = 0 if key == "0" else 127
                    send_fader(fader)
                elif key == " ":
                    note = note_for(section, row, col)
                    if note is not None:
                        # A tap is press then release, which is what a
                        # toggle binding wants and what a flash binding
                        # correctly treats as a momentary blip.
                        send_press(note)
                        send_release(note)
                        surface.held.discard(note)
                elif key in ("\r", "\n"):
                    note = note_for(section, row, col)
                    if note is not None:
                        if note in surface.held:
                            surface.held.discard(note)
                            send_release(note)
                        else:
                            surface.held.add(note)
                            send_press(note)

            now = time.monotonic()
            if now - last_draw >= 1 / 20:      # fast enough for blink/pulse
                last_draw = now
                render(surface, section, row, col, fader,
                       (now - last_led) < 4.0, track_keys, scene_keys)
            time.sleep(0.005)

    except KeyboardInterrupt:
        pass
    finally:
        keys.restore()
        link.close()
        print("\nStopped.")


if __name__ == "__main__":
    main()
