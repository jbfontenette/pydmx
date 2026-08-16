"""Shared test setup.

Puts the repo root on sys.path so tests can import the modules without the
project needing to be installed, and points at the deterministic test show
in tests/show/ rather than the user's real rig in show/.
"""

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_SHOW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "show")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def load_show(warn=None):
    """The standard test show. Warnings are collected, not printed."""
    import showfile
    collected = []
    show = showfile.Show(TEST_SHOW)
    show.load()
    if warn is not None:
        warn.extend(show.warnings)
    return show


def temp_show(**overrides):
    """A copy of the test show with some files replaced.

    temp_show(mapping="pad,type,...\\n...") writes that text over
    mapping.csv in a throwaway directory. Returns the directory path; the
    caller is responsible for cleanup via shutil.rmtree.
    """
    path = tempfile.mkdtemp(prefix="dmxtest_")
    for name in ("profiles", "fixtures", "scenes", "chasers", "mapping"):
        target = os.path.join(path, f"{name}.csv")
        if name in overrides:
            with open(target, "w") as handle:
                handle.write(overrides[name])
        else:
            shutil.copy(os.path.join(TEST_SHOW, f"{name}.csv"), target)
    return path
