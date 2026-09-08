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


def make_xtouch(incoming=()):
    """An XTouch wired to stub ports, with mido stubbed out for its sends."""
    import xtouch

    class Message(types.SimpleNamespace):
        def __init__(self, type, **fields):
            super().__init__(type=type, **fields)

    device = xtouch.XTouch.__new__(xtouch.XTouch)
    device.inp = StubPort(incoming)
    device.out = StubPort()
    device.layer = xtouch_constants.LAYER_AT_START
    device._delivered = [{} for _ in range(xtouch_constants.LAYERS)]
    patch = mock.patch.dict("sys.modules",
                            {"mido": types.SimpleNamespace(Message=Message)})
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
             "OFF", "IDLE", "FEEDBACK",
             "layer_index", "parse_control", "describe_control")

    def modules(self):
        import virtualapc
        return (surface_constants, xtouch_constants, virtualapc)

    def test_every_surface_carries_the_whole_vocabulary(self):
        for module in self.modules():
            for name in self.NAMES:
                self.assertTrue(hasattr(module, name),
                                f"{module.__name__} lacks {name}")

    def test_a_driver_re_exports_it_so_either_can_be_held(self):
        # surface_vocab() returns the driver when one is loaded, so the two
        # must agree -- that is what lets the tests inject virtualapc and
        # controller.py read PADS off it without knowing.
        import virtualapc
        for name in self.NAMES:
            self.assertEqual(getattr(virtualapc, name),
                             getattr(surface_constants, name), name)

    def test_feedback_lists_only_what_the_surface_can_do(self):
        # The X-Touch's lamps are binary -- measured velocity by velocity.
        # Offering 'pulse' would be a lie that showed up only as a pad that
        # never animated, so --feedback rejects it at startup instead.
        self.assertIn("pulse", surface_constants.FEEDBACK)
        self.assertEqual(list(xtouch_constants.FEEDBACK), ["intensity"])

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

    def test_asking_for_a_colour_pad_is_an_error_not_a_no_op(self):
        # build_leds only calls pad() when PADS is non-empty, so reaching
        # here means a caller assumed a grid. Silence would hide that until
        # someone wondered why their pads were dark.
        import xtouch
        device, patch = make_xtouch()
        with patch, self.assertRaises(xtouch.XTouchError):
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
        import apc
        device = apc.APC.__new__(apc.APC)
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

    def test_the_second_layer_falls_through_to_the_first(self):
        show = self.load("pad,type,target,mode,colour,shift\n"
                         "r0c0,scene,warm,toggle,red,\n")
        self.assertEqual(show.binding_for(0, 1).target, "warm")

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

    def test_a_binding_on_the_base_layer_serves_both(self):
        # f1 is bound on layer a only, so it stays the master when the
        # device is showing b -- the fall-through that stops a layer switch
        # taking the master away.
        self.assertEqual(self.show.fader_for(9, 1).kind, "master")


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
