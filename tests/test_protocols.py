"""OS2L framing, tempo maths, colours, and the UDP wire formats.

The framing tests matter most: the OS2L spec defines the messages but not
how they are delimited on the stream, so the parser must not assume.
"""

import unittest

import helper

import colours
import monitor
import os2l
import simlink
import tempo


class TestOS2LFraming(unittest.TestCase):
    def parse(self, *chunks):
        stream = os2l._Stream()
        out = []
        for chunk in chunks:
            out.extend(stream.feed(chunk))
        return out

    def test_newline_delimited(self):
        got = self.parse('{"evt":"beat","pos":1}\n{"evt":"beat","pos":2}\n')
        self.assertEqual([m["pos"] for m in got], [1, 2])

    def test_bare_concatenation(self):
        got = self.parse('{"evt":"beat","pos":1}{"evt":"beat","pos":2}')
        self.assertEqual([m["pos"] for m in got], [1, 2])

    def test_whitespace_separated(self):
        got = self.parse('{"evt":"beat","pos":1}   {"evt":"beat","pos":2}')
        self.assertEqual([m["pos"] for m in got], [1, 2])

    def test_object_split_across_reads(self):
        whole = '{"evt":"beat","pos":42,"bpm":128.0}'
        got = self.parse(whole[:11], whole[11:])
        self.assertEqual(got[0]["pos"], 42)

    def test_object_split_byte_by_byte(self):
        whole = '{"evt":"beat","pos":7,"strength":0.5}'
        got = self.parse(*list(whole))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["pos"], 7)

    def test_partial_object_yields_nothing_yet(self):
        stream = os2l._Stream()
        self.assertEqual(stream.feed('{"evt":"be'), [])


class TestBeat(unittest.TestCase):
    def make(self, pos):
        return os2l.Beat(pos=pos, bpm=120.0, strength=0.5, change=False, at=0.0)

    def test_bar_and_phrase_detection(self):
        self.assertTrue(self.make(0).is_bar)
        self.assertTrue(self.make(0).is_phrase)
        self.assertTrue(self.make(4).is_bar)
        self.assertFalse(self.make(4).is_phrase)
        self.assertTrue(self.make(16).is_phrase)

    def test_negative_positions_keep_bar_phase(self):
        # Confirmed against real VirtualDJ logs, which send negative pos
        # before the beat-grid origin.
        self.assertTrue(self.make(-4).is_bar)
        self.assertTrue(self.make(-16).is_phrase)
        self.assertEqual(self.make(-23).in_bar, 1)
        self.assertFalse(self.make(-23).is_bar)

    def test_audible_distinguishes_silence_from_stopped(self):
        # A silent intro still sends beats, with strength 0. A pause sends
        # nothing at all. Two different silences.
        loud = os2l.Beat(pos=0, bpm=120.0, strength=0.9, change=False, at=0.0)
        quiet = os2l.Beat(pos=0, bpm=120.0, strength=0.0, change=False, at=0.0)
        unknown = os2l.Beat(pos=0, bpm=120.0, strength=None, change=False, at=0.0)
        self.assertTrue(loud.audible)
        self.assertFalse(quiet.audible)
        self.assertTrue(unknown.audible)


class TestOS2LBadMessages(unittest.TestCase):
    """A bad message costs that message, never the listener thread.

    _dispatch runs on the listener thread. An exception there unwinds the
    accept loop and the beats stop with no error and no way back short of a
    restart, so every one of these must be survivable.
    """

    def setUp(self):
        self.status = []
        self.clock = os2l.BeatClock(advertise=False,
                                    on_status=self.status.append)
        self.clock.bpm = 128.0

    def feed(self, *messages):
        for msg in messages:
            self.clock._dispatch(msg)

    def good(self, pos=1):
        return {"evt": "beat", "pos": pos, "bpm": 128.0, "strength": 0.8}

    def test_unparseable_strength_drops_one_beat(self):
        # The concrete case from REVIEW item 11: a future VirtualDJ build
        # sending a word where a number was.
        self.feed(self.good(1), {"evt": "beat", "pos": 2, "strength": "loud"},
                  self.good(3))
        self.assertEqual([b.pos for b in self.clock.poll()], [1, 3])
        self.assertEqual(self.clock.bad_messages, 1)

    def test_unparseable_bpm_leaves_the_tempo_alone(self):
        self.feed(self.good(1))
        self.feed({"evt": "beat", "pos": 2, "bpm": "fast"})
        self.assertEqual(self.clock.bpm, 128.0)
        self.assertEqual(self.clock.total_beats, 1)

    def test_missing_and_nonsense_fields_survive(self):
        for msg in ({"evt": "beat"},                       # no pos
                    {"evt": "beat", "pos": None},
                    {"evt": "beat", "pos": "one"},
                    {"evt": "beat", "pos": [1, 2]}):
            self.feed(msg)
        self.assertEqual(self.clock.poll(), [])
        self.assertEqual(self.clock.bad_messages, 4)

    def test_a_message_that_is_not_an_object_survives(self):
        # _Stream decodes any JSON value, not only objects, so a bare number
        # or list reaches _dispatch and would hit .get on a non-dict.
        self.feed(42, [1, 2, 3], "beat", None)
        self.assertEqual(self.clock.bad_messages, 4)
        self.feed(self.good(9))
        self.assertEqual([b.pos for b in self.clock.poll()], [9])

    def test_each_distinct_fault_is_reported_once(self):
        # Two per second, so a line per bad beat would bury the terminal.
        for pos in range(5):
            self.feed({"evt": "beat", "pos": pos, "strength": "loud"})
        self.assertEqual(len(self.status), 1)
        self.assertEqual(self.clock.bad_messages, 5)

        # ...but a different fault is not hidden by the first.
        self.feed({"evt": "beat", "pos": "one"})
        self.assertEqual(len(self.status), 2)

    def test_non_beat_messages_still_pass_through(self):
        self.feed({"evt": "btn", "name": "pad1", "state": True})
        self.assertEqual(len(self.clock.poll_messages()), 1)
        self.assertEqual(self.clock.bad_messages, 0)


