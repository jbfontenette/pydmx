"""The seam between a control surface and the rest of the controller.

Two devices with nothing in common geometrically have to look identical to
controller.py, and the whole of that agreement is: a module exposing the
right names, and a poll() emitting four event shapes. These tests pin both
sides of it.

The X-Touch half is where the risk is. Its layer is a LATCHING switch the
device never reports, so the driver infers it from arriving numbers and
translates every control id both ways. Get that translation off by one layer
and the wrong binding fires -- once per switch, silently, and only where the
two layers disagree, which is the hardest kind of bug to notice mid-set.
"""

import shutil
import types
import unittest
from unittest import mock

import helper

import controller
import showfile
import surface_constants
import xtouch_constants


class StubPort:
    """Stands in for a mido port: yields messages once, records what is sent."""

    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent = []

    def iter_pending(self):
        batch, self.incoming = self.incoming, []
        return iter(batch)

    def send(self, message):
        self.sent.append(message)

    def close(self):
        pass


def message(kind, **fields):
    return types.SimpleNamespace(type=kind, **fields)


def note_on(number, velocity=127):
    return message("note_on", note=number, velocity=velocity, channel=10)


def note_off(number):
    return message("note_off", note=number, velocity=0, channel=10)


def control_change(number, value):
    return message("control_change", control=number, value=value, channel=10)


class Message(types.SimpleNamespace):
    """Stands in for mido.Message: keeps whatever fields it is given."""

    def __init__(self, type, **fields):
        super().__init__(type=type, **fields)


# Enough of a mido to build a driver and let it send.
STUB_MIDO = types.SimpleNamespace(
    Message=Message,
    get_input_names=lambda: ["X-TOUCH MINI"],
    get_output_names=lambda: ["X-TOUCH MINI"],
    open_input=lambda name: StubPort(),
    open_output=lambda name: StubPort(),
)


def _driver(name):
    """Import a driver module, standing in for mido if it is not installed.

    The suite has to run with no third-party packages -- CLAUDE.md says so,
    and it is what keeps the invariants checkable on a machine that has never
    seen a MIDI backend. Both drivers do `import mido` at module scope, so
    the stand-in has to be in sys.modules at their FIRST import.
    """
    with mock.patch.dict("sys.modules", {"mido": STUB_MIDO}):
        import importlib
        return importlib.import_module(name)


APC_MODULE = _driver("apc")
XTOUCH_MODULE = _driver("xtouch")


def stub_sends(module):
    """Point a driver's mido at the stub, whichever one it imported.

    By attribute, not by sys.modules: if another test imported the module
    first with the real mido, it kept that reference and patching
    sys.modules afterwards would do nothing at all -- silently, with the
    driver then trying to open a real port.
    """
    return mock.patch.object(module, "mido", STUB_MIDO)


def make_xtouch(incoming=()):
    """An XTouch built by its own constructor, on stub ports.

    By its own constructor on purpose. An earlier version built the object
    with __new__ and set the fields by hand, which drifted the first time the
    driver grew a second cache: every painting test errored on a field the
    real device had had all along.
    """
    patch = stub_sends(XTOUCH_MODULE)
    with patch:
        device = XTOUCH_MODULE.XTouch()
    device.inp.incoming = list(incoming)
    return device, patch


