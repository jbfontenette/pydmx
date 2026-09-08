"""The one piece of X-Touch probe logic that is not just I/O.

xtouch_dump.classify_encoder decides whether the encoders report a position
or a step, and that answer decides whether encoder bindings can reuse
apply_fader() or need a kind of their own. Getting it wrong would send the
driver design down the wrong road, so it is a pure function and it is tested
here -- no device, no mido.
"""

import contextlib
import io
import types
import unittest
from unittest import mock

import helper

import xtouch_dump


def verdict(values):
    return xtouch_dump.classify_encoder(values)[0]


class TestEncoderClassification(unittest.TestCase):
    def test_a_full_sweep_reads_as_absolute(self):
        # What a fader-like encoder does: walks the range, visits each value
        # about once, stops at the ends.
        self.assertEqual(verdict(list(range(0, 128, 2))), "absolute")

    def test_single_clicks_read_as_relative(self):
        # One click right, one click left, over and over. Two values however
        # long you turn -- no position could look like this.
        self.assertEqual(verdict([1] * 5 + [127] * 5), "relative")

    def test_fast_turns_still_read_as_relative(self):
        self.assertEqual(verdict([1, 2, 3, 63, 65, 66, 67, 127]), "relative")

    def test_binary_offset_reads_as_relative(self):
        # Centred on 64: above is one way, below is the other.
        self.assertEqual(verdict([65, 66, 67, 63, 62, 61] * 3), "relative")

    def test_a_short_absolute_sweep_is_not_mistaken_for_binary_offset(self):
        # The trap: a half-turn of an ABSOLUTE encoder near the middle sits
        # either side of 64, exactly where binary offset lives. What tells
        # them apart is repetition -- a sweep visits each value once.
        self.assertEqual(verdict([60, 61, 62, 63, 64, 65, 66, 67]),
                         "absolute")

    def test_real_x_touch_mini_captures_read_as_absolute(self):
        """Actual data off an X-Touch Mini, 2026-09-08.

        Kept verbatim because it is the only ground truth this project has
        for the device: the encoders report a POSITION, saturating at 0 and
        sweeping to 127, which is what Standard mode does. CC 3 is the case
        that mattered -- a partial turn of ~30 values, which the first
        version of this classifier shrugged at and called unknown. A slow
        careful turn is exactly how someone would test an encoder, so
        shrugging at it was the wrong answer to the most likely input.
        """
        cc1 = [1, 4, 8, 13, 18, 23, 29, 34, 39, 42, 45, 44, 39, 34, 29, 24,
               19, 14, 9, 5, 2, 0, 2]
        cc3 = [1, 4, 7, 11, 12, 15, 19, 22, 23, 26, 25, 22, 19, 16, 15, 12,
               11, 10, 11, 14, 17, 20, 23, 24, 26, 27, 28, 31, 30, 26, 22,
               18, 15]
        cc4 = [0, 0, 0, 0, 0, 0, 0, 0, 1, 4, 7, 10, 13, 16, 19, 22, 25, 26,
               29, 31, 32, 35, 37, 38, 41, 44, 47, 50, 51, 54, 57, 60, 63,
               66, 67, 70, 73, 74, 73, 70, 71, 74, 77, 80, 83, 82, 81, 78,
               75, 71, 69, 68, 67, 64, 63]
        for name, values in (("cc1", cc1), ("cc3", cc3), ("cc4", cc4)):
            self.assertEqual(verdict(values), "absolute", name)

    def test_the_real_fader_sweep_reads_as_absolute(self):
        # CC 9, the fader, end to end. Every single value, no gaps.
        self.assertEqual(verdict(list(range(8, 128)) + list(range(126, -1, -1))),
                         "absolute")

    def test_too_little_movement_admits_it(self):
        self.assertEqual(verdict([64, 65]), "unknown")
        self.assertIn("turn it more",
                      xtouch_dump.classify_encoder([64, 65])[1].lower())

    def test_every_verdict_explains_itself(self):
        # The explanation is the whole point -- a bare label would not tell
        # you what to do next.
        for values in ([1] * 6, list(range(0, 128, 2)), [64, 65],
                       [65, 66, 63, 62] * 3):
            _, detail = xtouch_dump.classify_encoder(values)
            self.assertTrue(detail.strip())
            self.assertGreater(len(detail), 20, values)


