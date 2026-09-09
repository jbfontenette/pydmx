#!/usr/bin/env python3
"""On-screen X-Touch Mini. Build an X-Touch show with no controller plugged in.

    # terminal 1
    python3 controller.py --surface xtouchsim --no-dmx --monitor
    # terminal 2
    python3 xtouchsim.py
    # terminal 3 (optional)
    python3 dmxmon.py

Keys
    Tab          move between TOP / BOTTOM / PUSH / ENCODERS / FADER
    arrows       move within a row (up/down adjusts an encoder or the fader)
    space        tap: press and release
    enter        hold / release -- what flash-mode bindings need
    l            switch LAYER, exactly as the device's own button does
    < >          nudge the selected encoder or fader by 1
    0 / f        selected encoder or fader to 0 / to full
    q            quit

Direct shortcuts, laid out for AZERTY the way apcsim.py's are, and sharing
its tables so the two simulators never disagree about a keyboard:

    & e " ' ( S e !   top row    BT1-BT8
    1 2 3 4 5 6 7 8   bottom row BB1-BB8

Pass --qwerty for the equivalent keys on that layout.

IT REPRODUCES THE DEVICE'S AWKWARD PARTS, on purpose. A simulator that was
merely convenient would let you build a show that behaves differently on the
night, and these three are exactly what the driver exists to handle:

  * The LAYER button sends nothing. Press `l` and the controller is not told
    -- it finds out from the next number that arrives, as it must.
  * A lamp written to the layer NOT showing is DISCARDED, not stored. This
    is what makes painting both layers safe, and you can watch it work.
  * A button LIGHTS ITSELF while held and goes dark on release, whatever the
    host asked for. The driver puts the lamp back afterwards; here you can
    see the moment it does.

Encoders are absolute and their rings show the value, so seeding an encoder
from the show -- which on the real device physically moves the knob -- moves
this one too.
"""

import sys
import time

import simlink
import xtouch_constants as xt

# Keyboard handling and the key tables come from apcsim: they are the same
# terminal and the same layouts, and two copies would disagree the first
# time one was fixed. Importing a sibling script is a little unusual, but
# less so than a third file existing to hold forty lines of termios.
from apcsim import (BOLD, CLEAR, DIM, RESET, Keys, paint,
                    SCENE_KEYS_AZERTY, SCENE_KEYS_QWERTY,
                    TRACK_KEYS_AZERTY, TRACK_KEYS_QWERTY)

SECTIONS = ("top", "bottom", "push", "encoders", "fader")

LIT = (255, 190, 60)        # the device's lamps are amber, and only amber
RING = (90, 200, 255)


class Surface:
    """The device's own state: what it shows, and what it refuses to hear."""

    def __init__(self):
        self.layer = 0
        # Per layer, because the device keeps two independent surfaces and
        # shows one. Writes to the other are dropped on arrival.
        self.lamps = [{} for _ in range(xt.LAYERS)]
        self.rings = [{n: 0 for n in xt.RINGS} for _ in range(xt.LAYERS)]
        self.faders = [0, 0]                    # one per layer
        self.held = set()

    def layer_name(self):
        return xt.LAYER_NAMES[self.layer].upper()

    def apply_note(self, note, velocity):
        """A lamp write. Discarded unless it names the layer showing.

        THE POINT OF THE SIMULATOR, in four lines. The controller sends both
        layers' pictures every time precisely because this happens, and if
        this accepted everything, the surface would look right here and be
        wrong on the device.
        """
        layer = xt.layer_of("note", note)
        if layer is None or layer != self.layer:
            return
        control = xt.to_control(note, layer)
        if control in xt.BUTTONS:
            self.lamps[layer][control] = 1 if velocity else 0

    def apply_cc(self, control, value):
        """A ring write -- which MOVES the encoder, as on the real device."""
        layer = xt.layer_of("cc", control)
        if layer is None or layer != self.layer:
            return
        number = xt.to_fader(control, layer)
        if number in xt.RINGS:
            self.rings[layer][number] = value

    def lamp(self, control):
        """Lit right now, including the device's own light-while-held.

        The lamp follows the finger whatever the host asked for. That is not
        a nicety: it is why the driver re-asserts the value on release, and
        without it here you would never see that happen.
        """
        if control in self.held:
            return True
        return bool(self.lamps[self.layer].get(control))