class TestSurfaceContract(unittest.TestCase):
    """Every name controller.py reads off a surface, on every surface.

    A driver that is missing one of these fails at the moment it is used --
    mid-show, on the first press of whatever kind exercises it -- rather than
    at startup. Listing them here means adding a third device starts with a
    failing test instead of a surprise.
    """

    NAMES = ("NAME", "MAPPING_NAMES", "LAYERS", "LAYER_NAMES",
             "LAYER_AT_START", "LAYER_HINT", "BUTTON_SHOWS",
             "PADS", "BUTTONS", "RINGS", "FADERS", "FADER_LAYERS",
             "OFF", "IDLE", "FEEDBACK", "SOFT_BLINK",
             "layer_index", "parse_control", "describe_control")

    def modules(self):
        """Every module a call site might be holding: both vocabularies and
        every driver. The drivers matter most -- that is where SOFT_BLINK
        was missing while both vocabularies had it."""
        import virtualapc
        return (surface_constants, xtouch_constants, virtualapc,
                APC_MODULE, XTOUCH_MODULE)

    def test_every_surface_carries_the_whole_vocabulary(self):
        for module in self.modules():
            for name in self.NAMES:
                self.assertTrue(hasattr(module, name),
                                f"{module.__name__} lacks {name}")

    def test_every_name_the_controller_reads_exists_where_it_reads_it(self):
        """Derived from controller.py itself, because the list above drifted.

        SOFT_BLINK was added to both vocabulary modules and to neither
        driver. The hand-written list did not mention it, the tests only
        ever checked the vocabularies, and controller.py read it through
        getattr with a default -- so --feedback blink was accepted and
        nothing blinked, in silence. Three gaps lined up.

        controller.py holds a surface two ways: `vocab` is the vocabulary,
        which must work with nothing installed, and `mod`/`apc_mod` is the
        driver. The distinction is the point -- Surface, the class, exists
        only on a driver -- so each set is checked where it is read.
        """
        import re
        with open(helper.os.path.join(helper.ROOT, "controller.py")) as handle:
            source = handle.read()
        by_handle = {}
        for handle_name, attribute in re.findall(
                r"\b(mod|apc_mod|vocab)\.([A-Za-z_]+)", source):
            by_handle.setdefault(handle_name, set()).add(attribute)

        self.assertIn("SOFT_BLINK", by_handle.get("mod", set()),
                      "the regex stopped matching what build_leds reads")

        import virtualapc
        drivers = (virtualapc, APC_MODULE, XTOUCH_MODULE)
        vocabularies = (surface_constants, xtouch_constants) + drivers

        for name in ("mod", "apc_mod"):
            for module in drivers:
                missing = sorted(n for n in by_handle.get(name, ())
                                 if not hasattr(module, n))
                self.assertEqual(missing, [], f"{module.__name__}: {missing}")
        for module in vocabularies:
            missing = sorted(n for n in by_handle.get("vocab", ())
                             if not hasattr(module, n))
            self.assertEqual(missing, [], f"{module.__name__}: {missing}")

    def test_a_driver_re_exports_it_so_either_can_be_held(self):
        # surface_vocab() returns the driver when one is loaded, so the two
        # must agree -- that is what lets the tests inject virtualapc and
        # controller.py read PADS off it without knowing.
        import virtualapc
        for name in self.NAMES:
            self.assertEqual(getattr(virtualapc, name),
                             getattr(surface_constants, name), name)

    def test_feedback_lists_only_what_the_surface_can_do(self):
        # 'pulse' is a hardware rate the APC's pads animate themselves. The
        # X-Touch's lamps are binary -- measured velocity by velocity -- so
        # offering pulse there would be a lie that showed up only as a lamp
        # that never animated. --feedback rejects it at startup instead.
        self.assertIn("pulse", surface_constants.FEEDBACK)
        self.assertNotIn("pulse", xtouch_constants.FEEDBACK)

    def test_a_surface_that_cannot_blink_blinks_in_software(self):
        # Blink DOES exist on this device, at velocity 1 -- but in MC MODE,
        # where the buttons are notes 40-45 and 84-95 and the encoders send
        # relative deltas. That would cost the absolute encoders and the
        # whole control map for a flashing lamp. So the controller toggles
        # it instead, which also keeps every button behaving the same
        # without per-button configuration to keep in step with the show.
        self.assertFalse(surface_constants.SOFT_BLINK)
        self.assertEqual(set(xtouch_constants.SOFT_BLINK),
                         {"blink", "fast-blink"})
        for style in xtouch_constants.SOFT_BLINK:
            self.assertIn(style, xtouch_constants.FEEDBACK)
        self.assertGreater(xtouch_constants.SOFT_BLINK["fast-blink"],
                           xtouch_constants.SOFT_BLINK["blink"])

    def test_only_a_surface_with_pads_has_an_idle_brightness(self):
        # IDLE collapses to OFF where a lamp cannot be dimmed, which is the
        # honest way to say the APC's idle-glow scheme has no equivalent.
        self.assertNotEqual(surface_constants.IDLE, surface_constants.OFF)
        self.assertEqual(xtouch_constants.IDLE, xtouch_constants.OFF)
        self.assertFalse(xtouch_constants.PADS)


class TestControlVocabulary(unittest.TestCase):
    def test_the_apc_dialect_is_unchanged(self):
        # This ran a six-hour show. Every token it accepts must keep meaning
        # exactly what it meant.
        for token, expected in (("0", 0), ("r0c0", 0), ("r1c1", 9),
                                ("r7c7", 63), ("t1", 0x64), ("s1", 0x70),
                                ("f9", ("fader", 9))):
            self.assertEqual(showfile.parse_pad(token), expected, token)

    def test_the_two_dialects_do_not_overlap(self):
        # A layout written for one device should not half-parse on the
        # other: half a surface silently bound is worse than none.
        for token in ("r0c0", "t1", "s1"):
            with self.assertRaises(ValueError, msg=token):
                xtouch_constants.parse_control(token)

    def test_xtouch_buttons_by_row_and_by_number(self):
        for token, expected in (("bt1", 8), ("bt8", 15),
                                ("bb1", 16), ("bb8", 23),
                                ("b1", 8), ("b16", 23)):
            self.assertEqual(xtouch_constants.parse_control(token), expected,
                             token)

    def test_xtouch_continuous_controls(self):
        self.assertEqual(xtouch_constants.parse_control("e1"), ("fader", 1))
        self.assertEqual(xtouch_constants.parse_control("e8"), ("fader", 8))
        # f1 and f9 are the same one fader: f9 so that 'master' spells the
        # same way it does on the APC and a show can move between surfaces.
        self.assertEqual(xtouch_constants.parse_control("f1"), ("fader", 9))
        self.assertEqual(xtouch_constants.parse_control("f9"), ("fader", 9))

    def test_out_of_range_is_refused_with_the_real_range(self):
        for token in ("bt9", "bb0", "b17", "p9", "e9", "f2"):
            with self.assertRaises(ValueError, msg=token):
                xtouch_constants.parse_control(token)

    def test_describe_round_trips_every_control(self):
        # describe_control feeds the "no binding in mapping.csv" log, so a
        # wrong answer sends you to edit the wrong row.
        for module in (surface_constants, xtouch_constants):
            controls = list(module.PADS) + list(module.BUTTONS)
            controls += [("fader", n) for n in module.FADERS]
            for control in controls:
                token = module.describe_control(control)
                self.assertEqual(module.parse_control(token), control,
                                 f"{module.NAME}: {token}")

    def test_a_raw_note_for_the_other_layer_is_refused(self):
        # Layer B numbers never appear in a mapping file: control ids are
        # layer-independent and the layer is its own column. Accepting 34
        # here would create a second way to say the same thing, and one of
        # them would ignore the layer column.
        with self.assertRaises(ValueError):
            xtouch_constants.parse_control("34")
        self.assertEqual(xtouch_constants.parse_control("10"), 10)


