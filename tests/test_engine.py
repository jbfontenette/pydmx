"""Merge policy, fader arithmetic, and chaser clocking.

These lock down the decisions that are easy to "simplify" into bugs: HTP
versus LTP, why the master must not touch snap channels, and why chaser
position is derived rather than counted.
"""

import unittest

import helper

import engine as engine_mod
import os2l


def make_engine():
    show = helper.load_show()
    return show, engine_mod.Engine(show.patch, show.scenes, show.chasers)


def beat(pos, bpm=120.0, strength=0.8, change=False):
    return os2l.Beat(pos=pos, bpm=bpm, strength=strength, change=change, at=0.0)


class TestMergePolicy(unittest.TestCase):
    def setUp(self):
        self.show, self.eng = make_engine()
        self.dimmer = self.show.patch.resolve("par1", "dimmer")
        self.colour = self.show.patch.resolve("par1", "colour")

    def test_fade_channels_merge_htp(self):
        self.eng.activate("half")     # par1 dimmer 128
        self.eng.activate("warm")     # par1 dimmer 255
        self.assertEqual(self.eng.output()[self.dimmer], 255)

    def test_htp_is_order_independent(self):
        self.eng.activate("warm")
        self.eng.activate("half")
        self.assertEqual(self.eng.output()[self.dimmer], 255)

    def test_snap_channels_merge_ltp_not_htp(self):
        # HTP on a selector is meaningless: merging colour 10 and colour 42
        # must not give 42 "because it is larger", it gives 42 because it
        # was activated later. Reversing the order proves which rule ran.
        self.eng.activate("colour_b")   # 42
        self.eng.activate("colour_a")   # 10
        self.assertEqual(self.eng.output()[self.colour], 10)

    def test_reactivating_moves_a_source_to_the_front_of_ltp(self):
        self.eng.activate("colour_a")
        self.eng.activate("colour_b")
        self.assertEqual(self.eng.output()[self.colour], 42)
        self.eng.activate("colour_a")   # re-press
        self.assertEqual(self.eng.output()[self.colour], 10)

    def test_a_zero_scene_cannot_turn_anything_off(self):
        # Consequence of HTP that surprises people: a scene of zeros loses
        # every comparison, so it is indistinguishable from being inactive.
        # Going dark is deactivating sources, not activating a zero scene.
        self.eng.activate("warm")
        self.eng.activate("zeros")
        self.assertEqual(self.eng.output()[self.dimmer], 255)


class TestMaster(unittest.TestCase):
    def setUp(self):
        self.show, self.eng = make_engine()
        self.dimmer = self.show.patch.resolve("par1", "dimmer")
        self.colour = self.show.patch.resolve("par1", "colour")

    def test_master_scales_fade_channels(self):
        self.eng.activate("warm")
        self.eng.set_master(128)
        self.assertEqual(self.eng.output()[self.dimmer], 128)

    def test_master_never_touches_snap_channels(self):
        # Half of colour index 42 is index 21 -- a DIFFERENT COLOUR, not a
        # dimmer version of the same one. This is the whole reason the
        # fade/snap distinction exists.
        self.eng.activate("colour_b")
        self.eng.set_master(128)
        self.assertEqual(self.eng.output()[self.colour], 42)

    def test_master_at_zero_blacks_out_fade_channels(self):
        self.eng.activate("warm")
        self.eng.set_master(0)
        self.assertEqual(self.eng.output()[self.dimmer], 0)


class TestFaders(unittest.TestCase):
    def setUp(self):
        self.show, self.eng = make_engine()
        self.channels = self.show.faders[1].channels    # par*.dimmer

    def test_level_fader_adds_htp(self):
        self.eng.set_level(1, self.channels, 180)
        self.assertEqual(self.eng.output()[1], 180)

    def test_level_fader_cannot_pull_a_scene_down(self):
        # It merges HTP like any other source, so it raises but never lowers.
        # Dimming a scene is what the master and scale faders are for.
        self.eng.activate("warm")                       # 255
        self.eng.set_level(1, self.channels, 100)
        self.assertEqual(self.eng.output()[1], 255)

    def test_scale_fader_multiplies(self):
        self.eng.activate("warm")
        self.eng.set_scale(2, self.channels, 128)
        self.assertEqual(self.eng.output()[1], 128)

    def test_scale_applies_after_level(self):
        self.eng.set_level(1, self.channels, 200)
        self.eng.set_scale(2, self.channels, 128)
        self.assertEqual(self.eng.output()[1], 100)

    def test_scale_then_master_compose(self):
        self.eng.activate("warm")                       # 255
        self.eng.set_scale(2, self.channels, 128)       # -> 128
        self.eng.set_master(128)                        # -> 64
        self.assertEqual(self.eng.output()[1], 64)

    def test_scale_only_touches_its_own_channels(self):
        self.eng.activate("warm")
        self.eng.activate("red_all")
        bar = self.show.patch.resolve("bar1", "colour")
        self.eng.set_scale(2, self.channels, 0)
        out = self.eng.output()
        self.assertEqual(out[1], 0)
        self.assertEqual(out[bar], 15)                  # untouched

    def test_clear_does_not_reset_faders(self):
        # Fader state mirrors a physical position. Zeroing it would leave
        # the software disagreeing with the hardware until you touched it.
        self.eng.set_level(1, self.channels, 200)
        self.eng.clear()
        self.assertEqual(self.eng.output()[1], 200)