def note_for(section, index, layer):
    """Raw note for a control in a section, or None where there is no note."""
    if section == "top":
        return xt.BUTTONS_TOP[xt.LAYER_NAMES[layer].upper()][index]
    if section == "bottom":
        return xt.BUTTONS_BOTTOM[xt.LAYER_NAMES[layer].upper()][index]
    if section == "push":
        return xt.ENCODER_PUSH[xt.LAYER_NAMES[layer].upper()][index]
    return None


def shortcut(key, top_keys, bottom_keys):
    """(section, index) for a direct key, or None."""
    if key in top_keys:
        return "top", top_keys.index(key)
    if key in bottom_keys:
        return "bottom", bottom_keys.index(key)
    return None


def render(surface, section, index, connected, top_keys, bottom_keys):
    name = surface.layer_name()
    status = "" if connected else f"{DIM}waiting for controller...{RESET}"
    out = [CLEAR, f"  {BOLD}X-Touch Mini simulator{RESET}   "
                  f"LAYER {BOLD}{name}{RESET}   {status}\n"]

    rings = []
    for n in xt.RINGS:
        value = surface.rings[surface.layer][n]
        filled = round(value / 127 * 8)
        bar = "".join("■" if i < filled else "·" for i in range(8))
        cell = paint(bar, RING) if value else f"{DIM}{bar}{RESET}"
        cursor = (section == "encoders" and index == n - 1)
        rings.append(f"[{cell}]" if cursor else f" {cell} ")
    out.append("   enc " + "".join(rings))
    out.append("       " + "".join(f" {DIM}E{n}{RESET}{' ' * 7}"
                                   for n in xt.RINGS))

    pushes = []
    for i in range(8):
        control = xt.PUSHES[i]
        glyph = "●" if control in surface.held else "·"
        cell = (paint(glyph, LIT) if control in surface.held
                else f"{DIM}{glyph}{RESET}")
        cursor = (section == "push" and index == i)
        pushes.append(f"   [{cell}]   " if cursor else f"    {cell}    ")
    out.append("\n  push " + "".join(pushes))
    out.append(f"       {DIM}" + "".join(f"   P{i + 1}     "
                                         for i in range(8)) + RESET + "\n")

    for row, keys, label in (("top", top_keys, "BT"),
                             ("bottom", bottom_keys, "BB")):
        cells = []
        for i in range(8):
            control = note_for(row, i, 0)       # ids are layer A's numbers
            lit = surface.lamp(control)
            glyph = "●" if lit else "·"
            cell = paint(glyph, LIT) if lit else f"{DIM}{glyph}{RESET}"
            cursor = (section == row and index == i)
            cells.append(f"   [{cell}]   " if cursor else f"    {cell}    ")
        out.append(f"  {label:>4} " + "".join(cells))
        out.append("       " + "".join(f"   {BOLD}{k}{RESET}     "
                                       for k in keys))

    value = surface.faders[surface.layer]
    filled = round(value / 127 * 22)
    mark = ">" if section == "fader" else " "
    out.append(f"\n  {mark} fader     {value:>4} |"
               + "#" * filled + DIM + "." * (22 - filled) + RESET + "|")

    out.append(f"\n  {DIM}tab section | arrows move | space tap | enter hold"
               f" | l layer | q quit{RESET}")
    out.append(f"  {DIM}the LAYER key tells the controller nothing, exactly "
               f"as the device does not{RESET}")
    print("\n".join(out), flush=True)