class TestLayerTranslation(unittest.TestCase):
    """The X-Touch's numbers, in and out.

    Measured on the device: buttons send notes 8-23 on layer A and 32-47 on
    layer B, encoders CC 1-8 and CC 11-18, with the two FADERS at CC 9 and 10
    in the gap between the encoder blocks. That gap is the trap -- a range
    check across 1-18 would treat a fader as a ring.
    """

    def test_a_press_on_either_layer_is_the_same_control(self):
        for note, layer in ((8, 0), (32, 1), (23, 0), (47, 1)):
            control = xtouch_constants.to_control(note, layer)
            self.assertEqual(xtouch_constants.to_note(control, layer), note)
        self.assertEqual(xtouch_constants.to_control(34, 1),
                         xtouch_constants.to_control(10, 0))

    def test_the_layer_of_every_control(self):
        for note in list(range(0, 24)):
            self.assertEqual(xtouch_constants.layer_of("note", note), 0, note)
        for note in list(range(24, 48)):
            self.assertEqual(xtouch_constants.layer_of("note", note), 1, note)
        self.assertIsNone(xtouch_constants.layer_of("note", 60))

    def test_the_faders_sit_between_the_encoder_blocks(self):
        self.assertEqual(xtouch_constants.to_fader(9, 0), 9)    # layer A
        self.assertEqual(xtouch_constants.to_fader(10, 1), 9)   # layer B
        self.assertEqual(xtouch_constants.to_fader(1, 0), 1)
        self.assertEqual(xtouch_constants.to_fader(11, 1), 1)
        self.assertEqual(xtouch_constants.to_fader(18, 1), 8)
        # ...and a CC belonging to the OTHER layer is not one of ours.
        self.assertIsNone(xtouch_constants.to_fader(11, 0))
        self.assertIsNone(xtouch_constants.to_fader(1, 1))


class TestXTouchPolling(unittest.TestCase):
    def poll(self, *incoming):
        device, patch = make_xtouch(incoming)
        with patch:
            return device, device.poll()

    def test_a_press_arrives_as_a_layer_independent_control(self):
        _, events = self.poll(note_on(8))
        self.assertEqual(events, [("layer", 0), ("press", 8)])

    def test_the_layer_change_comes_before_what_revealed_it(self):
        # The order is the whole point. A press is resolved against
        # state["layer"], so if the press landed first it would resolve on
        # the OLD layer -- firing the other layer's binding once per switch,
        # silently, and only where the two layers disagree.
        _, events = self.poll(note_on(8), note_on(34))
        self.assertEqual(events, [("layer", 0), ("press", 8),
                                  ("layer", 1), ("press", 10)])

    def test_the_layer_is_reported_once_not_per_message(self):
        _, events = self.poll(note_on(8), note_off(8), note_on(9))
        self.assertEqual(events, [("layer", 0), ("press", 8),
                                  ("release", 8), ("press", 9)])

    def test_an_encoder_is_a_fader_on_both_layers(self):
        _, events = self.poll(control_change(3, 64))
        self.assertEqual(events, [("layer", 0), ("fader", 3, 64)])
        _, events = self.poll(control_change(13, 64))
        self.assertEqual(events, [("layer", 1), ("fader", 3, 64)])

    def test_the_fader_is_f9_on_both_layers(self):
        # So 'master' is spelled f9 here as it is on the APC.
        _, events = self.poll(control_change(9, 100))
        self.assertEqual(events, [("layer", 0), ("fader", 9, 100)])
        _, events = self.poll(control_change(10, 100))
        self.assertEqual(events, [("layer", 1), ("fader", 9, 100)])

    def test_a_note_off_and_a_zero_velocity_note_on_both_release(self):
        _, events = self.poll(note_on(8), note_off(8), note_on(8, velocity=0))
        self.assertEqual([e[0] for e in events],
                         ["layer", "press", "release", "release"])

    def test_messages_that_are_not_controls_are_ignored(self):
        _, events = self.poll(message("clock"), note_on(99),
                              control_change(64, 1))
        self.assertEqual(events, [])

    def test_the_layer_starts_unknown(self):
        device, _ = make_xtouch()
        self.assertIsNone(device.layer)


