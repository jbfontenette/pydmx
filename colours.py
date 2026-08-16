"""Named colours for the APC mini mk2 pads.

Each name carries two things:

  rgb      the true 24-bit colour, sent via SysEx. This is what you actually
           see by default.
  palette  the nearest entry in the device's fixed 128-colour palette, used
           only by the pulse and blink feedback styles, which are driven by
           Note On channel and therefore cannot carry arbitrary RGB.

The two differ where the palette simply cannot express a colour:

  yellow_warm / yellow
               Settled by eye, not by hex. Three attempts:
                 #FFE126 as warm -> read LIGHTER than #FFFF00, not warmer,
                   because its blue channel of 38 desaturates it.
                 #B9B000 as warm -> read as plain yellow; too dull to
                   register as a separate colour.
                 SWAPPED -> #FFFF00 is the warm one and #FFE126 the plain
                   one. On these LEDs the saturated primary looks warmer
                   than the slightly blued version, so the names follow
                   what the eye reports rather than what the hex implies.

  cold_white   ACCEPTED AS INDISTINGUISHABLE FROM WHITE. The palette offers
               only #E0FFFF (one step from #FFFFFF) or #4CC3FF (an icy blue
               that no longer reads as white). Nothing sits between them, so
               #E0FFFF stands and the two whites look alike. Do not "fix"
               this again without checking the palette for a new candidate --
               there isn't one.

PALETTE IS THE DEFAULT. The RGB values are used only by the experimental
'rgb' feedback style, which drives pads over SysEx. SysEx pad colouring is
NOT yet confirmed working on this unit -- verify with:
    python3 apc_leds.py rgbtest
"""

#            name            rgb        palette
NAMES = {
    "red":         ("FF0000",   5),
    "amber":       ("FF7F00",  96),
    "yellow_warm": ("FFC000",  13),   # #FFFF00 -- reads as the warmer of the two
    "yellow":      ("FFFF00", 109),   # #FFE126 -- blue 38 makes it the paler one
    "green":       ("00FF00",  21),
    "turquoise":   ("00FFA0",  90),
    "cyan":        ("00E0FF",  37),
    "blue":        ("0000FF",  45),
    "lavender":    ("8080FF", 115),
    "mauve":       ("C000FF",  94),
    "magenta":     ("FF00FF",  53),
    "pink":        ("FF4080",  56),
    "warm_white":  ("FFB060",   8),
    "white":       ("FFFFFF",   3),
    "cold_white":  ("90C0FF", 119),   # #E0FFFF -- barely differs from white
    "uv":          ("6000FF",  81),
}

ORDER = list(NAMES)

# Idle pads sit at this fraction of full. LED output is close enough to
# linear in PWM that 0.12 reads as roughly "clearly on but not running".
IDLE_SCALE = 0.12


def _key(token):
    """'Yellow Warm', 'yellow warm', 'yellow-warm' all normalise the same."""
    return token.strip().lower().replace(" ", "_").replace("-", "_")


def resolve(token):
    """Accept a colour name or a raw palette index 0-127. Returns the name.

    Raw numbers become a synthetic entry so old numeric mappings keep working.
    """
    token = str(token).strip()
    if token.isdigit():
        index = int(token)
        if not 0 <= index <= 127:
            raise ValueError(f"palette index {index} out of range 0-127")
        return f"#{index}"
    key = _key(token)
    if key not in NAMES:
        raise ValueError(f"unknown colour '{token}'. Known: "
                         + ", ".join(ORDER) + " (or a number 0-127)")
    return key


def rgb(name, scale=1.0):
    """(r, g, b) for a resolved name, optionally dimmed."""
    if name.startswith("#"):
        return (0, 0, 0)          # numeric entries have no RGB; palette only
    hexv = NAMES[name][0]
    parts = tuple(int(hexv[i:i + 2], 16) for i in (0, 2, 4))
    if scale == 1.0:
        return parts
    return tuple(max(0, min(255, round(v * scale))) for v in parts)


