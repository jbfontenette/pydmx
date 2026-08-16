"""Scene stacking, chasers, and channel merging.

Merge policy comes from each channel's mode, and the two policies exist for
a concrete reason:

  fade channels merge HTP (highest takes precedence). Two scenes both
       lighting a wash produce the brighter of the two -- what you expect.

  snap channels merge LTP (latest takes precedence). HTP is meaningless on a
       colour wheel: merging red (11) and blue (31) would give 31, which is
       not a blend, just the larger number winning by accident. So the most
       recently activated source owns the selector outright.

That is why self.active is an ordered LIST rather than a set -- the order IS
the LTP rule. Entries are ("scene", name) or ("chaser", name), in one list
together, so a chaser started after a scene correctly wins the snap channels
they share. Keeping two separate lists would have left that order undefined.

CHASER CLOCKING

A chaser advances when something calls step(). What that something is stays
deliberately open:

  the timer     a step with duration_ms > 0 advances itself
  a pad         a 'chaser_step' binding advances it on tap
  the music     on_beat() places a fully beat-synced chaser by beat number

The first two ADVANCE by one. The third does not advance at all -- it DERIVES
the step from the track's beat position. That difference is deliberate: a
counter drifts out of phase at every pause, seek and deck change, and you
only notice mid-set. Deriving from pos means those cases need no handling.

A step with duration_ms = 0 holds until something else fires, which is what
makes a fully manual chaser just an ordinary chaser with no timers.
"""

import time

from showfile import FADE

TOGGLE = "toggle"
FLASH = "flash"
SOLO = "solo"
MODES = (TOGGLE, FLASH, SOLO)

SCENE = "scene"
CHASER = "chaser"


class ChaserState:
    """Where a running chaser currently is."""

    def __init__(self, chaser, now):
        self.chaser = chaser
        self.index = 0
        self.beats_seen = 0
        self.entered_at = now

    @property
    def step(self):
        if not self.chaser.steps:
            return None
        return self.chaser.steps[self.index % len(self.chaser.steps)]

    @property
    def scene_name(self):
        step = self.step
        return step.scene if step else None

    def due_at(self):
        """When the timer should fire, or None if this step holds."""
        step = self.step
        if step is None or step.duration_ms <= 0:
            return None
        return self.entered_at + step.duration_ms / 1000.0

    def advance(self, now):
        if not self.chaser.steps:
            return
        self.index = (self.index + 1) % len(self.chaser.steps)
        self.beats_seen = 0
        self.entered_at = now