class TestXTouchPainting(unittest.TestCase):
    def lit(self, device):
        return [(m.note, m.velocity) for m in device.out.sent
                if m.type == "note_on"]

    def test_nothing_is_painted_until_the_layer_is_known(self):
        # Invariant 10 for a latching switch. Lighting the layer that
        # happens not to be in front of you is worse than a dark surface for
        # one gesture, and the first press fixes it.
        device, patch = make_xtouch()
        with patch:
            device.button(8, 1)
            device.ring(1, 64)
        self.assertEqual(device.out.sent, [])

    def test_a_button_is_lit_by_the_showing_layer_s_note(self):
        device, patch = make_xtouch([note_on(34)])
        with patch:
            device.poll()                       # now on layer B
            device.button(10, 1)
        self.assertEqual(self.lit(device), [(34, xtouch_constants.ON)])

    def test_the_cache_survives_a_layer_switch(self):
        # The device holds two LED surfaces and shows one, so what it was
        # told about the hidden layer is still true when that layer comes
        # back. Dropping the cache on a switch -- which is what apc.py's
        # refresh() does -- would be sixteen wasted messages for a picture
        # the device already has.
        device, patch = make_xtouch()
        with patch:
            device.inp.incoming = [note_on(8)]
            device.poll()
            device.button(8, 1)                 # lit on layer A
            device.inp.incoming = [note_on(32)]
            device.poll()                       # over to B
            device.inp.incoming = [note_on(8)]
            device.poll()                       # and back to A
            before = len(device.out.sent)
            device.button(8, 1)                 # already lit there
        self.assertEqual(len(device.out.sent), before)

    def test_each_layer_keeps_its_own_state(self):
        device, patch = make_xtouch()
        with patch:
            device.inp.incoming = [note_on(8)]
            device.poll()
            device.button(8, 1)
            device.inp.incoming = [note_on(32)]
            device.poll()
            device.button(8, 1)                 # same control, other layer
        self.assertEqual(self.lit(device),
                         [(8, xtouch_constants.ON), (32, xtouch_constants.ON)])

    def test_refresh_forgets_both_layers(self):
        # After a reload the bindings behind the hidden layer's lamps may
        # have changed too, and its cache would otherwise suppress the paint
        # that should happen when it next appears.
        device, patch = make_xtouch()
        with patch:
            device.inp.incoming = [note_on(8)]
            device.poll()
            device.button(8, 1)
            device.refresh()
            device.button(8, 1)
        self.assertEqual(len(self.lit(device)), 2)

    def test_a_ring_uses_the_showing_layer_s_cc(self):
        device, patch = make_xtouch()
        with patch:
            device.inp.incoming = [note_on(8)]
            device.poll()
            device.ring(3, 64)
            device.inp.incoming = [note_on(32)]
            device.poll()
            device.ring(3, 64)
        sent = [(m.control, m.value) for m in device.out.sent
                if m.type == "control_change"]
        self.assertEqual(sent, [(3, 64), (13, 64)])

    def test_ring_values_are_clamped(self):
        device, patch = make_xtouch([note_on(8)])
        with patch:
            device.poll()
            device.ring(1, 999)
            device.ring(2, -5)
        sent = [m.value for m in device.out.sent if m.type == "control_change"]
        self.assertEqual(sent, [127, 0])

    def test_everything_goes_out_on_the_mapped_channel(self):
        device, patch = make_xtouch([note_on(8)])
        with patch:
            device.poll()
            device.button(8, 1)
            device.ring(1, 64)
        for sent in device.out.sent:
            self.assertEqual(sent.channel, xtouch_constants.CHANNEL)

    def test_a_lamp_is_put_back_after_the_device_darkens_it(self):
        # THE BUG THIS EXISTS FOR: the buttons light themselves while held
        # and go dark on release, of their own accord -- the controller
        # sends one Note On and never turns it off. Left alone the diff
        # cache then believes the lamp is lit, nothing is re-sent, and a
        # scene runs all night behind a dark button.
        device, patch = make_xtouch([note_on(8)])
        with patch:
            device.poll()
            device.button(8, 1)
            before = len(self.lit(device))
            device.inp.incoming = [note_off(8)]
            device.poll()
        self.assertEqual(self.lit(device)[before:],
                         [(8, xtouch_constants.ON)])

    def test_a_dark_lamp_is_not_lit_by_being_pressed(self):
        # The re-assert sends what the SHOW wants, not what the device did.
        device, patch = make_xtouch([note_on(8)])
        with patch:
            device.poll()
            device.button(8, 0)
            before = len(device.out.sent)
            device.inp.incoming = [note_off(8)]
            device.poll()
        self.assertEqual([(m.note, m.velocity)
                          for m in device.out.sent[before:]],
                         [(8, xtouch_constants.OFF)])

    def test_nothing_is_sent_while_the_button_is_held(self):
        # Only on release. Fighting the device for the lamp under your
        # finger would make the press feel dead, and that local flash is
        # decent press feedback in its own right.
        device, patch = make_xtouch()
        with patch:
            device.inp.incoming = [note_on(8)]
            device.poll()
            device.button(8, 1)
            before = len(device.out.sent)
            device.inp.incoming = [note_on(8)]
            device.poll()
        self.assertEqual(device.out.sent[before:], [])

    def test_an_encoder_push_has_no_lamp_to_put_back(self):
        device, patch = make_xtouch([note_on(0)])
        with patch:
            device.poll()
            before = len(device.out.sent)
            device.inp.incoming = [note_off(0)]
            device.poll()
        self.assertEqual(device.out.sent[before:], [])

    def test_turning_a_knob_invalidates_what_we_believe_its_ring_holds(self):
        # The device drives the ring itself as the knob turns, so the cache
        # goes stale exactly as it does for a pressed button -- and would
        # then suppress the repaint that should correct it.
        device, patch = make_xtouch([note_on(8)])
        with patch:
            device.poll()
            device.ring(3, 64)
            before = len(device.out.sent)
            device.ring(3, 64)                  # cached, sends nothing
            self.assertEqual(device.out.sent[before:], [])
            device.inp.incoming = [control_change(3, 100)]
            device.poll()
            device.ring(3, 64)                  # cache dropped, re-sent
        self.assertEqual([(m.control, m.value)
                          for m in device.out.sent[before:]], [(3, 64)])

    def test_the_fader_has_no_ring_to_invalidate(self):
        device, patch = make_xtouch([note_on(8)])
        with patch:
            device.poll()
            device.inp.incoming = [control_change(9, 100)]
            events = device.poll()
        self.assertEqual(events, [("fader", 9, 100)])

    def test_asking_for_a_colour_pad_is_an_error_not_a_no_op(self):
        # build_leds only calls pad() when PADS is non-empty, so reaching
        # here means a caller assumed a grid. Silence would hide that until
        # someone wondered why their pads were dark.
        device, patch = make_xtouch()
        with patch, self.assertRaises(XTOUCH_MODULE.XTouchError):
            device.pad(8, 5)

    def test_there_is_nothing_to_ask_about_fader_positions(self):
        # No Introduction message on this device, so by invariant 10 the
        # master starts at 0 and the first move syncs it.
        device, _ = make_xtouch()
        self.assertIsNone(device.introduce())