class TestControlMap(unittest.TestCase):
    """The map measured off the device on 2026-09-08.

    Pinned because one part of it is counter-intuitive and was got wrong on
    the first pass: the notes offset cleanly by +24 between layers, but the
    CCs do NOT. The two faders sit adjacent at 9 and 10 with an encoder block
    either side, so the tidy "+9 for everything" guess put the layer B fader
    on CC 18 -- and the device says 10.
    """

    def test_notes_offset_by_24_between_layers(self):
        for number in range(24):
            a = xtouch_dump.name_for("note", number)
            b = xtouch_dump.name_for("note", number + 24)
            self.assertIsNotNone(a, number)
            self.assertEqual(a[0], b[0], number)      # same control
            self.assertEqual((a[1], b[1]), ("A", "B"), number)

    def test_the_faders_are_adjacent_not_offset(self):
        self.assertEqual(xtouch_dump.name_for("cc", 9), ("fader", "A"))
        self.assertEqual(xtouch_dump.name_for("cc", 10), ("fader", "B"))
        # CC 18 is the last layer B ENCODER, not the fader the offset
        # pattern would predict.
        self.assertEqual(xtouch_dump.name_for("cc", 18), ("encoder 8", "B"))

    def test_encoder_blocks_sit_either_side_of_the_faders(self):
        self.assertEqual(xtouch_dump.name_for("cc", 1), ("encoder 1", "A"))
        self.assertEqual(xtouch_dump.name_for("cc", 8), ("encoder 8", "A"))
        self.assertEqual(xtouch_dump.name_for("cc", 11), ("encoder 1", "B"))

    def test_the_three_note_blocks(self):
        self.assertEqual(xtouch_dump.name_for("note", 0),
                         ("encoder 1 push", "A"))
        self.assertEqual(xtouch_dump.name_for("note", 8), ("button top 1", "A"))
        self.assertEqual(xtouch_dump.name_for("note", 16),
                         ("button bottom 1", "A"))
        self.assertEqual(xtouch_dump.name_for("note", 47),
                         ("button bottom 8", "B"))

    def test_unknown_numbers_are_admitted_not_guessed(self):
        self.assertIsNone(xtouch_dump.name_for("note", 48))
        self.assertIsNone(xtouch_dump.name_for("cc", 0))
        self.assertIsNone(xtouch_dump.name_for("cc", 19))


class TestOutputChannel(unittest.TestCase):
    """The LED tool must send where the device listens.

    It did not. Every LED test sent on channel 0 while the device lives on
    channel 10, so the first run on real hardware lit nothing at all and the
    question it was meant to answer went unanswered. The channel was written
    down in two places and only one of them was right -- the same
    duplication-drift as REVIEW item 16.
    """

    def test_the_led_tool_uses_the_mapped_channel(self):
        import xtouch_leds
        self.assertEqual(xtouch_leds.CHANNEL, xtouch_dump.CHANNEL)

    def test_note_and_cc_default_to_it(self):
        import inspect
        import xtouch_leds
        for function in (xtouch_leds.note, xtouch_leds.cc):
            default = inspect.signature(function).parameters["channel"].default
            self.assertEqual(default, xtouch_dump.CHANNEL, function.__name__)


class TestLearnWalk(unittest.TestCase):
    """The buffer-then-drain trick at the heart of --learn.

    Pressing a control and THEN hitting Enter is what makes the guided walk
    work without threads: the messages queue in the port while input() is
    blocked, and get drained afterwards. If that attribution slipped by one,
    every control would be recorded against the wrong name and the driver
    would be written from a wrong map -- worse than having no map at all.
    """

    class StubPort:
        """Stands in for a mido input port. Yields one batch per drain."""

        def __init__(self, batches):
            self.batches = list(batches)

        def iter_pending(self):
            batch = self.batches.pop(0) if self.batches else []
            return iter(batch)

    @staticmethod
    def _one_batch(port, prompt):
        """Stands in for collect_until_enter: one prompt, one drained batch.

        Patched rather than driven through stdin because the real function
        polls sys.stdin with select, which is not something a test should be
        pretending to be. What these tests are for is what learn_layer does
        with the batch it gets back.
        """
        return list(port.iter_pending())

    @staticmethod
    def note(number, channel=0):
        return types.SimpleNamespace(type="note_on", note=number,
                                     velocity=127, channel=channel)

    @staticmethod
    def control(number, channel=10):
        return types.SimpleNamespace(type="control_change", control=number,
                                     value=64, channel=channel)

    def walk(self, batches):
        port = self.StubPort(batches)
        with mock.patch.object(xtouch_dump, "collect_until_enter",
                               self._one_batch), \
                contextlib.redirect_stdout(io.StringIO()):
            return xtouch_dump.learn_layer(port, "A")

    def test_each_control_records_what_arrived_before_its_enter(self):
        found = self.walk([[self.note(89)], [self.note(90)]])
        items = xtouch_dump.checklist()
        self.assertEqual(found[items[0]], ("note", 89, 0))
        self.assertEqual(found[items[1]], ("note", 90, 0))

    def test_a_silent_prompt_records_nothing(self):
        # Blank Enter with nothing touched must skip, not inherit the
        # previous control's message.
        found = self.walk([[self.note(89)], [], [self.note(91)]])
        items = xtouch_dump.checklist()
        self.assertIn(items[0], found)
        self.assertNotIn(items[1], found)
        self.assertEqual(found[items[2]], ("note", 91, 0))

    def test_only_the_first_message_of_a_batch_counts(self):
        # A button sends press AND release, and a fumbled prompt can catch a
        # neighbouring control too. The FIRST message identifies what was
        # asked for; anything after it is noise, so the batch must not be
        # read from the wrong end.
        found = self.walk([[self.note(89), self.note(90), self.note(91)]])
        self.assertEqual(found[xtouch_dump.checklist()[0]], ("note", 89, 0))

    def test_cc_controls_are_recorded_with_their_channel(self):
        found = self.walk([[self.control(16, channel=10)]])
        self.assertEqual(found[xtouch_dump.checklist()[0]], ("cc", 16, 10))


