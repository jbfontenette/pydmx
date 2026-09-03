"""Internal beat clock, for when Virtual DJ is not there.

Produces the same Beat objects the OS2L listener does, so beat-synced
chasers cannot tell the difference -- an internal clock only has to make
`pos` increment at the right rate, and every phase-locking decision
downstream works unchanged.

Two ways to set the tempo:

  a fader   60-180 BPM across the 127 fader positions, about 1 BPM a step.
            Sets rate only; the phase keeps running so the lights do not
            jump while you hunt for the right number.

  tapping   a pad, four or more times. Sets rate AND phase: the last tap
            becomes pos 0, which is a phrase boundary, so a tapped tempo
            lands its downbeat where you tapped it rather than wherever the
            clock happened to be.

The clock is DISARMED until you give it a tempo. Silence is the right
default -- a rig that starts inventing its own beats because VirtualDJ was
slow to connect would be worse than one that simply holds.
"""

import time

from os2l import Beat

BPM_MIN = 60.0
BPM_MAX = 180.0

# Taps further apart than this start a new measurement rather than being
# averaged into the old one. Two seconds is 30 BPM -- slower than anything
# worth tapping, and short enough that a stale tap never poisons a new tempo.
TAP_RESET_S = 2.0
TAP_HISTORY = 8
TAP_MIN_BPM = 40.0
TAP_MAX_BPM = 250.0


def bpm_from_fader(value):
    """0-127 -> 60-180 BPM, in steps of roughly 1."""
    value = max(0, min(127, int(value)))
    return round(BPM_MIN + value * (BPM_MAX - BPM_MIN) / 127)


def fader_from_bpm(bpm):
    """Inverse, for showing where a tapped tempo sits on the fader."""
    bpm = max(BPM_MIN, min(BPM_MAX, float(bpm)))
    return round((bpm - BPM_MIN) * 127 / (BPM_MAX - BPM_MIN))


class TapTempo:
    """Averages tap intervals into a tempo."""

    def __init__(self):
        self.taps = []

    def tap(self, now=None):
        """Record a tap. Returns the BPM once there are enough, else None."""
        now = time.monotonic() if now is None else now
        if self.taps and now - self.taps[-1] > TAP_RESET_S:
            self.taps = []
        self.taps.append(now)
        if len(self.taps) > TAP_HISTORY:
            self.taps = self.taps[-TAP_HISTORY:]
        return self.bpm

    @property
    def count(self):
        return len(self.taps)

    @property
    def bpm(self):
        if len(self.taps) < 2:
            return None
        intervals = [b - a for a, b in zip(self.taps, self.taps[1:])]
        # Median, not mean: one clumsy tap should not drag the tempo, and
        # with only a handful of samples an outlier dominates an average.
        intervals.sort()
        middle = intervals[len(intervals) // 2]
        if middle <= 0:
            return None
        bpm = 60.0 / middle
        if not TAP_MIN_BPM <= bpm <= TAP_MAX_BPM:
            return None
        return round(bpm, 1)

    def reset(self):
        self.taps = []


class InternalClock:
    """Emits beats at a set tempo. Disarmed until given one."""

    def __init__(self, on_status=None):
        self.bpm = 0.0
        self.armed = False
        self.source = None            # 'fader' or 'tap', for reporting
        self.on_status = on_status or (lambda msg: None)
        self.reanchors = 0
        self._next_at = 0.0
        self._pos = 0
        self.tapper = TapTempo()

    @property
    def period(self):
        return 60.0 / self.bpm if self.bpm else 0.0

    def set_bpm(self, bpm, now=None, source="fader"):
        """Change the rate, keeping the current phase.

        Deliberately does not restart the beat: while you sweep a fader
        looking for the right tempo, the lights should change speed, not
        stutter back to a downbeat on every step.
        """
        now = time.monotonic() if now is None else now
        bpm = float(bpm)
        if bpm <= 0:
            return
        first = not self.armed
        self.bpm = bpm
        self.source = source
        self.armed = True
        if first:
            self._next_at = now
            self._pos = 0

    def tap(self, now=None):
        """Register a tap. Sets rate and phase once there are enough.

        Returns (bpm_or_None, tap_count) so the caller can report progress
        -- the first tap does nothing measurable, and saying so beats a pad
        that appears dead.
        """
        now = time.monotonic() if now is None else now
        bpm = self.tapper.tap(now)
        if bpm:
            self.bpm = bpm
            self.armed = True
            self.source = "tap"
            # The tap IS the downbeat: emit at pos 0, a phrase boundary.
            self._pos = 0
            self._next_at = now
        return bpm, self.tapper.count

    def disarm(self):
        self.armed = False
        self.bpm = 0.0
        self.source = None
        self.tapper.reset()

    def poll(self, now=None):
        """Beats due since the last call. Empty unless armed."""
        now = time.monotonic() if now is None else now
        if not self.armed or self.bpm <= 0:
            return []

        out = []
        period = self.period
        # Guard against a long stall (laptop sleep, a blocking reload)
        # producing a burst of catch-up beats that would race the chaser
        # through several steps at once.
        #
        # Re-anchoring is the right call, but it is not free: the beat now
        # lands wherever the stall ended, so a tapped downbeat has quietly
        # moved. Say so. Chasers derive position from pos, which keeps
        # counting, so nothing jumps a step -- what shifts is the phase
        # against the music, and only an ear can judge that.
        behind = now - self._next_at
        if behind > period * 4:
            self._next_at = now
            self.reanchors += 1
            self.on_status(f"re-anchored after a {behind:.1f}s stall -- "
                           f"phase shifted, re-tap if it has drifted")
        while now >= self._next_at:
            out.append(Beat(pos=self._pos, bpm=self.bpm, strength=None,
                            change=False, at=self._next_at))
            self._pos += 1
            self._next_at += period
        return out

    def resync(self, now=None):
        """Restart the phase from here, keeping the tempo."""
        now = time.monotonic() if now is None else now
        self._pos = 0
        self._next_at = now
