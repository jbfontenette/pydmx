"""Show file parsing: the patch maths, named values, globs, and error policy."""

import shutil
import unittest

import helper

import showfile


class TestPatchArithmetic(unittest.TestCase):
    """channel = address + offset - 1, with 1-based offsets."""

    def setUp(self):
        self.show = helper.load_show()

    def test_offsets_are_one_based(self):
        # par1 is at address 1, so its dimmer (offset 1) IS channel 1.
        # An off-by-one here shifts every fixture in the rig.
        self.assertEqual(self.show.patch.resolve("par1", "dimmer"), 1)
        self.assertEqual(self.show.patch.resolve("par1", "colour"), 5)
        self.assertEqual(self.show.patch.resolve("par2", "dimmer"), 11)
        self.assertEqual(self.show.patch.resolve("par2", "colour"), 15)

    def test_footprint_is_highest_offset(self):
        self.assertEqual(self.show.profiles["par"].footprint, 6)
        self.assertEqual(self.show.profiles["bar"].footprint, 3)

    def test_unknown_feature_names_the_alternatives(self):
        with self.assertRaises(KeyError) as caught:
            self.show.patch.resolve("par1", "nonexistent")
        self.assertIn("dimmer", str(caught.exception))

    def test_conflicts_detects_overlap(self):
        path = helper.temp_show(
            fixtures="fixture,profile,address\na,par,1\nb,par,4\n",
            scenes="scene,fixture,feature,value\nx,a,dimmer,255\n",
            chasers="chaser,step,scene,duration_ms,beats\n",
            mapping="pad,type,target,mode,colour\nr0c0,scene,x,toggle,red\n")
        try:
            show = showfile.Show(path)
            show.load()
            problems = show.patch.conflicts()
            self.assertTrue(problems)
            self.assertIn("overlaps", problems[0])
        finally:
            shutil.rmtree(path)

    def test_conflicts_detects_out_of_universe(self):
        path = helper.temp_show(
            fixtures="fixture,profile,address\na,par,510\n",
            scenes="scene,fixture,feature,value\nx,a,dimmer,255\n",
            chasers="chaser,step,scene,duration_ms,beats\n",
            mapping="pad,type,target,mode,colour\nr0c0,scene,x,toggle,red\n")
        try:
            show = showfile.Show(path)
            show.load()
            self.assertTrue(any("outside" in p for p in show.patch.conflicts()))
        finally:
            shutil.rmtree(path)


class TestNamedValues(unittest.TestCase):
    """Plain-text names for snap values, resolved per profile."""

    def setUp(self):
        self.show = helper.load_show()

    def test_same_word_different_number_per_profile(self):
        # This is why scene values cannot be parsed before the fixture is
        # known: 'red' is 10 on par and 15 on bar.
        par = self.show.profiles["par"].features["colour"]
        bar = self.show.profiles["bar"].features["colour"]
        self.assertEqual(par.resolve("red"), 10)
        self.assertEqual(bar.resolve("red"), 15)

    def test_range_resolves_to_midpoint(self):
        # Manuals document bands; the edges are where an off-by-one lands
        # you in the next colour, so a name means the middle of its band.
        strobe = self.show.profiles["par"].features["strobe"]
        self.assertEqual(strobe.values["slow"], (10, 100))
        self.assertEqual(strobe.resolve("slow"), 55)

    def test_label_matches_anywhere_inside_a_band(self):
        strobe = self.show.profiles["par"].features["strobe"]
        for value in (10, 55, 100):
            self.assertEqual(strobe.label(value), "slow")
        self.assertEqual(strobe.label(0), "off")
        self.assertIsNone(strobe.label(200))

    def test_numbers_still_work(self):
        colour = self.show.profiles["par"].features["colour"]
        self.assertEqual(colour.resolve("42"), 42)

    def test_unknown_name_lists_the_known_ones(self):
        colour = self.show.profiles["par"].features["colour"]
        with self.assertRaises(ValueError) as caught:
            colour.resolve("puce")
        self.assertIn("red", str(caught.exception))

    def test_glob_resolves_per_fixture(self):
        # red_all uses '*,colour,red' across both profiles.
        levels = self.show.scenes["red_all"].levels
        self.assertEqual(levels[self.show.patch.resolve("par1", "colour")], 10)
        self.assertEqual(levels[self.show.patch.resolve("bar1", "colour")], 15)