class TestTapTempo(unittest.TestCase):
    def test_two_taps_give_a_tempo(self):
        tapper = tempo.TapTempo()
        self.assertIsNone(tapper.tap(now=0.0))
        self.assertAlmostEqual(tapper.tap(now=0.5), 120.0, places=1)

    def test_median_resists_one_clumsy_tap(self):
        tapper = tempo.TapTempo()
        for moment in (0.0, 0.5, 1.0, 1.5, 2.3, 2.8):   # one long gap
            tapper.tap(now=moment)
        self.assertAlmostEqual(tapper.bpm, 120.0, delta=2.0)

    def test_a_long_gap_starts_a_new_measurement(self):
        tapper = tempo.TapTempo()
        tapper.tap(now=0.0)
        tapper.tap(now=0.5)
        tapper.tap(now=99.0)                # way past TAP_RESET_S
        self.assertEqual(tapper.count, 1)

    def test_absurd_tempos_are_rejected(self):
        tapper = tempo.TapTempo()
        tapper.tap(now=0.0)
        tapper.tap(now=0.01)                # 6000 bpm
        self.assertIsNone(tapper.bpm)


class TestFaderTempo(unittest.TestCase):
    def test_fader_covers_60_to_180(self):
        self.assertEqual(tempo.bpm_from_fader(0), 60)
        self.assertEqual(tempo.bpm_from_fader(127), 180)

    def test_fader_is_clamped(self):
        self.assertEqual(tempo.bpm_from_fader(-5), 60)
        self.assertEqual(tempo.bpm_from_fader(999), 180)

    def test_round_trip(self):
        for bpm in (60, 90, 120, 150, 180):
            self.assertAlmostEqual(
                tempo.bpm_from_fader(tempo.fader_from_bpm(bpm)), bpm, delta=1)


class TestInternalClock(unittest.TestCase):
    def test_silent_until_given_a_tempo(self):
        # A rig that invents beats because VirtualDJ was slow to connect
        # would be worse than one that holds.
        clock = tempo.InternalClock()
        self.assertFalse(clock.armed)
        self.assertEqual(clock.poll(now=100.0), [])

    def test_emits_at_the_set_rate(self):
        clock = tempo.InternalClock()
        clock.set_bpm(120, now=0.0)
        self.assertEqual([b.pos for b in clock.poll(now=0.0)], [0])
        self.assertEqual(clock.poll(now=0.4), [])
        self.assertEqual([b.pos for b in clock.poll(now=0.5)], [1])

    def test_tap_sets_phase_to_zero(self):
        # The tap IS the downbeat, so it starts a phrase.
        clock = tempo.InternalClock()
        clock.tap(now=0.0)
        clock.tap(now=0.5)
        first = clock.poll(now=0.5)[0]
        self.assertEqual(first.pos, 0)
        self.assertTrue(first.is_phrase)

    def test_fader_change_keeps_phase_running(self):
        # Sweeping a fader to hunt for a tempo should change the speed, not
        # stutter back to a downbeat on every step.
        clock = tempo.InternalClock()
        clock.set_bpm(120, now=0.0)
        clock.poll(now=0.0)
        clock.poll(now=0.5)
        clock.set_bpm(140, now=0.6)
        self.assertGreater(clock._pos, 0)

    def test_long_stall_does_not_produce_a_burst(self):
        # Laptop sleep or a blocking reload must not race a chaser through
        # several steps at once on the next poll.
        clock = tempo.InternalClock()
        clock.set_bpm(120, now=0.0)
        clock.poll(now=0.0)
        self.assertLessEqual(len(clock.poll(now=60.0)), 2)


