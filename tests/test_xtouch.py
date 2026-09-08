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