class TestScenes(unittest.TestCase):
    def setUp(self):
        self.show = helper.load_show()

    def test_scenes_are_sparse(self):
        # 'half' touches one channel; everything else must be absent, not 0,
        # or scene stacking would not work.
        self.assertEqual(len(self.show.scenes["half"].levels), 1)

    def test_glob_expands_to_matching_fixtures(self):
        warm = self.show.scenes["warm"].levels
        self.assertEqual(warm[1], 255)     # par1 dimmer
        self.assertEqual(warm[11], 255)    # par2 dimmer
        self.assertNotIn(21, warm)         # bar1 not matched by 'par*'

    def test_glob_skips_fixtures_lacking_the_feature(self):
        # 'red_all' globs '*' for colour; bar HAS colour so all three appear.
        self.assertEqual(len(self.show.scenes["red_all"].levels), 3)

    def test_pattern_matching_nothing_is_fatal(self):
        path = helper.temp_show(
            scenes="scene,fixture,feature,value\nx,nosuch*,dimmer,255\n")
        try:
            with self.assertRaises(ValueError):
                showfile.Show(path).load()
        finally:
            shutil.rmtree(path)


class TestChaserLoading(unittest.TestCase):
    def setUp(self):
        self.show = helper.load_show()

    def test_steps_sort_by_step_column_not_file_order(self):
        path = helper.temp_show(
            chasers="chaser,step,scene,duration_ms,beats\n"
                    "c,30,warm,100,\nc,10,half,100,\nc,20,warm,100,\n")
        try:
            show = showfile.Show(path)
            show.load()
            self.assertEqual([s.scene for s in show.chasers["c"].steps],
                             ["half", "warm", "warm"])
        finally:
            shutil.rmtree(path)

    def test_beat_synced_requires_every_step(self):
        self.assertTrue(self.show.chasers["beatsync"].beat_synced)
        self.assertFalse(self.show.chasers["mixed"].beat_synced)
        self.assertFalse(self.show.chasers["timed"].beat_synced)

    def test_partly_beat_synced_warns(self):
        self.assertTrue(any("mixed" in w and "beat" in w
                            for w in self.show.warnings),
                        f"expected a warning about 'mixed': {self.show.warnings}")

    def test_cycle_beats_sums_the_steps(self):
        self.assertEqual(self.show.chasers["beatsync"].cycle_beats, 4)

    def test_unknown_scene_skips_the_step(self):
        path = helper.temp_show(
            chasers="chaser,step,scene,duration_ms,beats\n"
                    "c,10,warm,100,\nc,20,nosuchscene,100,\n")
        try:
            show = showfile.Show(path)
            show.load()
            self.assertEqual(len(show.chasers["c"].steps), 1)
        finally:
            shutil.rmtree(path)