class TestChaserClocking(unittest.TestCase):
    def setUp(self):
        self.show, self.eng = make_engine()

    def test_timed_chaser_advances_on_tick(self):
        self.eng.start_chaser("timed", now=0.0)
        self.assertEqual(self.eng.chaser_position("timed"), (1, 2))
        self.eng.tick(now=0.4)
        self.assertEqual(self.eng.chaser_position("timed"), (1, 2))
        self.eng.tick(now=0.6)
        self.assertEqual(self.eng.chaser_position("timed"), (2, 2))

    def test_manual_chaser_never_advances_on_its_own(self):
        self.eng.start_chaser("manual", now=0.0)
        self.eng.tick(now=10_000.0)
        self.assertEqual(self.eng.chaser_position("manual"), (1, 2))

    def test_step_chaser_advances_and_wraps(self):
        self.eng.start_chaser("manual", now=0.0)
        self.eng.step_chaser("manual")
        self.assertEqual(self.eng.chaser_position("manual"), (2, 2))
        self.eng.step_chaser("manual")
        self.assertEqual(self.eng.chaser_position("manual"), (1, 2))

    def test_step_all_advances_every_running_chaser(self):
        self.eng.start_chaser("manual", now=0.0)
        self.eng.start_chaser("timed", now=0.0)
        self.eng.step_chaser(None)
        self.assertEqual(self.eng.chaser_position("manual"), (2, 2))
        self.assertEqual(self.eng.chaser_position("timed"), (2, 2))

    def test_beat_synced_chaser_ignores_its_timers(self):
        # It must HOLD when the music stops rather than free-running, which
        # is what keeps it in phase when the music returns.
        self.eng.start_chaser("beatsync", now=0.0)
        self.eng.tick(now=10_000.0)
        self.assertEqual(self.eng.chaser_position("beatsync"), (1, 3))

    def test_beat_position_is_derived_not_counted(self):
        # beatsync is 1 + 1 + 2 = a 4-beat cycle.
        self.eng.start_chaser("beatsync", now=0.0)
        expected = [(0, 1), (1, 2), (2, 3), (3, 3),
                    (4, 1), (5, 2), (6, 3), (7, 3)]
        for pos, step in expected:
            self.eng.on_beat(beat(pos))
            self.assertEqual(self.eng.chaser_position("beatsync")[0], step,
                             f"pos {pos}")

    def test_phase_survives_a_pause(self):
        # VirtualDJ stops sending beats while paused and resumes wherever
        # the track is. A counter would drift; deriving cannot.
        self.eng.start_chaser("beatsync", now=0.0)
        self.eng.on_beat(beat(1))
        self.assertEqual(self.eng.chaser_position("beatsync")[0], 2)
        self.eng.on_beat(beat(10))          # long silence, then resume
        self.assertEqual(self.eng.chaser_position("beatsync")[0], 3)

    def test_phase_survives_a_deck_change(self):
        # pos is per-deck and resets; the chaser simply re-phases.
        self.eng.start_chaser("beatsync", now=0.0)
        self.eng.on_beat(beat(87, bpm=80))
        self.eng.on_beat(beat(4, bpm=132.7, change=True))
        self.assertEqual(self.eng.chaser_position("beatsync")[0], 1)

    def test_negative_positions_keep_the_right_phase(self):
        # pos runs negative before the beat-grid origin. Python's modulo is
        # non-negative, which is why this works -- never "fix" it with abs().
        self.eng.start_chaser("beatsync", now=0.0)
        for pos, step in [(-4, 1), (-3, 2), (-2, 3), (-1, 3), (0, 1)]:
            self.eng.on_beat(beat(pos))
            self.assertEqual(self.eng.chaser_position("beatsync")[0], step,
                             f"pos {pos}")

    def test_non_beat_synced_chasers_ignore_beats(self):
        self.eng.start_chaser("timed", now=0.0)
        for pos in range(8):
            self.eng.on_beat(beat(pos))
        self.assertEqual(self.eng.chaser_position("timed"), (1, 2))


class TestSourceOrdering(unittest.TestCase):
    """Scenes and chasers share one ordered list, so LTP between them works."""

    def setUp(self):
        self.show, self.eng = make_engine()
        self.colour = self.show.patch.resolve("par1", "colour")

    def test_chaser_started_after_a_scene_wins_the_snap_channel(self):
        self.eng.activate("colour_b")           # 42
        self.eng.start_chaser("beatsync")       # step 1 is colour_a = 10
        self.assertEqual(self.eng.output()[self.colour], 10)

    def test_scene_activated_after_a_chaser_wins(self):
        self.eng.start_chaser("beatsync")
        self.eng.activate("colour_b")
        self.assertEqual(self.eng.output()[self.colour], 42)

    def test_is_active_covers_both_kinds(self):
        self.eng.activate("warm")
        self.eng.start_chaser("timed")
        self.assertTrue(self.eng.is_active("warm"))
        self.assertTrue(self.eng.is_active("timed"))

    def test_clear_stops_scenes_and_chasers(self):
        self.eng.activate("warm")
        self.eng.start_chaser("timed")
        self.eng.clear()
        self.assertEqual(self.eng.active, [])
        self.assertEqual(self.eng.running, {})


if __name__ == "__main__":
    unittest.main()