class TestAPCPolling(unittest.TestCase):
    """SHIFT becomes a layer event before it reaches the controller.

    This is the path that ran a six-hour show, so the behaviour must be
    identical -- only the shape of the event changed. handle() no longer has
    a SHIFT branch, so if the surface stopped translating, the modifier would
    arrive as an ordinary press of note 122 and read as an unbound pad.
    """

    def poll_real(self, *incoming):
        device = APC_MODULE.APC.__new__(APC_MODULE.APC)
        device.inp = StubPort(incoming)
        return device.poll()

    def poll_sim(self, *payloads):
        import virtualapc
        device = virtualapc.VirtualAPC.__new__(virtualapc.VirtualAPC)
        device.link = types.SimpleNamespace(drain=lambda: list(payloads))
        device._led = {}
        device._pending_faders = None
        return device.poll()

    def test_shift_is_a_layer_change_not_a_press(self):
        self.assertEqual(self.poll_real(note_on(surface_constants.SHIFT)),
                         [("layer", 1)])
        self.assertEqual(self.poll_real(note_off(surface_constants.SHIFT)),
                         [("layer", 0)])

    def test_ordinary_pads_are_untouched(self):
        self.assertEqual(self.poll_real(note_on(0), note_off(0)),
                         [("press", 0), ("release", 0)])

    def test_faders_keep_their_numbering(self):
        self.assertEqual(self.poll_real(control_change(0x30, 64)),
                         [("fader", 1, 64)])
        self.assertEqual(self.poll_real(control_change(0x38, 64)),
                         [("fader", 9, 64)])

    def test_the_simulator_translates_it_the_same_way(self):
        import simlink
        self.assertEqual(
            self.poll_sim(bytes([simlink.PRESS, surface_constants.SHIFT])),
            [("layer", 1)])
        self.assertEqual(
            self.poll_sim(bytes([simlink.RELEASE, surface_constants.SHIFT])),
            [("layer", 0)])
        self.assertEqual(self.poll_sim(bytes([simlink.PRESS, 5])),
                         [("press", 5)])