class TestOutputMap(unittest.TestCase):
    """The device listens where it speaks -- measured, against the manual.

    Behringer's X-Touch Editor says button LEDs listen on notes 0-15, that
    program change selects the layer, and that a ring takes a behaviour CC
    and a value CC. All three were tested on the device and none is true of
    this unit in Standard mode: notes 8-23 light the buttons, program change
    moves nothing, and CC 9-10 are the faders. The editor was believed for
    one commit and it put two correct measurements in doubt, so what the
    hardware said is pinned here.
    """

    def test_an_led_note_is_the_note_the_button_sends(self):
        for layer in ("A", "B"):
            sends = set(xtouch_dump.BUTTONS_TOP[layer]) | set(
                xtouch_dump.BUTTONS_BOTTOM[layer])
            self.assertEqual(sends, set(xtouch_dump.LED_NOTE[layer]))

    def test_the_two_layers_do_not_share_led_notes(self):
        # Which is why a note for the inactive layer addresses something
        # real and is nonetheless discarded, rather than lighting the wrong
        # lamp on the layer that is showing.
        self.assertFalse(set(xtouch_dump.LED_NOTE["A"]) &
                         set(xtouch_dump.LED_NOTE["B"]))

    def test_led_note_names_a_button_by_row_and_position(self):
        self.assertEqual(xtouch_dump.led_note("A", "top", 1), 8)
        self.assertEqual(xtouch_dump.led_note("A", "bottom", 1), 16)
        self.assertEqual(xtouch_dump.led_note("B", "top", 1), 32)
        for index in (0, 9):
            with self.assertRaises(ValueError):
                xtouch_dump.led_note("A", "top", index)

    def test_a_ring_listens_on_its_encoders_transmit_cc(self):
        # The whole surface reduces to this: send the number the control
        # SENDS, on the layer that is showing. Buttons and rings both.
        for layer in ("A", "B"):
            self.assertEqual(xtouch_dump.RING_CC[layer],
                             xtouch_dump.ENCODER_CC[layer])
        self.assertEqual(xtouch_dump.ring_cc("A", 1), 1)
        self.assertEqual(xtouch_dump.ring_cc("A", 8), 8)
        self.assertEqual(xtouch_dump.ring_cc("B", 1), 11)
        for index in (0, 9):
            with self.assertRaises(ValueError):
                xtouch_dump.ring_cc("A", index)

    def test_the_two_layers_do_not_share_ring_ccs(self):
        self.assertFalse(set(xtouch_dump.RING_CC["A"]) &
                         set(xtouch_dump.RING_CC["B"]))

    def test_the_ruled_out_block_stays_in_the_walk(self):
        # CC 21-28 moved nothing on either layer. It stays in the probe's
        # walk anyway: eight prompts is the cheapest way to notice a
        # firmware that moved things, and dropping it would make the next
        # run silently narrower than the one that produced the answer.
        blocks = xtouch_dump.RING_CC_CANDIDATES
        self.assertEqual(blocks[:2],
                         (xtouch_dump.RING_CC["A"], xtouch_dump.RING_CC["B"]))
        self.assertEqual(len(blocks), 3)

    def test_the_faders_are_not_in_any_ring_block(self):
        # The editor's "ring value CC 9-16" ran straight through both
        # faders, which is how it was caught.
        faders = set(xtouch_dump.FADER_CC.values())
        for block in xtouch_dump.RING_CC_CANDIDATES:
            self.assertFalse(faders & set(block))

    def test_the_modes_accept_the_numbers_that_mean_something(self):
        import xtouch_leds
        allowed = {mode: set(entry[1])
                   for mode, entry in xtouch_leds.TAKES_NUMBER.items()}
        self.assertEqual(allowed["ring"], set(xtouch_dump.RING_CC["A"]) |
                         set(xtouch_dump.RING_CC["B"]))
        self.assertEqual(allowed["states"], set(xtouch_dump.LED_NOTE["A"]) |
                         set(xtouch_dump.LED_NOTE["B"]))

    def test_the_fader_ccs_in_the_gap_are_not_accepted_as_rings(self):
        # 1-8 and 11-18 with the faders sitting in the hole between them. A
        # range check would wave 9 and 10 through, and moving a fader looks
        # exactly like nothing happening -- which is how a whole hardware
        # session got spent on an LED protocol that was working fine.
        import xtouch_leds
        allowed = set(xtouch_leds.TAKES_NUMBER["ring"][1])
        self.assertFalse(set(xtouch_dump.FADER_CC.values()) & allowed)


