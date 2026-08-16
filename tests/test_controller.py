"""Reload reconciliation and the --watch detector.

The bug these guard against is a threading one: --watch used to reload from
its own thread while the main loop iterated the same dicts, which could kill
the main loop and freeze the rig on its last look. The fix splits the job in
two -- watch_files() only detects, the main loop reloads -- so the tests come
in two halves: what a reload does to engine state, and what the detector
promises the main loop.

controller.py imports no hardware package above main(), which is what lets
these run with nothing installed.
"""

import os
import shutil
import threading
import time
import unittest

import helper

import controller
import engine as engine_mod
import showfile


def show_and_engine(directory):
    show = showfile.Show(directory)
    show.load()
    return show, engine_mod.Engine(show.patch, show.scenes, show.chasers)


def rewrite(directory, name, text):
    """Overwrite one CSV and force a distinct mtime.

    Explicit utime rather than trusting the clock: filesystem mtime
    granularity is coarse enough that two writes in the same test can land on
    the same stamp, which would make the detector look broken when it is not.
    """
    path = os.path.join(directory, f"{name}.csv")
    with open(path, "w") as handle:
        handle.write(text)
    stamp = time.time() + 5
    os.utime(path, (stamp, stamp))


class TestApplyReload(unittest.TestCase):
    """What survives a reload, and what a failed one must not touch."""

    def setUp(self):
        self.dir = helper.temp_show()
        self.addCleanup(shutil.rmtree, self.dir)
        self.show, self.eng = show_and_engine(self.dir)

    def test_deleted_scene_drops_from_active(self):
        self.eng.activate("warm")
        self.eng.activate("half")
        rewrite(self.dir, "scenes", "scene,fixture,feature,value\n"
                                    "warm,par*,dimmer,255\n")

        ok, message = controller.apply_reload(self.show, self.eng)

        self.assertTrue(ok)
        self.assertEqual(self.eng.active, [("scene", "warm")])
        self.assertIn("half", message)          # says what it dropped

    def test_survivors_keep_their_ltp_order(self):
        # active is ordered oldest-first and the order IS the LTP rule for
        # snap channels, so dropping a scene from the middle must not
        # reshuffle the rest.
        self.eng.activate("warm")
        self.eng.activate("half")
        self.eng.activate("colour_a")
        rewrite(self.dir, "scenes", "scene,fixture,feature,value\n"
                                    "warm,par*,dimmer,255\n"
                                    "colour_a,par1,colour,10\n")

        controller.apply_reload(self.show, self.eng)

        self.assertEqual(self.eng.active,
                         [("scene", "warm"), ("scene", "colour_a")])

    def test_failed_parse_leaves_the_running_show_untouched(self):
        # The whole point of atomic reload: a typo mid-set costs nothing.
        self.eng.activate("warm")
        scenes, patch, bindings = (self.eng.scenes, self.eng.patch,
                                   self.show.bindings)
        rewrite(self.dir, "scenes", "scene,fixture,feature,value\n"
                                    "broken,par1,dimmer,not_a_number\n")

        ok, message = controller.apply_reload(self.show, self.eng)

        self.assertFalse(ok)
        self.assertTrue(message)
        self.assertIs(self.eng.scenes, scenes)
        self.assertIs(self.eng.patch, patch)
        self.assertIs(self.show.bindings, bindings)
        self.assertEqual(self.eng.active, [("scene", "warm")])

    def test_running_chaser_is_repointed_at_the_new_object(self):
        # A stale Chaser reference keeps playing the old steps, which looks
        # exactly like the reload having failed.
        self.eng.start_chaser("timed")
        old = self.eng.running["timed"].chaser
        rewrite(self.dir, "chasers", "chaser,step,scene,duration_ms,beats\n"
                                     "timed,10,half,250,\n"
                                     "timed,20,warm,250,\n")

        controller.apply_reload(self.show, self.eng)

        state = self.eng.running["timed"]
        self.assertIsNot(state.chaser, old)
        self.assertIs(state.chaser, self.show.chasers["timed"])
        self.assertEqual(state.scene_name, "half")

    def test_shortened_chaser_clamps_the_index(self):
        self.eng.start_chaser("beatsync")
        self.eng.running["beatsync"].index = 2
        rewrite(self.dir, "chasers", "chaser,step,scene,duration_ms,beats\n"
                                     "beatsync,10,warm,,1\n")

        controller.apply_reload(self.show, self.eng)

        self.assertEqual(self.eng.running["beatsync"].index, 0)
        self.assertEqual(self.eng.running["beatsync"].scene_name, "warm")

    def test_deleted_chaser_stops(self):
        self.eng.start_chaser("timed")
        rewrite(self.dir, "chasers", "chaser,step,scene,duration_ms,beats\n"
                                     "manual,10,warm,,\n")

        controller.apply_reload(self.show, self.eng)

        self.assertNotIn("timed", self.eng.running)
        self.assertEqual(self.eng.active, [])


