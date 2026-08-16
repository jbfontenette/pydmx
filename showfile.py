"""Load the show definition from CSV.

Three files, deliberately normalised so a rig with many identical fixtures
doesn't mean repeating yourself:

  profiles.csv   fixture TYPES -- what features a model has, at what offset
                 from its start address, and how each behaves. Transcribed
                 from the manufacturer's DMX chart. Written once per model.

  fixtures.csv   the PATCH -- instances. Name, profile, start address.
                 Adding another identical fixture is one line.

  scenes.csv     the LOOKS -- sparse (fixture, feature, value) rows.

DMX channel = fixture.address + feature.offset - 1

Offsets are 1-BASED so they match the fixture manual's own numbering
("Channel 1: Dimmer"). Transcribe without arithmetic.
"""

import csv
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch

FADE = "fade"
SNAP = "snap"


@dataclass
class Feature:
    """One controllable parameter of a fixture type.

    mode governs two things, and it's the same distinction both times:

      fade  a level (intensity, RGB, pan/tilt). Safe to crossfade.
            Merges HTP: highest value among active scenes wins.
      snap  a selector (colour wheel, gobo, strobe, mode). The value indexes
            the fixture's lookup table, it is NOT a level -- fading one
            sweeps through every intervening setting. Merges LTP.
    """
    name: str
    offset: int          # 1-based, as printed in the fixture manual
    mode: str = FADE
    values: dict = field(default_factory=dict)   # label -> (lo, hi) inclusive

    @property
    def fadeable(self):
        return self.mode == FADE

    def resolve(self, token):
        """Accept a raw number or one of this feature's named values.

        Named values resolve to the MIDDLE of their range. Fixture manuals
        document these as bands ("8-15 = red"), and the edges are where a
        transcription slip or an off-by-one lands you in the next colour.
        The midpoint is the safe place to sit.
        """
        token = str(token).strip()
        if token.isdigit():
            value = int(token)
            if not 0 <= value <= 255:
                raise ValueError(f"value {value} out of range 0-255")
            return value
        band = self.values.get(token.lower())
        if band is None:
            known = ", ".join(sorted(self.values)) or "none defined"
            raise ValueError(f"'{token}' is not a named value of "
                             f"'{self.name}'. Known: {known}")
        lo, hi = band
        return (lo + hi) // 2

    def label(self, value):
        """Name covering this value, or None. First match wins."""
        for name, (lo, hi) in self.values.items():
            if lo <= value <= hi:
                return name
        return None


@dataclass
class Profile:
    name: str
    features: dict = field(default_factory=dict)   # lowercase name -> Feature

    @property
    def footprint(self):
        """How many DMX channels one instance consumes."""
        return max((f.offset for f in self.features.values()), default=0)


@dataclass
class Fixture:
    name: str
    profile: Profile
    address: int

    def channel(self, feature_name):
        feature = self.profile.features.get(feature_name.lower())
        if feature is None:
            known = ", ".join(sorted(self.profile.features))
            raise KeyError(f"fixture '{self.name}' (profile "
                           f"'{self.profile.name}') has no feature "
                           f"'{feature_name}'. Known: {known}")
        return self.address + feature.offset - 1

    @property
    def channels(self):
        return range(self.address, self.address + self.profile.footprint)