class TestLayerColumn(unittest.TestCase):
    """'shift' and 'layer' are two spellings of one idea."""

    def load(self, mapping, surface=None):
        path = helper.temp_show(mapping=mapping)
        self.addCleanup(shutil.rmtree, path)
        show = showfile.Show(path, surface=surface)
        show.load()
        return show

    def test_shift_still_means_the_second_layer(self):
        show = self.load("pad,type,target,mode,colour,shift\n"
                         "r0c0,scene,warm,toggle,red,\n"
                         "r0c0,scene,half,toggle,blue,yes\n")
        self.assertEqual(show.binding_for(0, 0).target, "warm")
        self.assertEqual(show.binding_for(0, 1).target, "half")

    def test_layer_is_the_general_spelling(self):
        show = self.load("pad,type,target,mode,colour,layer\n"
                         "r0c0,scene,warm,toggle,red,\n"
                         "r0c0,scene,half,toggle,blue,shift\n")
        self.assertEqual(show.binding_for(0, 1).target, "half")

    def test_setting_both_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.load("pad,type,target,mode,colour,shift,layer\n"
                      "r0c0,scene,warm,toggle,red,yes,shift\n")
        self.assertIn("not both", str(caught.exception))

    def test_a_layer_the_device_does_not_have_is_fatal(self):
        # Structural, not drift: the file is asking for something that
        # cannot exist, and guessing which layer was meant is worse than
        # stopping.
        with self.assertRaises(ValueError) as caught:
            self.load("pad,type,target,mode,colour,layer\n"
                      "r0c0,scene,warm,toggle,red,b\n")
        self.assertIn("shift", str(caught.exception))

    def test_a_layered_fader_is_refused_where_faders_have_no_layer(self):
        # The APC's faders send one CC whether or not SHIFT is held, so the
        # row could never be reached. Failing at load beats a dead row.
        with self.assertRaises(ValueError) as caught:
            self.load("pad,type,target,mode,colour,shift\n"
                      "f1,level,par*.dimmer,,,yes\n")
        self.assertIn("no layer", str(caught.exception))

    def test_a_held_modifier_falls_through_to_the_base_layer(self):
        # SHIFT is a way to reach a few extra things. Losing every other pad
        # for as long as you hold it would be absurd, so an unbound shifted
        # pad still does what it does unshifted.
        show = self.load("pad,type,target,mode,colour,shift\n"
                         "r0c0,scene,warm,toggle,red,\n")
        self.assertEqual(show.binding_for(0, 1).target, "warm")
        self.assertEqual(show.layer(1)[0].target, "warm")

    def test_a_latching_layer_does_not(self):
        # The X-Touch's layer button LATCHES: page B is a page you stay on.
        # Inheriting page A wherever B is blank means an unbound button
        # fires something -- pressing p8 on B ran layer A's reload.
        import os
        path = helper.temp_show(mapping="")
        self.addCleanup(shutil.rmtree, path)
        with open(os.path.join(path, "mapping-xtouch.csv"), "w") as handle:
            handle.write("pad,type,target,mode,layer\n"
                         "bt1,scene,warm,toggle,a\n")
        show = showfile.Show(path, surface=xtouch_constants)
        show.load()
        self.assertEqual(show.binding_for(8, 0).target, "warm")
        self.assertIsNone(show.binding_for(8, 1))

    def test_the_lamps_agree_with_what_will_fire(self):
        # layer() paints and binding_for fires; if they disagreed a lamp
        # would advertise a binding that does nothing, which is a worse lie
        # than a dark button.
        import os
        path = helper.temp_show(mapping="")
        self.addCleanup(shutil.rmtree, path)
        with open(os.path.join(path, "mapping-xtouch.csv"), "w") as handle:
            handle.write("pad,type,target,mode,layer\n"
                         "bt1,scene,warm,toggle,a\n"
                         "bt2,scene,half,toggle,b\n")
        show = showfile.Show(path, surface=xtouch_constants)
        show.load()
        for index in (0, 1):
            painted = show.layer(index)
            for control in xtouch_constants.BUTTONS:
                self.assertEqual(control in painted,
                                 show.binding_for(control, index) is not None,
                                 f"layer {index}, control {control}")

    def test_a_master_missing_from_a_page_is_reported(self):
        # The cost of no fall-through: binding the master on page A alone
        # leaves the fader dead on page B. Load time is the place to find
        # that out, not mid-set.
        import os
        path = helper.temp_show(mapping="")
        self.addCleanup(shutil.rmtree, path)
        with open(os.path.join(path, "mapping-xtouch.csv"), "w") as handle:
            handle.write("pad,type,target,mode,layer\n"
                         "f1,master,,,a\n")
        show = showfile.Show(path, surface=xtouch_constants)
        show.load()
        said = [w for w in show.warnings if "master" in w]
        self.assertEqual(len(said), 1, said)
        self.assertIn("layer b", said[0])

    def test_an_unknown_layer_resolves_nothing(self):
        # The X-Touch at startup. Not a fallback to layer 0: that would fire
        # the base binding for a press made on a layer we cannot identify.
        show = self.load("pad,type,target,mode,colour\n"
                         "r0c0,scene,warm,toggle,red\n")
        self.assertIsNone(show.binding_for(0, None))
        self.assertEqual(show.layer(None), {})


class TestFaderLayers(unittest.TestCase):
    """Encoder 3 on layer A and encoder 3 on layer B are two controls.

    They are on the X-Touch, where the device remembers a position per
    layer. One key per number would silently merge them -- and merge their
    engine entries too, so moving one would drive the other's channels.
    """

    MAPPING = ("pad,type,target,layer\n"
               "e1,level,par*.dimmer,a\n"
               "e1,scale,par*.dimmer,b\n"
               "f1,master,,a\n")

    def setUp(self):
        self.path = helper.temp_show(mapping="")
        self.addCleanup(shutil.rmtree, self.path)
        import os
        with open(os.path.join(self.path, "mapping-xtouch.csv"), "w") as f:
            f.write(self.MAPPING)
        self.show = showfile.Show(self.path, surface=xtouch_constants)
        self.show.load()

    def test_both_layers_bind_the_same_encoder(self):
        self.assertEqual(self.show.fader_for(1, 0).kind, "level")
        self.assertEqual(self.show.fader_for(1, 1).kind, "scale")

    def test_they_drive_the_engine_under_separate_keys(self):
        import engine as engine_mod
        eng = engine_mod.Engine(self.show.patch, self.show.scenes,
                                self.show.chasers)
        state = {"master_pending": None, "bpm_pending": None,
                 "internal": None}
        controller.apply_fader(1, 0, 127, self.show, eng, state)
        controller.apply_fader(1, 1, 64, self.show, eng, state)
        self.assertIn((1, 0), eng.levels)
        self.assertIn((1, 1), eng.scales)

    def test_a_binding_on_one_layer_does_not_serve_the_other(self):
        # No fall-through on a LATCHING layer: page B is a page, not a
        # modifier, and inheriting page A's bindings wherever B is blank
        # fires the wrong thing -- an unbound button on B ran A's reload.
        # The cost is that a control wanted on both layers is bound twice,
        # which is one row and makes the file say what the surface does.
        self.assertEqual(self.show.fader_for(9, 0).kind, "master")
        self.assertIsNone(self.show.fader_for(9, 1))