class TestOutputMessages(unittest.TestCase):
    """What the probe actually puts on the wire, with mido stubbed out."""

    class Recorder:
        def __init__(self):
            self.sent = []

        def send(self, message):
            self.sent.append(message)

    def run_with_stub(self, function, *args):
        """Call an xtouch_leds function with a fake mido and no prompts."""
        import xtouch_leds

        class Message(types.SimpleNamespace):
            def __init__(self, type, **fields):
                super().__init__(type=type, **fields)

        stub = types.SimpleNamespace(Message=Message)
        out = self.Recorder()
        with mock.patch.dict("sys.modules", {"mido": stub}), \
                mock.patch.object(xtouch_leds, "wait", lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()):
            function(out, *args)
        return out.sent

    def test_everything_goes_to_the_mapped_channel(self):
        import xtouch_leds
        sent = self.run_with_stub(xtouch_leds.test_rings)
        self.assertTrue(sent)
        for message in sent:
            self.assertEqual(message.channel, xtouch_dump.CHANNEL)

    def test_clear_leaves_the_mode_switch_alone(self):
        import xtouch_leds
        sent = self.run_with_stub(xtouch_leds.clear)
        # CC 127 is Standard/MC mode, not an LED. Clearing it would
        # reconfigure the device -- and in MC mode the encoders go relative,
        # which changes the whole driver design.
        self.assertFalse([m for m in sent
                          if m.type == "control_change" and m.control == 127])
        self.assertTrue([m for m in sent if m.type == "note_on"])

    def test_clear_puts_out_every_button_on_both_layers(self):
        import xtouch_leds
        sent = self.run_with_stub(xtouch_leds.clear)
        off = {m.note for m in sent if m.type == "note_on" and m.velocity == 0}
        for layer in ("A", "B"):
            self.assertTrue(set(xtouch_dump.LED_NOTE[layer]) <= off, layer)

    def test_the_rings_probe_walks_every_candidate_block(self):
        # One CC at a time, each set to a value that is visibly not the
        # resting state. Setting a ring to 1 is what made one run
        # unreadable: minimum and resting look identical, so the layer A
        # block read as dead when it was simply at its floor.
        import xtouch_leds
        sent = self.run_with_stub(xtouch_leds.test_rings)
        lit = {m.control for m in sent
               if m.type == "control_change" and m.value > 1}
        for block in xtouch_dump.RING_CC_CANDIDATES:
            self.assertTrue(set(block) <= lit)

    def test_the_scan_walk_covers_both_layers_of_buttons(self):
        import xtouch_leds
        sent = self.run_with_stub(xtouch_leds.test_scan)
        lit = {m.note for m in sent
               if m.type == "note_on" and m.velocity > 0}
        for layer in ("A", "B"):
            self.assertTrue(set(xtouch_dump.LED_NOTE[layer]) <= lit, layer)


class TestProbeImports(unittest.TestCase):
    def test_importable_without_mido(self):
        # mido is imported inside the functions that need it, so the logic
        # above can be tested on a machine with nothing installed -- the same
        # reason controller.py defers its dmx import.
        path = helper.os.path.join(helper.ROOT, "xtouch_dump.py")
        with open(path) as handle:
            source = handle.read()
        header = source.split("def find_port")[0]
        self.assertNotIn("\nimport mido", header)


if __name__ == "__main__":
    unittest.main()