class Patch:
    """The rig. Resolves (fixture, feature) pairs to raw DMX channels."""

    def __init__(self, fixtures):
        self.fixtures = {f.name: f for f in fixtures}
        self._by_channel = {}     # channel -> (fixture, feature)
        for fixture in fixtures:
            for feature in fixture.profile.features.values():
                ch = fixture.address + feature.offset - 1
                self._by_channel[ch] = (fixture, feature)

    def match(self, pattern):
        """Fixtures matching a glob. 'wash*' hits wash1..wash4; '*' hits all."""
        return [f for name, f in self.fixtures.items() if fnmatch(name, pattern)]

    def resolve(self, fixture_name, feature_name):
        fixture = self.fixtures.get(fixture_name)
        if fixture is None:
            raise KeyError(f"unknown fixture '{fixture_name}'")
        return fixture.channel(feature_name)

    def mode(self, channel):
        entry = self._by_channel.get(channel)
        return entry[1].mode if entry else FADE

    def label(self, channel):
        entry = self._by_channel.get(channel)
        if not entry:
            return str(channel)
        fixture, feature = entry
        return f"{channel} ({fixture.name}.{feature.name})"

    def conflicts(self):
        """Overlapping or out-of-range patches -- a classic rig error."""
        problems = []
        for fixture in self.fixtures.values():
            end = fixture.address + fixture.profile.footprint - 1
            if fixture.address < 1 or end > 512:
                problems.append(f"'{fixture.name}' occupies {fixture.address}"
                                f"-{end}, outside 1-512")
        seen = list(self.fixtures.values())
        for i, a in enumerate(seen):
            for b in seen[i + 1:]:
                overlap = set(a.channels) & set(b.channels)
                if overlap:
                    problems.append(
                        f"'{a.name}' ({min(a.channels)}-{max(a.channels)}) "
                        f"overlaps '{b.name}' "
                        f"({min(b.channels)}-{max(b.channels)})")
        return problems

    def __len__(self):
        return len(self.fixtures)


@dataclass
class Scene:
    name: str
    levels: dict = field(default_factory=dict)   # raw channel -> value

    def __len__(self):
        return len(self.levels)