def palette(name):
    """Fixed-palette index for a resolved name."""
    if name.startswith("#"):
        return int(name[1:])
    return NAMES[name][1]


def hex_for(name):
    return None if name.startswith("#") else NAMES[name][0]


def find(token):
    """Canonical colour name for a token, or None if it is not a colour.

    Lenient on purpose: profile value names are hand-typed, so 'Warm White',
    'warm-white' and 'warm_white' all resolve. Returns None rather than
    raising, because most named values ('slow', 'on', 'off') are not colours
    and that is perfectly normal.
    """
    key = _key(str(token))
    return key if key in NAMES else None


# The device's fixed 128-entry palette, transcribed from the v1.0 protocol
# doc. Needed to RENDER a palette index back to a colour -- the controller
# sends indices, so anything drawing the surface (apcsim.py) has to reverse
# the lookup. Note 5/72, 21/87, 45/67 and 2/70 are genuine duplicates in the
# device's own table.
PALETTE_HEX = [
    "000000", "1E1E1E", "7F7F7F", "FFFFFF", "FF4C4C", "FF0000", "590000",
    "190000", "FFBD6C", "FF5400", "591D00", "271B00", "FFFF4C", "FFFF00",
    "595900", "191900", "88FF4C", "54FF00", "1D5900", "142B00", "4CFF4C",
    "00FF00", "005900", "001900", "4CFF5E", "00FF19", "00590D", "001902",
    "4CFF88", "00FF55", "00591D", "001F12", "4CFFB7", "00FF99", "005935",
    "001912", "4CC3FF", "00A9FF", "004152", "001019", "4C88FF", "0055FF",
    "001D59", "000819", "4C4CFF", "0000FF", "000059", "000019", "874CFF",
    "5400FF", "190064", "0F0030", "FF4CFF", "FF00FF", "590059", "190019",
    "FF4C87", "FF0054", "59001D", "220013", "FF1500", "993500", "795100",
    "436400", "033900", "005735", "00547F", "0000FF", "00454F", "2500CC",
    "7F7F7F", "202020", "FF0000", "BDFF2D", "AFED06", "64FF09", "108B00",
    "00FF87", "00A9FF", "002AFF", "3F00FF", "7A00FF", "B21A7D", "402100",
    "FF4A00", "88E106", "72FF15", "00FF00", "3BFF26", "59FF71", "38FFCC",
    "5B8AFF", "3151C6", "877FE9", "D31DFF", "FF005D", "FF7F00", "B9B000",
    "90FF00", "835D07", "392B00", "144C10", "0D5038", "15152A", "16205A",
    "693C1C", "A8000A", "DE513D", "D86A1C", "FFE126", "9EE12F", "67B50F",
    "1E1E30", "DCFF6B", "80FFBD", "9A99FF", "8E66FF", "404040", "757575",
    "E0FFFF", "A00000", "350000", "1AD000", "074200", "B9B000", "3F3100",
    "B35F00", "4B1502",
]

# MIDI channel -> brightness, from the LED behaviour table. Channels 7-10
# pulse and 11-15 blink; those are animated by the renderer rather than
# being a fixed level.
BRIGHTNESS = {0: 0.10, 1: 0.25, 2: 0.50, 3: 0.65, 4: 0.75, 5: 0.90, 6: 1.00}
PULSE_CHANNELS = (7, 8, 9, 10)
BLINK_CHANNELS = (11, 12, 13, 14, 15)
# Rate in Hz for each animated channel, used only for drawing.
ANIM_HZ = {7: 4.0, 8: 3.0, 9: 2.0, 10: 1.0,
           11: 6.0, 12: 5.0, 13: 4.0, 14: 3.0, 15: 2.0}


def palette_rgb(index):
    """(r, g, b) for a palette index 0-127."""
    hexv = PALETTE_HEX[index % len(PALETTE_HEX)]
    return tuple(int(hexv[i:i + 2], 16) for i in (0, 2, 4))