def main():
    args = sys.argv[1:]
    try:
        led_addr = simlink.parse_addr(
            args[args.index("--led") + 1] if "--led" in args else None,
            simlink.XTOUCH_LED_ADDR)
        event_addr = simlink.parse_addr(
            args[args.index("--event") + 1] if "--event" in args else None,
            simlink.XTOUCH_EVENT_ADDR)
    except (ValueError, IndexError) as exc:
        sys.exit(f"Bad address: {exc}")

    try:
        link = simlink.Endpoint(led_addr, event_addr)
    except OSError as exc:
        sys.exit(f"Cannot listen on {led_addr[0]}:{led_addr[1]}: {exc}\n"
                 "  Another xtouchsim may already be running.")

    qwerty = "--qwerty" in args
    top_keys = TRACK_KEYS_QWERTY if qwerty else TRACK_KEYS_AZERTY
    bottom_keys = SCENE_KEYS_QWERTY if qwerty else SCENE_KEYS_AZERTY

    surface = Surface()
    section, index = "top", 0
    keys = Keys()
    last_draw = last_hello = last_led = 0.0

    def send_note(note, pressed):
        link.send(bytes([simlink.PRESS if pressed else simlink.RELEASE, note]))

    def send_cc(control, value):
        link.send(simlink.encode_cc(control, value))

    def encoder_cc(number):
        return xt.ENCODER_CC[surface.layer_name()][number - 1]

    def move(delta):
        """Adjust the selected encoder or fader, and report it as the device
        would -- on the CC for the layer currently showing."""
        if section == "encoders":
            number = index + 1
            value = max(0, min(127, surface.rings[surface.layer][number]
                               + delta))
            surface.rings[surface.layer][number] = value
            send_cc(encoder_cc(number), value)
        elif section == "fader":
            value = max(0, min(127, surface.faders[surface.layer] + delta))
            surface.faders[surface.layer] = value
            send_cc(xt.FADER_CC[surface.layer_name()], value)

    def set_value(value):
        if section == "encoders":
            surface.rings[surface.layer][index + 1] = value
            send_cc(encoder_cc(index + 1), value)
        elif section == "fader":
            surface.faders[surface.layer] = value
            send_cc(xt.FADER_CC[surface.layer_name()], value)

    def press(where, i, hold=False):
        note = note_for(where, i, surface.layer)
        if note is None:
            return
        control = xt.to_control(note, surface.layer)
        if hold:
            if control in surface.held:
                surface.held.discard(control)
                send_note(note, False)
            else:
                surface.held.add(control)
                send_note(note, True)
        else:
            send_note(note, True)
            send_note(note, False)
            surface.held.discard(control)

    try:
        while True:
            now = time.monotonic()
            if now - last_hello >= 2.0:
                last_hello = now
                link.send(bytes([simlink.HELLO]))

            for payload in link.drain():
                last_led = now
                kind = payload[0]
                if kind == simlink.LED:
                    for note, velocity, _ in simlink.decode_leds(payload):
                        surface.apply_note(note, velocity)
                elif kind == simlink.CC:
                    surface.apply_cc(*simlink.decode_cc(payload))
                elif kind == simlink.CLEAR:
                    surface.lamps = [{} for _ in range(xt.LAYERS)]

            for key in keys.read():
                if key == "q":
                    raise KeyboardInterrupt

                direct = shortcut(key, top_keys, bottom_keys)
                if direct is not None:
                    press(direct[0], direct[1])
                    continue

                if key == "\t":
                    section = SECTIONS[(SECTIONS.index(section) + 1)
                                       % len(SECTIONS)]
                    index = 0
                elif key == "l":
                    # Silent, exactly as the device is. The controller works
                    # it out from the next number that arrives.
                    surface.layer = (surface.layer + 1) % xt.LAYERS
                    surface.held.clear()
                elif key in ("up", "down"):
                    move(8 if key == "up" else -8)
                elif key in ("left", "right"):
                    span = 8 if section != "fader" else 1
                    index = max(0, min(span - 1,
                                       index + (1 if key == "right" else -1)))
                elif key in ("<", ">"):
                    move(1 if key == ">" else -1)
                elif key in ("0", "f"):
                    set_value(0 if key == "0" else 127)
                elif key == " " and section in ("top", "bottom", "push"):
                    press(section, index)
                elif key in ("\r", "\n") and section in ("top", "bottom",
                                                         "push"):
                    press(section, index, hold=True)

            now = time.monotonic()
            if now - last_draw >= 1 / 20:
                last_draw = now
                render(surface, section, index, (now - last_led) < 4.0,
                       top_keys, bottom_keys)
            time.sleep(0.005)

    except KeyboardInterrupt:
        pass
    finally:
        keys.restore()
        link.close()
        print("\nStopped.")


if __name__ == "__main__":
    main()