def _rows(path):
    """Yield cleaned rows, skipping blanks and # comments."""
    with open(path, newline="", encoding="utf-8") as handle:
        lines = [ln for ln in handle
                 if ln.strip() and not ln.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    reader.fieldnames = [f.strip().lower() for f in (reader.fieldnames or [])]
    for line_no, row in enumerate(reader, start=2):
        yield line_no, {k: (v or "").strip() for k, v in row.items() if k}


def parse_values(spec, where, feature_name, warn=None):
    """'off=0;red=8-15;green=42' -> {'off': (0,0), 'red': (8,15), ...}

    Separator is ';' or '|' so commas stay free for CSV itself. A bare
    number means a band of one.
    """
    values = {}
    for part in re.split(r"[;|]", spec or ""):
        part = part.strip()
        if not part:
            continue
        label, sep, band = part.partition("=")
        if not sep:
            raise ValueError(f"{where}: expected name=value in '{part}'")
        label = label.strip().lower()
        band = band.strip()
        if not label:
            raise ValueError(f"{where}: missing name in '{part}'")

        try:
            if "-" in band:
                low, high = (int(x) for x in band.split("-", 1))
            else:
                low = high = int(band)
        except ValueError:
            raise ValueError(f"{where}: bad value '{band}' for '{label}' -- "
                             f"expected a number or a range like 8-15")

        if not (0 <= low <= 255 and 0 <= high <= 255):
            raise ValueError(f"{where}: '{label}' range {low}-{high} "
                             f"outside 0-255")
        if low > high:
            raise ValueError(f"{where}: '{label}' range {low}-{high} "
                             f"is backwards")
        if label in values:
            raise ValueError(f"{where}: '{label}' defined twice on "
                             f"'{feature_name}'")

        # Overlaps make label() ambiguous, but manuals do contain them, so
        # warn rather than refuse -- refusing mid-gig during a live reload
        # would be worse than a first-match-wins display.
        if warn:
            for other, (o_low, o_high) in values.items():
                if low <= o_high and o_low <= high:
                    warn(f"{where}: '{label}' ({low}-{high}) overlaps "
                         f"'{other}' ({o_low}-{o_high}) -- first match wins")
        values[label] = (low, high)
    return values


def load_profiles(path, warn=None):
    profiles = {}
    for line_no, row in _rows(path):
        name = row.get("profile", "")
        feature_name = row.get("feature", "")
        if not name or not feature_name:
            raise ValueError(f"{path} line {line_no}: needs profile and feature")

        try:
            offset = int(row["offset"])
        except (KeyError, ValueError):
            raise ValueError(f"{path} line {line_no}: bad offset "
                             f"'{row.get('offset')}'")
        if offset < 1:
            raise ValueError(f"{path} line {line_no}: offset is 1-based, "
                             f"got {offset}")

        mode = (row.get("mode") or FADE).lower()
        if mode not in (FADE, SNAP):
            raise ValueError(f"{path} line {line_no}: mode must be "
                             f"'{FADE}' or '{SNAP}', got '{mode}'")

        profile = profiles.setdefault(name, Profile(name))
        key = feature_name.lower()
        if key in profile.features:
            raise ValueError(f"{path} line {line_no}: profile '{name}' "
                             f"defines '{feature_name}' twice")
        clash = [f.name for f in profile.features.values() if f.offset == offset]
        if clash:
            raise ValueError(f"{path} line {line_no}: offset {offset} already "
                             f"used by '{clash[0]}' in profile '{name}'")
        values = parse_values(row.get("values", ""),
                              f"{path} line {line_no}", feature_name, warn)
        profile.features[key] = Feature(feature_name, offset, mode, values)
    return profiles


def load_patch(path, profiles):
    fixtures = []
    seen = set()
    for line_no, row in _rows(path):
        name = row.get("fixture", "")
        if not name:
            raise ValueError(f"{path} line {line_no}: missing fixture name")
        if name in seen:
            raise ValueError(f"{path} line {line_no}: fixture '{name}' "
                             f"defined twice")
        seen.add(name)

        profile = profiles.get(row.get("profile", ""))
        if profile is None:
            raise ValueError(f"{path} line {line_no}: unknown profile "
                             f"'{row.get('profile')}'. Known: "
                             f"{', '.join(sorted(profiles))}")

        try:
            address = int(row["address"])
        except (KeyError, ValueError):
            raise ValueError(f"{path} line {line_no}: bad address "
                             f"'{row.get('address')}'")

        fixtures.append(Fixture(name, profile, address))
    return Patch(fixtures)


def load_scenes(path, patch, warn=print):
    scenes = {}
    for line_no, row in _rows(path):
        name = row.get("scene", "")
        pattern = row.get("fixture", "")
        feature = row.get("feature", "")
        if not (name and pattern and feature):
            raise ValueError(f"{path} line {line_no}: needs scene, fixture "
                             f"and feature")

        # Deliberately NOT parsed here. A glob can span profiles, and
        # "red" may be 10 on one model and 42 on another, so the value can
        # only be resolved once we know which fixture we are talking to.
        raw_value = row.get("value", "")
        if raw_value == "":
            raise ValueError(f"{path} line {line_no}: missing value")

        targets = patch.match(pattern)
        if not targets:
            # Almost always a typo, and a silent no-op is worse than noise.
            raise ValueError(f"{path} line {line_no}: '{pattern}' matches no "
                             f"fixture. Known: {', '.join(sorted(patch.fixtures))}")

        scene = scenes.setdefault(name, Scene(name))
        for fixture in targets:
            try:
                channel = fixture.channel(feature)
                spec = fixture.profile.features[feature.lower()]
                value = spec.resolve(raw_value)
            except ValueError as exc:
                raise ValueError(f"{path} line {line_no}: {fixture.name}: {exc}")
            except KeyError as exc:
                # A glob hitting a fixture that lacks the feature is usually
                # intent ("all fixtures to full", some have no colour wheel),
                # so warn rather than fail -- unless it was named explicitly.
                if any(c in pattern for c in "*?["):
                    if warn:
                        warn(f"{path} line {line_no}: skipping {fixture.name} "
                             f"-- no '{feature}' feature")
                    continue
                raise ValueError(f"{path} line {line_no}: {exc}")

            if channel in scene.levels and scene.levels[channel] != value and warn:
                warn(f"{path} line {line_no}: scene '{name}' sets "
                     f"{patch.label(channel)} twice "
                     f"({scene.levels[channel]} -> {value})")
            scene.levels[channel] = value

    return scenes


@dataclass
class ChaserStep:
    """One step of a chaser.

    duration_ms  auto-advance after this long. 0 or blank means HOLD until
                 something advances it -- a pad tap, or later a beat.
    beats        advance after this many beats from an external clock.
                 Parsed and counted now, but nothing feeds beats yet; it is
                 here so an OS2L source can be wired in without rewriting
                 anyone's chasers.csv.
    """
    scene: str
    duration_ms: int = 0
    beats: int = 0


@dataclass
class Chaser:
    name: str
    steps: list = field(default_factory=list)

    def __len__(self):
        return len(self.steps)

    @property
    def beat_synced(self):
        """True when EVERY step declares a beat count.

        All-or-nothing on purpose. A beat-synced chaser derives its position
        from the track's beat number rather than counting, and that only
        works if the whole cycle has a known length in beats. Mixing a beat
        step with a timed one leaves the cycle length undefined, so such a
        chaser stays on timers and warns at load.
        """
        return bool(self.steps) and all(s.beats > 0 for s in self.steps)

    @property
    def cycle_beats(self):
        return sum(s.beats for s in self.steps)

    def step_at(self, pos):
        """Which step index the beat number `pos` falls on.

        Phase-locked: derived from pos, never counted. Pause, seek and deck
        changes therefore need no handling at all -- the answer is always
        whatever the music says it is.

        Python's modulo is non-negative, which matters because pos runs
        negative before the beat-grid origin. Do not "fix" it with abs().
        """
        total = self.cycle_beats
        if total <= 0:
            return 0
        offset = pos % total
        running = 0
        for index, step in enumerate(self.steps):
            running += step.beats
            if offset < running:
                return index
        return len(self.steps) - 1


def load_chasers(path, scenes, warn=print):
    """chaser,step,scene,duration_ms[,beats]

    Steps are ordered by the 'step' column, not by file order, so you can
    insert one without renumbering everything below it.
    """
    import os
    if not os.path.exists(path):
        return {}

    collected = {}
    for line_no, row in _rows(path):
        name = row.get("chaser", "")
        scene = row.get("scene", "")
        if not name or not scene:
            raise ValueError(f"{path} line {line_no}: needs chaser and scene")

        try:
            order = int(row["step"])
        except (KeyError, ValueError):
            raise ValueError(f"{path} line {line_no}: bad step "
                             f"'{row.get('step')}' -- expected a number")

        def _number(field, default=0):
            raw = (row.get(field) or "").strip()
            if not raw:
                return default
            try:
                value = int(raw)
            except ValueError:
                raise ValueError(f"{path} line {line_no}: bad {field} '{raw}'")
            if value < 0:
                raise ValueError(f"{path} line {line_no}: {field} cannot "
                                 f"be negative")
            return value

        duration = _number("duration_ms")
        beats = _number("beats")

        if scene not in scenes:
            # Same reasoning as an unknown scene in mapping.csv: a name that
            # does not resolve is drift between files you edit separately,
            # not a misunderstood file. Drop the step, keep the rig running.
            if warn:
                warn(f"{path} line {line_no}: chaser '{name}' step {order} "
                     f"uses unknown scene '{scene}' -- step SKIPPED")
            continue

        collected.setdefault(name, []).append(
            (order, ChaserStep(scene, duration, beats), line_no))

    chasers = {}
    for name, entries in collected.items():
        seen = {}
        for order, _, line_no in entries:
            if order in seen and warn:
                warn(f"{path} line {line_no}: chaser '{name}' has two "
                     f"step {order} rows (also line {seen[order]})")
            seen.setdefault(order, line_no)
        entries.sort(key=lambda e: e[0])
        chaser = Chaser(name, [step for _, step, _ in entries])

        # Partly-beat-synced is almost always a half-finished edit, and it
        # fails silently -- the chaser just ignores the beat clock. Say so.
        with_beats = sum(1 for s in chaser.steps if s.beats > 0)
        if 0 < with_beats < len(chaser.steps) and warn:
            warn(f"{path}: chaser '{name}' has beats on {with_beats} of "
                 f"{len(chaser.steps)} steps -- beat sync needs ALL steps to "
                 f"declare beats, so this one will use timers instead")
        chasers[name] = chaser

    return chasers


@dataclass
class Binding:
    """One control on the APC bound to something in the show."""
    note: int
    shift: bool          # True = only active while SHIFT is held
    kind: str            # scene | chaser | chaser_step | tap | clear | reload
                         # on a fader: master | bpm
    target: str
    mode: str            # toggle | flash | solo
    colour: str          # resolved colour name (see colours.py)
    channels: tuple = () # for a 'level' fader: the channels it drives


def parse_pad(token):
    """Accept a raw note number, or friendlier position notation.

    12      raw note number
    r1c4    grid row 1, column 4 -- row 0 is the BOTTOM row
    t3      track button 3
    s2      scene launch button 2 (s1 is the TOP one)
    f8      fader 8 -- returns ("fader", 8), a separate key space because
            fader CCs 48-56 would otherwise collide with grid notes 48-56
    """
    token = token.strip().lower()
    if token.isdigit():
        note = int(token)
        if not 0 <= note <= 127:
            raise ValueError(f"note {note} out of range 0-127")
        return note
    if token.startswith("r") and "c" in token:
        row, _, col = token[1:].partition("c")
        row, col = int(row), int(col)
        if not (0 <= row <= 7 and 0 <= col <= 7):
            raise ValueError(f"'{token}': row and col must be 0-7")
        return row * 8 + col
    if token.startswith("f") and token[1:].isdigit():
        n = int(token[1:])
        if not 1 <= n <= 9:
            raise ValueError(f"'{token}': faders are f1-f9")
        return ("fader", n)
    if token.startswith("t"):
        n = int(token[1:])
        if not 1 <= n <= 8:
            raise ValueError(f"'{token}': track buttons are t1-t8")
        return 0x64 + n - 1
    if token.startswith("s"):
        n = int(token[1:])
        if not 1 <= n <= 8:
            raise ValueError(f"'{token}': scene buttons are s1-s8")
        return 0x70 + n - 1
    raise ValueError(f"cannot parse pad '{token}'")


TRUTHY = ("yes", "y", "true", "1", "shift", "x")


def _level_channels(target, patch, where, warn):
    """Resolve 'par*.dimmer' to the DMX channels a level or scale fader drives.

    Returns a tuple of channels, or () if the row should be skipped.
    """
    target = (target or "").strip()
    if "." not in target:
        raise ValueError(f"{where}: this fader needs a target like "
                         f"'par*.dimmer' (fixture glob, dot, feature)")
    pattern, _, feature = target.rpartition(".")
    if patch is None:
        return ()

    matched = patch.match(pattern)
    if not matched:
        if warn:
            warn(f"{where}: '{pattern}' matches no fixture -- fader SKIPPED. "
                 f"Known: {', '.join(sorted(patch.fixtures))}")
        return ()

    channels, snap = [], []
    for fixture in matched:
        spec = fixture.profile.features.get(feature.lower())
        if spec is None:
            continue                      # glob across mixed models is fine
        if spec.mode == SNAP:
            snap.append(fixture.name)
            continue
        channels.append(fixture.address + spec.offset - 1)

    if snap:
        # A fader sweeping a selector walks through every colour or gobo on
        # the way. That is the one thing snap channels exist to prevent, so
        # refuse rather than produce a fader that looks broken in use.
        # Both kinds are wrong on a selector: a level fader sweeps through
        # every setting, and scaling one turns colour 31 into colour 15,
        # which is a different colour rather than a dimmer version of it.
        raise ValueError(f"{where}: '{feature}' is a SNAP channel on "
                         f"{', '.join(snap)} -- fading or scaling a selector "
                         f"changes the setting, not its level. Use scenes.")
    if not channels:
        if warn:
            warn(f"{where}: no fixture matching '{pattern}' has a "
                 f"'{feature}' feature -- fader SKIPPED")
        return ()
    return tuple(sorted(channels))


def load_mapping(path, scenes, chasers=None, patch=None, warn=print):
    """Bind APC controls to scenes, chasers and actions.

    Keyed by (note, shift) so SHIFT gives a second full layer -- 128 usable
    bindings instead of 64.
    """
    chasers = chasers or {}
    bindings = {}
    faders = {}
    unknown_scenes = []
    for line_no, row in _rows(path):
        try:
            note = parse_pad(row.get("pad", ""))
        except ValueError as exc:
            raise ValueError(f"{path} line {line_no}: {exc}")

        if note == 0x7A:
            raise ValueError(f"{path} line {line_no}: note 122 is SHIFT, the "
                             f"modifier itself -- it cannot be bound")

        shift = (row.get("shift") or "").strip().lower() in TRUTHY

        kind = (row.get("type") or "scene").lower()
        is_fader = isinstance(note, tuple)
        valid = (("master", "bpm", "level", "scale") if is_fader else
                 ("scene", "chaser", "chaser_step", "tap", "clear", "reload"))
        if kind not in valid:
            what = "a fader" if is_fader else "a pad"
            raise ValueError(f"{path} line {line_no}: type for {what} must be "
                             f"one of {', '.join(valid)}, got '{kind}'")

        if is_fader:
            # Faders live in their own dict: they carry a continuous value
            # rather than press/release. Nothing else in the row applies --
            # the APC mini mk2 faders have NO LEDs, so a colour cannot mean
            # anything, and there is no press to have a mode or a target.
            channels = ()
            if kind in ("level", "scale"):
                channels = _level_channels(row.get("target", ""), patch,
                                           f"{path} line {line_no}", warn)
                if not channels:
                    continue

            ignored = [name for name, value in
                       (("target", row.get("target")
                         if kind not in ("level", "scale") else ""),
                        ("mode", row.get("mode")),
                        ("colour", row.get("colour")))
                       if (value or "").strip()]
            if ignored and warn:
                warn(f"{path} line {line_no}: {', '.join(ignored)} ignored on "
                     f"a fader -- faders have no LED and no press. "
                     f"Leave those columns blank.")
            faders[note[1]] = Binding(note[1], False, kind,
                                      row.get("target", "")
                                      if kind in ("level", "scale") else "",
                                      "", "", channels)
            continue

        target = row.get("target", "")
        if kind == "scene":
            if not target:
                raise ValueError(f"{path} line {line_no}: scene needs a target")
            if target not in scenes:
                # Warn and drop the binding rather than refuse to start.
                # A structural error (bad pad, bad type) means the file is
                # not understood and stopping is right. But a name that does
                # not resolve is ordinary drift between two files you edit
                # separately -- one stale row should cost you one pad, not
                # the whole rig. The pad simply goes dark and says why.
                # Collected and reported once at the end. Repeating the
                # full scene list per offending line buried the summary
                # under its own error messages.
                unknown_scenes.append((line_no, target))
                continue

        if kind == "chaser":
            if not target:
                raise ValueError(f"{path} line {line_no}: chaser needs a target")
            if target not in chasers:
                if warn:
                    warn(f"{path} line {line_no}: pad bound to unknown chaser "
                         f"'{target}' -- binding SKIPPED. Known chasers: "
                         f"{', '.join(chasers) or 'none'}")
                continue

        if kind == "chaser_step":
            # A blank target means "advance every running chaser", which is
            # the useful default for a single tap-tempo pad.
            if target and target not in chasers:
                if warn:
                    warn(f"{path} line {line_no}: step pad targets unknown "
                         f"chaser '{target}' -- binding SKIPPED. Known: "
                         f"{', '.join(chasers) or 'none'}")
                continue

        # Blank is the honest value for the one-shot kinds below, and it is
        # what the column defaults to. Keep the raw text so an explicitly
        # written mode can be distinguished from an omitted one.
        raw_mode = (row.get("mode") or "").strip().lower()
        mode = raw_mode or "toggle"
        if mode not in ("toggle", "flash", "solo"):
            raise ValueError(f"{path} line {line_no}: mode must be toggle, "
                             f"flash or solo, got '{mode}'")

        # These fire once on press and never consult mode. Writing 'toggle'
        # there suggests a latching behaviour that does not exist, so say so
        # rather than accept it silently.
        ONE_SHOT = ("clear", "reload", "chaser_step", "tap")
        if kind in ONE_SHOT and raw_mode and warn:
            warn(f"{path} line {line_no}: mode '{raw_mode}' is ignored for "
                 f"type '{kind}' -- it fires once on press. Leave the mode "
                 f"column blank.")

        import colours as colour_names
        try:
            colour = colour_names.resolve(row.get("colour") or "white")
        except ValueError as exc:
            raise ValueError(f"{path} line {line_no}: {exc}")

        key = (note, shift)
        if key in bindings and warn:
            layer = "shift+" if shift else ""
            warn(f"{path} line {line_no}: {layer}pad {note} bound twice "
                 f"('{bindings[key].target}' -> '{target}')")
        bindings[key] = Binding(note, shift, kind, target, mode, colour)

    if unknown_scenes and warn:
        lines = ", ".join(f"line {n} '{t}'" for n, t in unknown_scenes)
        warn(f"{path}: {len(unknown_scenes)} pad(s) bound to unknown "
             f"scenes -- SKIPPED, those pads do nothing: {lines}")
        warn(f"  scenes that do exist: {', '.join(scenes)}")

    if 9 not in faders and warn:
        warn(f"{path}: no fader bound to 'master'. Add:  f9,master")

    unbound = [n for n in scenes if not any(
        b.kind == "scene" and b.target == n for b in bindings.values())]
    if unbound and warn:
        warn(f"scenes with no pad: {', '.join(unbound)}")
    if not any(b.kind == "reload" for b in bindings.values()) and warn:
        warn("no 'reload' binding -- edits to the CSVs will need a restart "
             "or --watch")
    return bindings, faders


class Show:
    """The show CSVs as one reloadable unit.

    Reload is atomic: everything is parsed into fresh objects first, and the
    live show is only swapped in if all three files parsed cleanly. A typo in
    scenes.csv therefore leaves the running show exactly as it was, which is
    the behaviour you want when you are editing during a gig.
    """

    def __init__(self, directory="show"):
        import os
        self.paths = {
            "profiles": os.path.join(directory, "profiles.csv"),
            "fixtures": os.path.join(directory, "fixtures.csv"),
            "scenes": os.path.join(directory, "scenes.csv"),
            "chasers": os.path.join(directory, "chasers.csv"),
        }
        # mapping.csv is looked for in show/ first, then next to the script.
        # An earlier version checked only show/ and silently loaded ZERO
        # bindings if it was elsewhere -- pads then did nothing, with no
        # message explaining why. Never fail silently on a missing binding
        # file again: mapping_path records what was actually used.
        self._mapping_candidates = [
            os.path.join(directory, "mapping.csv"),
            "mapping.csv",
        ]
        self.mapping_path = None
        self.profiles = {}
        self.patch = Patch([])
        self.scenes = {}
        self.chasers = {}
        self.bindings = {}
        self.faders = {}
        self.warnings = []
        self._stamps = {}

    def _resolve_mapping(self):
        import os
        for candidate in self._mapping_candidates:
            if os.path.exists(candidate):
                return candidate
        return None

    def stamps(self):
        """Current mtimes of every watched file. Safe to call off-thread.

        This is the ONE method on Show another thread may call: it stats
        files and reads two attributes that are only ever rebound whole
        (never mutated in place), so it cannot observe a half-built show.
        Everything else here must be called from the thread that owns the
        show -- see the threading doctrine in CLAUDE.md.

        A caller that keeps its own copy can therefore detect a change
        without waiting for the owning thread to accept it, which is what
        the --watch detector in controller.py does.
        """
        import os
        stamps = {}
        watched = dict(self.paths)
        if self.mapping_path:
            watched["mapping"] = self.mapping_path
        for key, path in watched.items():
            try:
                stamps[key] = os.path.getmtime(path)
            except OSError:
                stamps[key] = None
        return stamps

    def changed_on_disk(self, stamps=None):
        """True if the files differ from the last SUCCESSFUL load or reload.

        Pass a stamps() snapshot to ask the question about that snapshot
        instead of taking a fresh one. The watcher does exactly that, so its
        own baseline and this question come from a single look at the files:
        with two looks, a save landing between them is reported twice.

        Note the asymmetry: a failed reload does not advance the baseline, so
        this keeps reporting True until the files parse. A caller that wants
        one report per save should compare successive stamps() itself.
        """
        return (self.stamps() if stamps is None else stamps) != self._stamps

    def binding_for(self, note, shift):
        """Binding for a control, falling back to the base layer.

        A shifted press with nothing bound on the shift layer falls through
        to the unshifted binding, so SHIFT only overrides where you actually
        defined an override.
        """
        return (self.bindings.get((note, True)) if shift else None) \
            or self.bindings.get((note, False))

    def layer(self, shift):
        """{note: binding} for the layer currently visible on the grid."""
        base = {n: b for (n, s), b in self.bindings.items() if not s}
        if not shift:
            return base
        base.update({n: b for (n, s), b in self.bindings.items() if s})
        return base

    def _parse(self):
        """Build a complete new show. Raises without touching current state."""
        warnings = []
        profiles = load_profiles(self.paths["profiles"], warn=warnings.append)
        patch = load_patch(self.paths["fixtures"], profiles)
        scenes = load_scenes(self.paths["scenes"], patch, warn=warnings.append)

        chasers = load_chasers(self.paths["chasers"], scenes,
                               warn=warnings.append)

        mapping_path = self._resolve_mapping()
        if mapping_path is None:
            warnings.append(
                "NO mapping.csv FOUND -- looked in "
                + " and ".join(self._mapping_candidates)
                + ". Every pad press will do nothing until it exists.")
            bindings, faders = {}, {}
        else:
            bindings, faders = load_mapping(mapping_path, scenes, chasers,
                                            patch, warn=warnings.append)
        self.mapping_path = mapping_path
        return profiles, patch, scenes, chasers, bindings, faders, warnings

    def load(self):
        """Initial load. Raises on error."""
        (self.profiles, self.patch, self.scenes, self.chasers,
         self.bindings, self.faders, self.warnings) = self._parse()
        self._stamps = self.stamps()
        return self.warnings

    def reload(self):
        """Re-read from disk. Returns (ok, message, diff).

        diff is (added, removed, changed) scene-name lists. On failure the
        live show is untouched and message explains why.
        """
        try:
            (profiles, patch, scenes, chasers,
             bindings, faders, warnings) = self._parse()
        except (OSError, ValueError, KeyError) as exc:
            return False, str(exc), ([], [], [])

        old = self.scenes
        added = [n for n in scenes if n not in old]
        removed = [n for n in old if n not in scenes]
        changed = [n for n in scenes
                   if n in old and scenes[n].levels != old[n].levels]

        (self.profiles, self.patch, self.scenes, self.chasers,
         self.bindings, self.faders, self.warnings) = (
            profiles, patch, scenes, chasers, bindings, faders, warnings)
        self._stamps = self.stamps()

        bits = []
        if added:
            bits.append(f"+{len(added)} ({', '.join(added)})")
        if removed:
            bits.append(f"-{len(removed)} ({', '.join(removed)})")
        if changed:
            bits.append(f"~{len(changed)} ({', '.join(changed)})")
        summary = "; ".join(bits) if bits else "no scene changes"
        return True, f"{len(scenes)} scenes -- {summary}", (added, removed, changed)
