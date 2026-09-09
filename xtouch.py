"""Behringer X-Touch Mini surface wrapper.

The control map lives in xtouch_constants.py, so this module, xtouch_dump.py
and xtouch_leds.py cannot disagree about it. Its names are re-exported here
because every call site reaches them through whichever surface module it
holds (`mod.BUTTONS`, `mod.LAYER_AT_START`) and should not have to know or
care which one that is.

Three things make this device different from the APC, and all three are
measured rather than assumed -- see xtouch_constants.py for how, and for what
the manufacturer's own documentation says instead.

1. IT LISTENS WHERE IT SPEAKS, PER LAYER. To light a button you send the note
   that button sends on the layer currently showing. The other layer's
   numbers are discarded as they arrive, not queued. So this class translates
   both ways: the controller only ever sees layer-independent control ids.

2. THE DEVICE NEVER SAYS WHICH LAYER IT IS SHOWING. The layer button sends
   nothing at all, and program change does not move it either. The only
   evidence is the numbers that arrive, so the layer is inferred from them
   and reported as a ('layer', index) event -- and that inference is needed
   only for INPUT, to decide which binding a press fires. Painting does not
   need it: both layers are painted every time and the device keeps the one
   that matters, which is what makes the surface right at startup rather
   than dark, and right again after a switch made by hand.

3. THE DEVICE REMEMBERS EACH LAYER SEPARATELY. Switch away and back and the
   lamps are as you left them, so there is no repaint-on-switch here -- that
   would be sixteen wasted messages for a picture the device already has.
   The cache is per layer for the same reason, and a write aimed at the
   layer NOT showing is never cached, because the device discards it and
   believing otherwise would suppress the re-send that keeps the surface
   honest.

Button LEDs are BINARY -- velocity 0 off, every value 1-127 plain on, with no
brightness step and no blink anywhere in the range. The APC's idle-glow
scheme has no equivalent, so a bound button looks exactly like an unbound
one; build_leds shows ACTIVE state on these lamps instead, which is the half
worth having when only one can be shown.
"""

import mido

from xtouch_constants import (               # noqa: F401 -- re-exported
    CHANNEL, ENCODER_PUSH, BUTTONS_TOP, BUTTONS_BOTTOM, ENCODER_CC, FADER_CC,
    LED_NOTE, RING_CC, NAME, MAPPING_NAMES,
    PADS, BUTTONS, PUSHES, RINGS, FADERS, FADER_LAYERS,
    LAYERS, LAYER_NAMES, LAYER_AT_START, LAYER_HINT, BUTTON_SHOWS,
    LAYER_FALLS_THROUGH, PAINT_HIDDEN_LAYERS,
    OFF, ON, IDLE, FEEDBACK, SOFT_BLINK,
    layer_index, layer_of, parse_control, describe_control,
    to_control, to_note, to_fader,
)


class XTouchError(RuntimeError):
    pass


def _find(names, kind):
    for name in names:
        if "x-touch" in name.lower() or "xtouch" in name.lower():
            return name
    raise XTouchError(f"No X-Touch {kind} port found. Available: "
                      + (", ".join(names) if names else "none")
                      + "\n  Check the USB cable, and that the device is in"
                      + "\n  Standard mode -- MC MODE changes every number.")