class TestPaintingAnXTouchLayout(unittest.TestCase):
    """build_leds against a real X-Touch mapping, through the surface API.

    The unit tests above check each piece; this checks that controller.py
    asks for the right things. It is the seam where a surface with no grid
    and a surface with no rings both have to work, and where the wrong
    answer is silence rather than an exception.
    """

    MAPPING = ("pad,type,target,mode,layer\n"
               "bt1,scene,warm,toggle,a\n"
               "bt2,scene,half,toggle,a\n"
               "bt1,scene,half,toggle,b\n"
               "p1,clear,,,a\n"
               "e1,level,par*.dimmer,,a\n"
               "f1,master,,,a\n")

    class Recorder:
        """A surface that records what it was asked to paint."""

        def __init__(self):
            self.buttons = {}
            self.rings = {}
            self.pads = []

        def button(self, control, state=1, force=False):
            self.buttons[control] = state

        def ring(self, number, value, force=False):
            self.rings[number] = value

        def pad(self, *args, **kwargs):
            self.pads.append(args)

    def setUp(self):
        import os
        import engine as engine_mod
        self.path = helper.temp_show(mapping="")
        self.addCleanup(shutil.rmtree, self.path)
        with open(os.path.join(self.path, "mapping-xtouch.csv"), "w") as f:
            f.write(self.MAPPING)
        self.show = showfile.Show(self.path, surface=xtouch_constants)
        self.show.load()
        self.eng = engine_mod.Engine(self.show.patch, self.show.scenes,
                                     self.show.chasers)
        self.previous = controller._SURFACE_MODULE
        controller._SURFACE_MODULE = xtouch_constants
        self.addCleanup(setattr, controller, "_SURFACE_MODULE", self.previous)

    def paint(self, layer):
        surface = self.Recorder()
        controller.build_leds(surface, self.show, self.eng, "intensity", layer)
        return surface

    def test_no_colour_pads_are_ever_asked_for(self):
        # PADS is empty, so the pad loop must not run at all -- xtouch.pad()
        # raises, deliberately, and reaching it would take the show down.
        self.assertEqual(self.paint(0).pads, [])

    def test_an_unknown_layer_paints_nothing(self):
        surface = self.paint(None)
        self.assertEqual(surface.buttons, {})
        self.assertEqual(surface.rings, {})

    def test_bound_buttons_are_dark_until_their_target_is_active(self):
        # BUTTON_SHOWS is "active" here: a binary lamp cannot say "bound"
        # and "running" at once, and running is the half worth seeing.
        surface = self.paint(0)
        self.assertEqual(surface.buttons[8], 0)
        self.eng.activate("warm")
        self.assertEqual(self.paint(0).buttons[8], 1)

    def test_every_button_is_painted_including_the_unbound_ones(self):
        # Unbound must be explicitly dark, not merely unmentioned: the
        # device remembers whatever it was last told.
        surface = self.paint(0)
        self.assertEqual(set(surface.buttons), set(xtouch_constants.BUTTONS))

    def test_the_other_layer_is_not_painted(self):
        self.eng.activate("half")
        # bt1 on layer b is 'half'; on layer a it is 'warm'.
        self.assertEqual(self.paint(1).buttons[8], 1)
        self.assertEqual(self.paint(0).buttons[8], 0)

    def test_a_level_encoder_shows_the_engine_s_value_not_the_knob_s(self):
        # The point of ring feedback: after a reload the device still holds
        # wherever the knob was turned, and the show may disagree.
        binding = self.show.fader_for(1, 0)
        self.eng.set_level((1, 0), binding.channels, 255)
        self.assertEqual(self.paint(0).rings[1], 127)

    def test_an_unbound_encoder_ring_is_left_alone(self):
        # Not zeroed: the device drives its own rings when the user turns
        # them, and blanking one every repaint would fight the hardware.
        self.assertNotIn(2, self.paint(0).rings)

    def test_the_master_encoder_would_show_the_master(self):
        # f1 is the fader, not a ring, so nothing is painted for it here --
        # but fader_value must still answer, since which controls have rings
        # is the surface's business and another device may differ.
        binding = self.show.fader_for(9, 0)
        self.eng.set_master(255)
        self.assertEqual(controller.fader_value(binding, 9, self.eng), 127)
        self.eng.set_master(0)
        self.assertEqual(controller.fader_value(binding, 9, self.eng), 0)


