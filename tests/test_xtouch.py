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
    def note(number, channel=0):
        return types.SimpleNamespace(type="note_on", note=number,
                                     velocity=127, channel=channel)

    @staticmethod
    def control(number, channel=10):
        return types.SimpleNamespace(type="control_change", control=number,
                                     value=64, channel=channel)

    def walk(self, batches):
        port = self.StubPort(batches)
        with mock.patch("builtins.input", lambda *a: ""), \
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