class Engine:
    def __init__(self, patch, scenes, chasers=None):
        self.patch = patch
        self.scenes = scenes
        self.chasers = chasers or {}
        self.active = []          # ("scene"|"chaser", name), oldest first
        self.running = {}         # chaser name -> ChaserState
        self.master = 255         # 0-255, scales fade channels only
        self.levels = {}          # 'level' faders: number -> (channels, value)
        self.scales = {}          # 'scale' faders: number -> (channels, value)
        self.dirty = True

    # --- shared source handling -------------------------------------------
    def _add(self, kind, name):
        key = (kind, name)
        if key in self.active:
            # Re-pressing moves it to the end so it wins LTP on snap channels.
            self.active.remove(key)
        self.active.append(key)
        self.dirty = True

    def _remove(self, kind, name):
        key = (kind, name)
        if key in self.active:
            self.active.remove(key)
            self.dirty = True

    # --- scenes -----------------------------------------------------------
    def is_active(self, name):
        """True if a scene OR chaser of this name is running.

        One predicate because the LED layer only wants to know whether the
        pad's target is live, and should not care which kind it is.
        """
        return (SCENE, name) in self.active or name in self.running

    def activate(self, name):
        if name in self.scenes:
            self._add(SCENE, name)

    def deactivate(self, name):
        self._remove(SCENE, name)

    def toggle(self, name):
        if (SCENE, name) in self.active:
            self.deactivate(name)
        else:
            self.activate(name)

    def solo(self, name):
        """Make this the only live source."""
        self.clear()
        self.activate(name)

    # --- chasers ----------------------------------------------------------
    def start_chaser(self, name, now=None):
        chaser = self.chasers.get(name)
        if chaser is None or not chaser.steps:
            return
        now = time.monotonic() if now is None else now
        self.running[name] = ChaserState(chaser, now)
        self._add(CHASER, name)

    def stop_chaser(self, name):
        if name in self.running:
            del self.running[name]
        self._remove(CHASER, name)

    def toggle_chaser(self, name, now=None):
        if name in self.running:
            self.stop_chaser(name)
        else:
            self.start_chaser(name, now)

    def solo_chaser(self, name, now=None):
        self.clear()
        self.start_chaser(name, now)

    def step_chaser(self, name=None, now=None):
        """Advance one chaser, or every running one when name is None.

        The blank-target case is what makes a single pad useful as a manual
        tempo tap across whatever happens to be running.
        """
        now = time.monotonic() if now is None else now
        targets = [name] if name else list(self.running)
        for target in targets:
            state = self.running.get(target)
            if state:
                state.advance(now)
                self.dirty = True

    def on_beat(self, beat, now=None):
        """Called once per musical beat by the OS2L clock.

        Position is DERIVED from the track's beat number, not counted. That
        one decision removes every awkward case the protocol testing turned
        up: a pause resumes on the correct step, a seek lands where the
        music is, a deck change re-phases to the new track, and none of it
        needs the change flag or any special handling. A counter would drift
        out of alignment at every one of those and only show it mid-set.

        Chasers that are not fully beat-synced are left to their timers.
        """
        now = time.monotonic() if now is None else now
        for state in self.running.values():
            chaser = state.chaser
            if not chaser.beat_synced:
                continue
            index = chaser.step_at(beat.pos)
            if index != state.index:
                state.index = index
                state.entered_at = now
                self.dirty = True

    def tick(self, now=None):
        """Fire any step timers that are due. Call often; it is cheap."""
        now = time.monotonic() if now is None else now
        for state in self.running.values():
            # A beat-synced chaser is driven only by the clock. If the music
            # stops it HOLDS rather than free-running on a timer, which is
            # what keeps it in phase when the music comes back.
            if state.chaser.beat_synced:
                continue
            due = state.due_at()
            if due is not None and now >= due:
                state.advance(now)
                self.dirty = True

    def chaser_position(self, name):
        """(index, total) for a running chaser, or None."""
        state = self.running.get(name)
        if not state:
            return None
        return (state.index + 1, len(state.chaser.steps))

    # --- global -----------------------------------------------------------
    def clear(self):
        # Fader state is deliberately NOT cleared: it reflects the physical
        # position of a fader, and zeroing it would leave the software
        # disagreeing with the hardware until you touched it.
        if self.active or self.running:
            self.active = []
            self.running = {}
            self.dirty = True

    def set_level(self, fader, channels, value):
        """A level fader: a live scene whose values you are dialling.

        Merges HTP with everything else, exactly as a scene touching those
        channels would. That is the whole design -- it is not a new kind of
        source, so it needs no place in the LTP ordering and cannot fight
        with scenes for a snap channel (level faders are refused on those
        at load time).

        A sweep is ~127 messages, so a no-op is worth suppressing -- but the
        comparison covers the CHANNELS as well as the value. Comparing the
        value alone would strand a fader whose channels changed under it
        (a re-patch and reload) at the same physical position: the update it
        needs to reach the new channels looks like the no-op it is not.
        """
        value = max(0, min(255, int(value)))
        entry = (tuple(channels), value)
        if self.levels.get(fader) == entry:
            return
        self.levels[fader] = entry
        self.dirty = True

    def set_scale(self, fader, channels, value):
        """A scale fader: a master for one group of channels.

        Where a level fader ADDS (HTP, can only raise), a scale fader
        MULTIPLIES (can only lower). Two different jobs: 'bring the pars up'
        versus 'take the pars down without touching the scene'. Stacked
        scale faders multiply, which is what makes them behave like the
        global master, just narrower.

        Diffed on (channels, value) for the same reason as set_level.
        """
        value = max(0, min(255, int(value)))
        entry = (tuple(channels), value)
        if self.scales.get(fader) == entry:
            return
        self.scales[fader] = entry
        self.dirty = True

    def set_master(self, value):
        value = max(0, min(255, int(value)))
        if value != self.master:
            self.master = value
            self.dirty = True

    # --- merge ------------------------------------------------------------
    def _levels_for(self, kind, name):
        if kind == SCENE:
            scene = self.scenes.get(name)
            return scene.levels if scene else None
        state = self.running.get(name)
        if not state:
            return None
        scene = self.scenes.get(state.scene_name)
        return scene.levels if scene else None

    def output(self):
        """Merge every live source into {channel: value}."""
        levels = {}
        for kind, name in self.active:          # oldest -> newest
            source = self._levels_for(kind, name)
            if not source:
                continue
            for channel, value in source.items():
                if self.patch.mode(channel) == FADE:
                    levels[channel] = max(levels.get(channel, 0), value)
                else:
                    levels[channel] = value     # LTP: later source wins

        # Level faders merge last but still HTP, so a fader can raise a
        # channel above what the scenes ask for but never pull one down.
        # Dimming a scene is what the master is for.
        for channels, value in self.levels.values():
            if not value:
                continue
            for channel in channels:
                levels[channel] = max(levels.get(channel, 0), value)

        # Group scaling comes after everything additive, so a scale fader
        # governs whatever the scenes and level faders produced. Master is
        # applied last, on top: value x group x master.
        for channels, value in self.scales.values():
            if value == 255:
                continue
            factor = value / 255
            for channel in channels:
                if channel in levels and self.patch.mode(channel) == FADE:
                    levels[channel] = round(levels[channel] * factor)

        self.dirty = False
        return self._scaled(levels)

    def _scaled(self, levels):
        """Apply the master to fade channels only.

        Scaling a snap channel would be a bug, not a dimming: half of colour
        index 31 is colour index 15, a different colour. Selectors are never
        touched by the master.
        """
        if self.master == 255:
            return levels
        scale = self.master / 255
        return {ch: (round(val * scale) if self.patch.mode(ch) == FADE else val)
                for ch, val in levels.items()}