class TestStamps(unittest.TestCase):
    """show.stamps() is the one thing the watcher thread may call."""

    def setUp(self):
        self.dir = helper.temp_show()
        self.addCleanup(shutil.rmtree, self.dir)
        self.show, self.eng = show_and_engine(self.dir)

    def test_stable_without_writes(self):
        self.assertEqual(self.show.stamps(), self.show.stamps())

    def test_changes_when_a_file_is_written(self):
        before = self.show.stamps()
        rewrite(self.dir, "scenes", "scene,fixture,feature,value\n"
                                    "warm,par*,dimmer,200\n")
        self.assertNotEqual(self.show.stamps(), before)

    def test_changed_on_disk_stays_true_after_a_failed_reload(self):
        # Documents the asymmetry the detector exists to work around:
        # changed_on_disk compares against the last SUCCESSFUL reload, so a
        # broken file keeps answering True forever.
        rewrite(self.dir, "scenes", "scene,fixture,feature,value\n"
                                    "broken,par1,dimmer,not_a_number\n")
        ok, _ = controller.apply_reload(self.show, self.eng)
        self.assertFalse(ok)
        self.assertTrue(self.show.changed_on_disk())


class TestWatchDetector(unittest.TestCase):
    """watch_files() raises a flag and nothing else."""

    def setUp(self):
        self.dir = helper.temp_show()
        self.addCleanup(shutil.rmtree, self.dir)
        self.show, self.eng = show_and_engine(self.dir)
        self.stop = threading.Event()
        self.request = threading.Event()

    def start(self):
        thread = threading.Thread(
            target=controller.watch_files, daemon=True,
            args=(self.show, self.stop, self.request, 0.01))
        thread.start()

        def shut_down():
            # Signal before joining. The thread only wakes to check stop, so
            # joining first would just wait out the timeout.
            self.stop.set()
            thread.join(1.0)
            self.assertFalse(thread.is_alive())

        self.addCleanup(shut_down)

    def edit(self):
        rewrite(self.dir, "scenes", "scene,fixture,feature,value\n"
                                    f"warm,par*,dimmer,{time.time_ns() % 255}\n")

    def test_quiet_files_raise_nothing(self):
        self.start()
        self.assertFalse(self.request.wait(0.1))

    def test_a_save_raises_the_flag(self):
        self.start()
        self.edit()
        self.assertTrue(self.request.wait(1.0))

    def test_one_request_per_save_even_when_the_reload_fails(self):
        # The regression this guards: if the detector asked
        # changed_on_disk(), an unparseable file would re-request every
        # interval and the main loop would re-parse a broken show forever.
        self.start()
        rewrite(self.dir, "scenes", "scene,fixture,feature,value\n"
                                    "broken,par1,dimmer,not_a_number\n")
        self.assertTrue(self.request.wait(1.0))

        ok, _ = controller.apply_reload(self.show, self.eng)
        self.assertFalse(ok)
        self.request.clear()
        self.assertFalse(self.request.wait(0.1))

        # ...and the save that fixes it is reported like any other.
        self.edit()
        self.assertTrue(self.request.wait(1.0))

    def test_detector_never_reloads(self):
        # It may look at mtimes; it may not touch the parsed show or the
        # engine. Anything else is the race this whole change removes.
        self.eng.activate("warm")
        scenes, bindings = self.eng.scenes, self.show.bindings
        self.start()
        rewrite(self.dir, "scenes", "scene,fixture,feature,value\n"
                                    "different,par1,dimmer,10\n")
        self.assertTrue(self.request.wait(1.0))

        self.assertIs(self.eng.scenes, scenes)
        self.assertIs(self.show.bindings, bindings)
        self.assertEqual(self.eng.active, [("scene", "warm")])


class TestReloadPadRouting(unittest.TestCase):
    """The pad path still reaches the reload action, and says which pad.

    do_reload() decides whether to flash the surface from whether it was
    given a note, so a pad press must always carry one -- None is reserved
    for the watcher, which has no pad to light.
    """

    def setUp(self):
        import virtualapc
        # Route through the simulator's constant table: it is stdlib-only,
        # where apc.py needs mido. Same note numbers either way.
        self.previous = controller._SURFACE_MODULE
        controller._SURFACE_MODULE = virtualapc
        self.addCleanup(setattr, controller, "_SURFACE_MODULE", self.previous)
        self.show = helper.load_show()
        self.eng = engine_mod.Engine(self.show.patch, self.show.scenes,
                                     self.show.chasers)

    def test_press_calls_the_action_with_the_pad_note(self):
        note = 0x70                      # s1 in the test mapping is 'reload'
        self.assertEqual(self.show.binding_for(note, False).kind, "reload")
        seen = []
        state = {"shift": False, "held": {}, "relayout": False,
                 "master_pending": None, "bpm_pending": None,
                 "flash_until": 0.0, "internal": None}

        controller.handle(("press", note), self.show, self.eng,
                          lambda m: None, state,
                          {"reload": seen.append})

        self.assertEqual(seen, [note])


class TestReloadHasOneCallSite(unittest.TestCase):
    """The invariant the fix rests on, checked the only way source can be.

    apply_reload mutates the show and engine, so it must be reachable from
    exactly one place -- the main loop's do_reload(). If a second call site
    appears, the odds are it is on a thread again.
    """

    def test_apply_reload_is_called_once_in_controller(self):
        with open(os.path.join(helper.ROOT, "controller.py")) as handle:
            source = handle.read()
        calls = source.count("apply_reload(")
        self.assertEqual(calls, 2, "expected one definition and one call site")


if __name__ == "__main__":
    unittest.main()