class TestArgumentChecking(unittest.TestCase):
    """An unknown flag stops the program instead of meaning nothing.

    This is the failure it exists to stop: --surface was typed against a
    build that did not have it yet, the flag was ignored in silence, and the
    APC started and failed on a missing port. The error pointed at the
    hardware. A typo does exactly the same thing.
    """

    def refuse(self, *args):
        with self.assertRaises(SystemExit) as caught:
            controller.check_args(list(args))
        return str(caught.exception)

    def test_an_unknown_flag_is_refused(self):
        self.assertIn("--surfce", self.refuse("--surfce", "xtouch"))

    def test_a_near_miss_is_named(self):
        self.assertIn("Did you mean --surface?", self.refuse("--surfce"))

    def test_nothing_close_offers_no_guess(self):
        # Suggesting --no-dmx for --nonsense because they share "--no" is
        # noise dressed as help.
        self.assertNotIn("Did you mean", self.refuse("--nonsense"))

    def test_every_documented_flag_is_accepted(self):
        controller.check_args(["--check", "--sim", "--no-dmx", "--no-midi",
                               "--beats", "--watch"])

    def test_a_flag_s_value_is_not_mistaken_for_a_flag(self):
        controller.check_args(["--surface", "xtouch"])
        controller.check_args(["--feedback", "pulse", "--check"])

    def test_an_optional_value_may_be_left_out(self):
        # --os2l and --monitor both have useful defaults, so the next
        # argument is only swallowed when it is not itself a flag.
        controller.check_args(["--os2l", "--no-dmx"])
        controller.check_args(["--os2l", "9000", "--no-dmx"])
        controller.check_args(["--monitor", "--watch"])
        controller.check_args(["--monitor", "host:9002"])

    def test_the_flag_list_matches_what_the_help_promises(self):
        # The usage text at the top of the file is what people read; a flag
        # in one and not the other is a lie in whichever they trust.
        for flag in controller.FLAGS:
            self.assertIn(flag, controller.__doc__, flag)


class TestSoftwareBlink(unittest.TestCase):
    """Blinking a lamp the device cannot blink.

    The X-Touch's LEDs are binary in Standard mode, and the mode that can
    blink them costs the absolute encoders. So --feedback blink toggles the
    lamp from the main loop instead. The phase is DERIVED from the clock,
    never counted, for the same reason chaser position is: a repaint only
    happens when something changes, and a counter would drift against it.
    """

    def phase(self, style, now):
        return controller.blink_phase(style, xtouch_constants, now)

    def test_a_hardware_blink_is_left_to_the_hardware(self):
        self.assertIsNone(controller.blink_phase("blink", surface_constants))
        self.assertIsNone(controller.blink_phase("intensity",
                                                 xtouch_constants))

    def test_the_phase_alternates_at_the_declared_rate(self):
        rate = xtouch_constants.SOFT_BLINK["blink"]
        half = 1.0 / (rate * 2)
        self.assertNotEqual(self.phase("blink", 0.0),
                            self.phase("blink", half * 1.05))
        self.assertEqual(self.phase("blink", 0.0),
                         self.phase("blink", half * 2.05))

    def test_fast_blink_is_faster(self):
        # Two styles that ticked at the same rate would be two names for one
        # thing, and the difference is unnoticeable across a dark room.
        slow = [self.phase("blink", t / 100) for t in range(100)]
        fast = [self.phase("fast-blink", t / 100) for t in range(100)]
        flips = lambda seq: sum(a != b for a, b in zip(seq, seq[1:]))
        self.assertGreater(flips(fast), flips(slow))

    def test_the_phase_is_derived_from_the_clock_not_counted(self):
        # Same instant, same answer, however many times it is asked -- so a
        # repaint driven by an unrelated event cannot advance the blink.
        for now in (0.0, 1.234, 99.5):
            self.assertEqual(self.phase("blink", now), self.phase("blink", now))


class TestBlinkingALayout(unittest.TestCase):
    """What actually reaches the lamps when a blink style is running."""

    MAPPING = ("pad,type,target,mode,layer\n"
               "bt1,scene,warm,toggle,a\n"
               "bt2,scene,half,toggle,a\n")

    def setUp(self):
        import os
        import engine as engine_mod
        self.path = helper.temp_show(mapping="")
        self.addCleanup(shutil.rmtree, self.path)
        with open(os.path.join(self.path, "mapping-xtouch.csv"), "w") as f:
            f.write(self.MAPPING)
        self.show = showfile.Show(self.path, surface=xtouch_constants)
        self.show.load()
        self.eng = engine_mod.Engine(self.show.patch, self.show.scenes,
                                     self.show.chasers)
        self.previous = controller._SURFACE_MODULE
        controller._SURFACE_MODULE = xtouch_constants
        self.addCleanup(setattr, controller, "_SURFACE_MODULE", self.previous)

    def paint(self, style, now):
        surface = TestPaintingAnXTouchLayout.Recorder()
        controller.build_leds(surface, self.show, self.eng, style, 0, now)
        return surface.buttons

    def test_an_active_lamp_alternates(self):
        self.eng.activate("warm")
        half = 1.0 / (xtouch_constants.SOFT_BLINK["blink"] * 2)
        first = self.paint("blink", 0.0)[8]
        second = self.paint("blink", half * 1.05)[8]
        self.assertNotEqual(first, second)

    def test_an_inactive_lamp_stays_dark_through_both_halves(self):
        # Blinking every BOUND button would turn the surface into a strobe
        # that says nothing. Only what is running blinks.
        half = 1.0 / (xtouch_constants.SOFT_BLINK["blink"] * 2)
        for moment in (0.0, half * 1.05):
            self.assertEqual(self.paint("blink", moment)[9], 0)

    def test_intensity_never_blinks(self):
        self.eng.activate("warm")
        half = 1.0 / (xtouch_constants.SOFT_BLINK["blink"] * 2)
        for moment in (0.0, half * 1.05, half * 2.05):
            self.assertEqual(self.paint("intensity", moment)[8], 1)