class TestColours(unittest.TestCase):
    def test_names_normalise(self):
        for token in ("warm_white", "warm white", "Warm White", "WARM-WHITE"):
            self.assertEqual(colours.resolve(token), "warm_white")

    def test_raw_indices_still_work(self):
        self.assertEqual(colours.palette(colours.resolve("53")), 53)

    def test_unknown_colour_lists_the_known_ones(self):
        with self.assertRaises(ValueError) as caught:
            colours.resolve("puce")
        self.assertIn("magenta", str(caught.exception))

    def test_find_returns_none_for_non_colours(self):
        # Profile value names like 'slow' and 'on' are not colours, and
        # that is normal -- dmxmon uses this to decide whether to draw a
        # swatch, so it must not raise.
        self.assertIsNone(colours.find("slow"))
        self.assertIsNone(colours.find("off"))
        self.assertEqual(colours.find("red"), "red")

    def test_every_named_colour_has_a_valid_palette_index(self):
        for name in colours.ORDER:
            index = colours.palette(name)
            self.assertTrue(0 <= index <= 127, name)

    def test_palette_table_is_complete(self):
        self.assertEqual(len(colours.PALETTE_HEX), 128)

    def test_idle_scale_is_visibly_dimmer(self):
        full = colours.rgb("red")
        idle = colours.rgb("red", colours.IDLE_SCALE)
        self.assertLess(max(idle), max(full) / 4)


class TestSurfaceConstants(unittest.TestCase):
    """The real surface and the simulator must describe the same device.

    controller.py reaches every one of these through whichever module it
    happens to hold, so a difference between them is a bug that only shows up
    in one of the two setups -- the hardest kind to notice. They share one
    source now; this checks the re-exports did not miss anything.
    """

    SHARED = ("GRID", "TRACK_BUTTONS", "SCENE_BUTTONS", "SHIFT", "FADER_CC",
              "SOLID_10", "SOLID_25", "SOLID_50", "SOLID_100",
              "PULSE_4", "BLINK_4", "BLINK_2", "OFF", "IDLE", "FEEDBACK")

    def test_the_simulator_matches_the_shared_table(self):
        import surface_constants
        import virtualapc
        for name in self.SHARED:
            self.assertEqual(getattr(virtualapc, name),
                             getattr(surface_constants, name), name)

    def test_the_real_surface_matches_the_shared_table(self):
        # apc.py needs mido. Where it is installed -- a machine that can
        # actually drive the hardware -- check it too; elsewhere the
        # simulator check above still covers the re-export.
        try:
            import apc
        except ImportError:
            self.skipTest("mido not installed")
        import surface_constants
        for name in self.SHARED:
            self.assertEqual(getattr(apc, name),
                             getattr(surface_constants, name), name)

    def test_idle_is_dimmer_than_every_active_style(self):
        # The point of the brightness scheme: an idle pad must never be as
        # bright as an active one, whichever feedback style is selected.
        import surface_constants as sc
        self.assertLess(sc.IDLE, sc.SOLID_100)
        self.assertNotIn(sc.IDLE, sc.FEEDBACK.values())


class TestWireFormats(unittest.TestCase):
    def test_monitor_addresses(self):
        self.assertEqual(monitor.parse_addr(None), monitor.DEFAULT_ADDR)
        self.assertEqual(monitor.parse_addr(""), monitor.DEFAULT_ADDR)
        self.assertEqual(monitor.parse_addr("9001"), ("127.0.0.1", 9001))
        self.assertEqual(monitor.parse_addr("host:9002"), ("host", 9002))

    def test_monitor_rejects_nonsense(self):
        for bad in ("abc", "99999", "host:xyz"):
            with self.assertRaises(ValueError):
                monitor.parse_addr(bad)

    def test_led_encoding_round_trips(self):
        updates = [(0, 5, 6), (63, 127, 15), (100, 1, 0)]
        self.assertEqual(simlink.decode_leds(simlink.encode_leds(updates)),
                         updates)

    def test_led_encoding_masks_out_of_range(self):
        # A malformed update must not corrupt the rest of the datagram.
        encoded = simlink.encode_leds([(0, 200, 20)])
        note, velocity, channel = simlink.decode_leds(encoded)[0]
        self.assertLessEqual(velocity, 127)
        self.assertLessEqual(channel, 15)


if __name__ == "__main__":
    unittest.main()