class XTouch:
    def __init__(self, input_name=None, output_name=None):
        self.input_name = input_name or _find(mido.get_input_names(), "input")
        self.output_name = (output_name
                            or _find(mido.get_output_names(), "output"))
        self.inp = mido.open_input(self.input_name)
        self.out = mido.open_output(self.output_name)

        # Which layer the device is showing, as far as we can tell. None
        # until something arrives to tell us. Everything that paints checks
        # this first.
        self.layer = LAYER_AT_START

        # What the device holds, per layer. Not one cache: the two surfaces
        # are independent and both persist, so a switch does not invalidate
        # either. A write to the layer that is not showing would be dropped
        # by the device, so it is never made -- which is exactly what keeps
        # the hidden layer's entry true.
        self._delivered = [{} for _ in range(LAYERS)]

        # What the SHOW wants, per layer. A second dict because this device
        # changes its own lamps and rings behind our back -- see _restore --
        # and re-asserting a value needs to know what the value was.
        self._desired = [{} for _ in range(LAYERS)]

    # --- input ------------------------------------------------------------
    def poll(self):
        """Non-blocking. ('press'|'release', control), ('fader', n, v) and
        ('layer', index).

        Control ids are layer-INDEPENDENT: a press of note 34 comes back as
        control 10, the same id note 10 gives on layer A. mapping-xtouch.csv
        therefore never mentions a layer B number, and moving a binding
        between layers is a one-column edit.

        A layer change is emitted BEFORE the event that revealed it, so the
        press resolves against the layer it was actually made on. Getting
        that order wrong would fire the other layer's binding on the first
        press after every switch -- once per switch, silently, and only when
        the two layers disagree about that control.
        """
        events = []
        for msg in self.inp.iter_pending():
            if msg.type in ("note_on", "note_off"):
                kind, number = "note", msg.note
            elif msg.type == "control_change":
                kind, number = "cc", msg.control
            else:
                continue

            layer = layer_of(kind, number)
            if layer is None:
                continue                    # not a control we know
            if layer != self.layer:
                self.layer = layer
                events.append(("layer", layer))

            if kind == "cc":
                fader = to_fader(number, layer)
                if fader is not None:
                    if fader in RINGS:
                        # The device drove this ring itself as the knob
                        # turned, so what we believe it holds is now wrong.
                        self._delivered[layer].pop(("ring", fader), None)
                    events.append(("fader", fader, msg.value))
                continue

            control = to_control(number, layer)
            if msg.type == "note_on" and msg.velocity > 0:
                events.append(("press", control))
            else:
                events.append(("release", control))
                self._restore(control)
        return events

    def _restore(self, control):
        """Put back a lamp the device changed by itself.

        THE BUTTONS LIGHT THEMSELVES WHILE HELD AND GO DARK ON RELEASE. That
        is the device's own doing, not ours -- the controller sends one Note
        On and never turns it off, and the lamp goes out anyway.

        Left alone it is worse than cosmetic, because of the diff cache: we
        still believe the lamp is lit, so nothing is re-sent and a scene can
        run all night behind a dark button. Every press would quietly
        de-sync one more control.

        So on release, forget what we thought and send the show's value
        again. Only on release: doing it on press would fight the device for
        the lamp while your finger is on it, and that local flash is decent
        press feedback in its own right.

        X-Touch Editor can reportedly set a button's behaviour to Toggle,
        which may hand the lamp to the host outright. This does not depend
        on that being true, or on the device being configured at all.
        """
        if self.layer is None or control not in BUTTONS:
            return
        self._delivered[self.layer].pop(control, None)
        self.button(control, self._desired[self.layer].get(control, 0))

    # --- output -----------------------------------------------------------
    def button(self, control, state=1, force=False, layer=None):
        """Light or unlight one button, on a given layer.

        Binary: any non-zero state is on. layer defaults to the one believed
        to be showing; naming one explicitly is how the controller paints
        every layer without knowing which is which.

        A write to a layer that is NOT showing is discarded by the device,
        so it cannot be cached -- and while the layer is unknown, that is
        every write. Sending anyway is the whole trick: both pictures go
        out, the device keeps the one that matters, and the surface is
        correct without anybody having to know which layer that was.
        """
        target = self.layer if layer is None else layer
        if target is None:
            return
        state = 1 if state else 0
        self._desired[target][control] = state
        landed = target == self.layer
        cache = self._delivered[target]
        if landed and not force and cache.get(control) == state:
            return
        if landed:
            cache[control] = state
        else:
            cache.pop(control, None)
        self.out.send(mido.Message("note_on", channel=CHANNEL,
                                   note=to_note(control, target),
                                   velocity=ON if state else OFF))

    def ring(self, number, value, force=False, layer=None):
        """Set an encoder's LED ring to a value 0-127.

        THIS ALSO MOVES THE ENCODER. Measured: with the knob at 7, setting
        the ring to 110 made the next click report 111. So this is not only
        a display -- it is the one way to tell this device where a control
        should be sitting, and the cure for the startup jump. There is no
        Introduction message here to ask with, but painting a bound encoder
        with the show's value puts the knob physically where the show is.

        The device drives the ring itself when the user turns the knob, so
        the rest of the time this is for values the SOFTWARE owns -- after a
        reload, or on a layer switch, where the device's remembered position
        and the show's value have no reason to agree.

        How the value is DRAWN -- a travelling dot, a fill from one end, a
        fan from the centre -- is a device-side setting per encoder per
        layer, made in X-Touch Editor. It cannot be set over MIDI, so a
        pan/tilt encoder wanting a dot has to be configured on the device.
        """
        target = self.layer if layer is None else layer
        if target is None:
            return
        value = max(0, min(127, int(value)))
        key = ("ring", number)
        landed = target == self.layer
        cache = self._delivered[target]
        if landed and not force and cache.get(key) == value:
            return
        if landed:
            cache[key] = value
        else:
            cache.pop(key, None)
        self.out.send(mido.Message(
            "control_change", channel=CHANNEL,
            control=RING_CC[LAYER_NAMES[target].upper()][number - 1],
            value=value))

    def pad(self, note, colour, behaviour=None, force=False):
        """There is no colour grid on this device.

        Raising rather than ignoring: build_leds only calls this when
        mod.PADS is non-empty, so reaching here means a caller assumed a
        grid, and a silent no-op would hide that until someone wondered why
        their pads were dark.
        """
        raise XTouchError("the X-Touch Mini has no colour pads -- "
                          "PADS is empty, so nothing should call pad()")

    def pads_rgb(self, entries):
        """No-op. No RGB pads to colour, and --feedback rgb is refused at
        startup anyway, since FEEDBACK lists only what this surface can do."""
        return

    def clear(self):
        """Everything off, on every layer.

        Both layers, because only one of the two writes will land and there
        is no way to know which. That is also why this is worth doing on
        exit: an earlier version cleared only the layer it believed was
        showing, and left the other one lit after the process ended.
        """
        for layer in range(LAYERS):
            for control in BUTTONS:
                self.button(control, 0, force=True, layer=layer)
            for number in RINGS:
                self.ring(number, 0, force=True, layer=layer)

    def refresh(self):
        """Forget what we believe the device holds, so the next paint
        re-sends everything.

        Both layers, not just the one showing: after a reload the bindings
        behind the hidden layer's lamps may have changed too, and its cache
        would otherwise keep a paint from being made when it does appear.

        What the show WANTS is deliberately kept. It is about to be
        recomputed by the next paint anyway, and dropping it would leave
        _restore with nothing to put back if a release landed in between.
        """
        self._delivered = [{} for _ in range(LAYERS)]

    def introduce(self, timeout=1.0):
        """No equivalent of the APC's Introduction message.

        Returns None, always. The device cannot be asked where its encoders
        and fader are sitting, so by invariant 10 the master starts at 0 and
        the first move syncs it. An unexpected blackout costs one gesture; an
        unexpected full blast in a venue does not.
        """
        return None

    def close(self):
        try:
            self.clear()
        finally:
            self.inp.close()
            self.out.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# controller.py builds `surface_module().Surface()`. The alias means the
# module itself is the contract, so no call site learns which device it holds.
Surface = XTouch