class TestMapping(unittest.TestCase):
    def setUp(self):
        self.show = helper.load_show()

    def test_pad_notation(self):
        self.assertEqual(showfile.parse_pad("0"), 0)
        self.assertEqual(showfile.parse_pad("r0c0"), 0)
        self.assertEqual(showfile.parse_pad("r1c1"), 9)
        self.assertEqual(showfile.parse_pad("r7c7"), 63)
        self.assertEqual(showfile.parse_pad("t1"), 0x64)
        self.assertEqual(showfile.parse_pad("s1"), 0x70)
        self.assertEqual(showfile.parse_pad("f9"), ("fader", 9))

    def test_row_zero_is_the_bottom_row(self):
        # note = row * 8 + col, so the visual TOP row is 56-63. Getting this
        # backwards mirrors the whole layout vertically.
        self.assertEqual(showfile.parse_pad("r0c0"), 0)
        self.assertEqual(showfile.parse_pad("r7c0"), 56)

    def test_shift_is_a_separate_layer(self):
        # r0c2 is bound on the SHIFT layer only, so unshifted it is unbound.
        self.assertIsNone(self.show.binding_for(2, False))
        self.assertIsNotNone(self.show.binding_for(2, True))
        self.assertEqual(self.show.binding_for(2, True).mode, "solo")

    def test_unshifted_shows_through_where_shift_has_nothing(self):
        self.assertIsNotNone(self.show.binding_for(0, True))
        self.assertEqual(self.show.binding_for(0, True).target, "warm")

    def test_unknown_scene_warns_but_does_not_stop_startup(self):
        # Drift between two files you edit separately should cost one pad,
        # not the whole rig.
        path = helper.temp_show(
            mapping="pad,type,target,mode,colour\n"
                    "r0c0,scene,warm,toggle,red\n"
                    "r0c1,scene,ghost,toggle,red\n")
        try:
            show = showfile.Show(path)
            show.load()
            self.assertEqual(len(show.bindings), 1)
            self.assertTrue(any("ghost" in w for w in show.warnings))
        finally:
            shutil.rmtree(path)

    def test_structural_error_is_fatal(self):
        # A pad spec that cannot be parsed means the file is not understood,
        # and guessing would be worse than stopping.
        path = helper.temp_show(
            mapping="pad,type,target,mode,colour\nzzz9,scene,warm,toggle,red\n")
        try:
            with self.assertRaises(ValueError):
                showfile.Show(path).load()
        finally:
            shutil.rmtree(path)


class TestFaderBindings(unittest.TestCase):
    def setUp(self):
        self.show = helper.load_show()

    def test_level_resolves_glob_to_channels(self):
        self.assertEqual(self.show.faders[1].kind, "level")
        self.assertEqual(self.show.faders[1].channels, (1, 11))

    def test_scale_resolves_the_same_way(self):
        self.assertEqual(self.show.faders[2].kind, "scale")
        self.assertEqual(self.show.faders[2].channels, (1, 11))

    def test_snap_feature_is_refused(self):
        # A level fader would sweep through every colour; scaling one turns
        # colour 31 into colour 15, a different colour rather than a dimmer
        # version of it. Neither has a sensible meaning.
        for kind in ("level", "scale"):
            path = helper.temp_show(
                mapping=f"pad,type,target,mode,colour\nf1,{kind},par*.colour\n")
            try:
                with self.assertRaises(ValueError) as caught:
                    showfile.Show(path).load()
                self.assertIn("SNAP", str(caught.exception))
            finally:
                shutil.rmtree(path)

    def test_target_needs_a_dot(self):
        path = helper.temp_show(
            mapping="pad,type,target,mode,colour\nf1,level,par*\n")
        try:
            with self.assertRaises(ValueError):
                showfile.Show(path).load()
        finally:
            shutil.rmtree(path)

    def test_fader_and_pad_key_spaces_do_not_collide(self):
        # Fader CCs are 48-56 and grid notes are 0-63, so a shared key space
        # would make f1 collide with the pad at note 48.
        self.assertIsInstance(showfile.parse_pad("f1"), tuple)
        self.assertIsInstance(showfile.parse_pad("48"), int)


class TestReload(unittest.TestCase):
    def test_reload_is_atomic_on_failure(self):
        path = helper.temp_show()
        try:
            show = showfile.Show(path)
            show.load()
            before = len(show.scenes)
            with open(f"{path}/scenes.csv", "a") as handle:
                handle.write("broken,par1,dimmer,NOT_A_NUMBER\n")
            ok, message, _ = show.reload()
            self.assertFalse(ok)
            self.assertEqual(len(show.scenes), before)
            self.assertIn("line", message)
        finally:
            shutil.rmtree(path)

    def test_reload_reports_what_changed(self):
        path = helper.temp_show()
        try:
            show = showfile.Show(path)
            show.load()
            with open(f"{path}/scenes.csv", "a") as handle:
                handle.write("brandnew,par1,dimmer,10\n")
            ok, message, (added, removed, changed) = show.reload()
            self.assertTrue(ok)
            self.assertIn("brandnew", added)
        finally:
            shutil.rmtree(path)


if __name__ == "__main__":
    unittest.main()
