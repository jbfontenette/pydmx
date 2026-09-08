#!/usr/bin/env python3
"""DMX controller: APC mini mk2 pads launch scenes.

    pip install pyserial mido python-rtmidi
    python3 controller.py
    python3 controller.py --no-midi     # keyboard only, for testing
    python3 controller.py --no-dmx      # no adapter needed, dry run
    python3 controller.py --monitor     # publish frames for dmxmon.py
    python3 controller.py --sim         # on-screen APC (run apcsim.py too)
    python3 controller.py --os2l        # beat sync from Virtual DJ
    python3 controller.py --os2l 9997   # ...on a non-default port
    python3 controller.py --os2l --beats  # log every beat (noisy, debugging)
    python3 controller.py --watch       # reload the CSVs when they change
    python3 controller.py --check       # validate CSVs, touch no hardware
    python3 controller.py --feedback pulse   # active pads pulse instead
    python3 controller.py --feedback blink   # active pads blink instead
    python3 controller.py --feedback rgb     # EXPERIMENTAL, SysEx 24-bit

--feedback is a controller.py flag only. apc_leds.py does not accept it.

Tempo fallback: bind a fader to 'bpm' and a pad to 'tap' in mapping.csv.
The internal clock stays silent until you give it a tempo, and yields to
VirtualDJ whenever VirtualDJ is actually delivering beats.

Three threads, each with exactly one job:

    DMX thread    pushes a frame every 33ms, forever. Never blocks on input.
    main loop     polls MIDI, updates the engine, refreshes LEDs, reloads.
    watcher       optional, NOTICES that the CSVs changed on disk. It does
                  not reload them -- it raises a flag and the main loop does.

Input events never write to the port directly -- they mutate engine state,
and the DMX thread transmits whatever it finds. That separation is what keeps
the output steady when the Mac is busy.

The same rule governs the watcher, for the same reason one level up: show and
engine objects are mutated by exactly one thread. A reload swaps the very
dicts the main loop is iterating (bindings in build_leds, active in
engine.output, running in tick), so doing it from the watcher thread could
kill the main loop with 'dict changed size during iteration' and leave the
rig frozen on its last look with no input. Detect off-thread, mutate on the
main loop.
"""

import importlib
import sys
import threading
import time

import engine as engine_mod
import showfile

# dmx is imported inside main(): it needs pyserial, and everything above
# main() -- input routing, reload reconciliation -- is stdlib-only logic the
# tests exercise without any hardware packages installed.

SHOW_DIR = "show"

# Which module provides the control surface. Resolved once in main() from
# --surface; every other call site goes through surface_module() and never
# learns which it got. That is the whole point: a driver is a module with
# the right names on it, not a branch in here.
# Each entry is (vocabulary, driver). They are separate because --check must
# keep working with nothing installed: the vocabulary module is pure tables
# and parsing, while the driver imports mido and opens ports. Validating a
# show should never need a MIDI backend, let alone a device.
SURFACES = {
    "apc": ("surface_constants", "apc"),        # Akai APC mini mk2
    "apcsim": ("surface_constants", "virtualapc"),   # ...drawn by apcsim.py
    "xtouch": ("xtouch_constants", "xtouch"),   # Behringer X-Touch Mini
}

_SURFACE = "apc"
_SURFACE_MODULE = None


def surface_vocab():
    """The control vocabulary: names, ranges, parsing. No hardware, no mido.

    A driver module re-exports all of it, so when one is already loaded --
    which is how the tests inject virtualapc -- that is what comes back.
    """
    if _SURFACE_MODULE is not None:
        return _SURFACE_MODULE
    return importlib.import_module(SURFACES[_SURFACE][0])


def surface_module():
    """The driver. Imports mido, so only call it when opening a device."""
    global _SURFACE_MODULE
    if _SURFACE_MODULE is None:
        _SURFACE_MODULE = importlib.import_module(SURFACES[_SURFACE][1])
    return _SURFACE_MODULE
TICK_S = 0.005          # 200Hz input poll -- well under human perception


def build_leds(surface, show, eng, style="intensity", layer=0):
    """Paint the surface to match engine state.

    Same colour for idle and active; only the brightness changes. That keeps
    the layout readable at a glance -- you learn where red lives, and its
    brightness tells you whether it is running.

    'intensity' is the default: the fixed palette, 25%% when idle and 100%%
    when active. 'rgb' uses SysEx for exact 24-bit colours, but SysEx pad
    colouring is unverified on this unit -- treat it as experimental.

    What gets painted comes from the surface module, not from here: PADS are
    controls with colour and brightness, BUTTONS are binary lamps, RINGS
    show a value. A device with no grid has PADS empty and the loop simply
    does not run.

    layer None means the surface has not said which layer it is showing --
    the X-Touch at startup. Nothing is painted, because lighting the layer
    that happens not to be in front of you is worse than a dark surface for
    one gesture. Invariant 10, applied to a latching layer button.
    """
    mod = surface_module()
    import colours

    if layer is None:
        return

    visible = show.layer(layer)

    def is_on(binding):
        # Chasers light exactly like scenes -- is_active() covers both, so
        # the LED layer never has to care which kind a pad points at.
        return (binding.kind in ("scene", "chaser")
                and eng.is_active(binding.target))

    if style == "rgb" and mod.PADS:
        # Whole grid in two SysEx messages: blanket off, then bound pads.
        surface.pads_rgb([(0, 63, (0, 0, 0))])
        entries = []
        for note, binding in visible.items():
            if note not in mod.PADS:
                continue
            scale = 1.0 if is_on(binding) else colours.IDLE_SCALE
            entries.append((note, note, colours.rgb(binding.colour, scale)))
        surface.pads_rgb(entries)
    elif mod.PADS:
        active_behaviour = mod.FEEDBACK[style]
        for note in mod.PADS:
            binding = visible.get(note)
            if binding is None:
                surface.pad(note, mod.OFF)
            else:
                index = colours.palette(binding.colour)
                surface.pad(note, index,
                            active_behaviour if is_on(binding) else mod.IDLE)

    for note in mod.BUTTONS:
        binding = visible.get(note)
        if binding is None:
            surface.button(note, 0)
        elif mod.BUTTON_SHOWS == "active":
            # A binary lamp cannot show "bound" and "active" at once. Where
            # the buttons ARE the surface, active is the one worth having:
            # the layout you learn, but what is running you have to see.
            surface.button(note, 1 if is_on(binding) else 0)
        else:
            surface.button(note, 1)

    for number in mod.RINGS:
        binding = show.fader_for(number, layer)
        value = fader_value(binding, number, eng)
        if value is not None:
            surface.ring(number, value)


def fader_value(binding, number, eng):
    """Where a continuous control should be sitting, per the engine.

    None when nothing is bound, or when the binding drives something with no
    readable position. Exists so a ring shows the SHOW's value rather than
    the last position the user happened to turn the knob to -- which is what
    it reverts to after a reload, or after a layer switch the device
    remembers but the show does not.
    """
    if binding is None:
        return None
    if binding.kind == "master":
        return round(eng.master / 255 * 127)
    source = (eng.levels if binding.kind == "level"
              else eng.scales if binding.kind == "scale" else None)
    if source is None:
        return None
    entry = source.get((number, binding.layer))
    return None if entry is None else round(entry[1] / 255 * 127)


def apply_fader(number, layer, value, show, eng, state):
    """Route one continuous control. Shared by live moves and startup sync.

    Which fader does what comes from mapping.csv. "Master" was never
    hardware behaviour -- CC 0x38 is an ordinary fader -- so there was no
    reason for it to be the one hardwired choice.

    The engine is keyed by (number, layer), not by number. On the APC the
    layer is always 0 and this reads as it always did, but the X-Touch's
    encoders keep an independent value per layer, so encoder 3 on A and
    encoder 3 on B are two controls that may drive different channels. One
    key would silently merge them.
    """
    binding = show.fader_for(number, layer)
    if binding is None:
        if number == 9:
            eng.set_master(round(value / 127 * 255))     # legacy default
            state["master_pending"] = time.monotonic()
        return

    key = (number, binding.layer)
    if binding.kind == "master":
        eng.set_master(round(value / 127 * 255))
        # A sweep is ~127 messages. Logging each one buries everything
        # else, so defer and print once the move settles.
        state["master_pending"] = time.monotonic()
    elif binding.kind == "level":
        eng.set_level(key, binding.channels, round(value / 127 * 255))
    elif binding.kind == "scale":
        eng.set_scale(key, binding.channels, round(value / 127 * 255))
    elif binding.kind == "bpm":
        internal = state.get("internal")
        if internal is not None:
            import tempo
            internal.set_bpm(tempo.bpm_from_fader(value), source="fader")
            state["bpm_pending"] = time.monotonic()


def describe_active(eng):
    """Readable summary of every live source.

    Needed because eng.active holds (kind, name) tuples now, not bare
    strings: scenes and chasers share one ordered list so that LTP between
    them is well defined. Joining it directly would raise.
    """
    if not eng.active:
        return "nothing active"
    parts = []
    for kind, name in eng.active:
        position = eng.chaser_position(name) if kind == "chaser" else None
        parts.append(f"{name}[{position[0]}/{position[1]}]" if position
                     else name)
    return ", ".join(parts)


def handle(event, show, eng, log, state, actions):
    kind = event[0]

    if kind == "fader":
        _, number, value = event
        apply_fader(number, state["layer"] or 0, value, show, eng, state)
        return

    apc_mod = surface_module()

    if kind == "layer":
        # The surface reports which layer it is showing; how it decides is
        # its own business -- the APC watches SHIFT, the X-Touch infers it
        # from the numbers arriving, since the device never says. Neither
        # mechanism reaches this far.
        state["layer"] = event[1]
        state["relayout"] = True
        return

    note = event[1]

    if kind == "release":
        # Use the binding captured at PRESS time, not a fresh lookup. If you
        # release SHIFT while still holding a flash pad, a fresh lookup would
        # resolve to the other layer and leave the scene stranded on.
        binding = state["held"].pop(note, None)
        if binding and binding.mode == "flash":
            if binding.kind == "chaser":
                eng.stop_chaser(binding.target)
            else:
                eng.deactivate(binding.target)
            log(f"{binding.target} off  [{describe_active(eng)}]")
        return

    binding = show.binding_for(note, state["layer"])
    if binding is None:
        # Unmapped presses are logged deliberately. Silence here is what made
        # a missing mapping.csv look like broken MIDI.
        where = apc_mod.describe_control(note)
        index = state["layer"]
        prefix = f"{apc_mod.LAYER_NAMES[index]}+" if index else ""
        log(f"{prefix}{where} pressed -- no binding in mapping.csv")
        return

    state["held"][note] = binding

    if binding.kind == "reload":
        actions["reload"](note)
        return

    if binding.kind == "clear":
        eng.clear()
        log("cleared")
        return

    if binding.kind == "chaser":
        if binding.mode == "toggle":
            eng.toggle_chaser(binding.target)
        elif binding.mode == "solo":
            eng.solo_chaser(binding.target)
        elif binding.mode == "flash":
            eng.start_chaser(binding.target)
        log(f"chaser {binding.target} "
            f"{'running' if eng.is_active(binding.target) else 'stopped'}"
            f"  [{describe_active(eng)}]")
        return

    if binding.kind == "tap":
        internal = state.get("internal")
        if internal is None:
            log("tap tempo needs --os2l or --internal-clock")
            return
        bpm, count = internal.tap()
        if bpm:
            log(f"tap {count}: {bpm} bpm")
        else:
            log(f"tap {count}: keep tapping")
        return

    if binding.kind == "chaser_step":
        # A blank target advances every running chaser, which is what makes
        # one pad usable as a manual tempo tap over whatever is live. This
        # is the same entry point a future OS2L beat will call.
        eng.step_chaser(binding.target or None)
        log(f"step {binding.target or 'all'}  [{describe_active(eng)}]")
        return

    if binding.mode == "toggle":
        eng.toggle(binding.target)
    elif binding.mode == "solo":
        eng.solo(binding.target)
    elif binding.mode == "flash":
        eng.activate(binding.target)
    log(f"{binding.target} "
        f"{'on' if eng.is_active(binding.target) else 'off'}"
        f"  [{describe_active(eng)}]")


def watch_files(show, stop, request, interval=0.5):
    """The --watch thread. Detects changes; never acts on them.

    Deliberately takes only what it is allowed to touch: mtimes off the show
    and two events. It cannot reach the engine or the parsed show even by
    accident, which is the point -- reloading from here swaps the dicts the
    main loop is iterating and can kill it mid-frame. The main loop picks the
    request up and does the reload itself.

    Keeps its own copy of the stamps rather than calling
    show.changed_on_disk(), which compares against the last SUCCESSFUL
    reload: while a typo sits on disk that stays true, and we would ask the
    main loop to re-parse a broken show twice a second. Comparing successive
    stamps reports each edit once -- including the one that fixes the typo.

    It asks only once the files have STOPPED moving, which is not the same as
    asking on every change. One save is rarely one mtime bump: an editor
    writes and then sets the times, saving five CSVs at once bumps five, and
    a file caught halfway through being written parses as a typo that is not
    there. Waiting for a quiet interval collapses all of that into the single
    reload the user meant, at the cost of one extra interval of latency.
    """
    seen = show.stamps()
    # A save that landed between the load and this thread being scheduled
    # counts as a change: startup is not instant -- the Introduction message
    # alone can wait 1.5s -- and the main loop is already running a show the
    # files no longer describe.
    settling = show.changed_on_disk(seen)

    while not stop.wait(interval):
        stamps = show.stamps()
        if stamps != seen:
            seen = stamps
            settling = True
        elif settling:
            settling = False
            request.set()


def apply_reload(show, eng):
    """Reload the CSVs and reconcile engine state. Returns (ok, message).

    Atomic: if any file fails to parse the running show is untouched, so a
    typo mid-set costs you nothing. Active scenes survive if they still
    exist; ones that vanished are dropped. Running chasers and live fader
    positions are re-resolved against the new show, so nothing keeps driving
    what the files used to say.
    """
    ok, message, _ = show.reload()
    if not ok:
        return False, message
    eng.scenes = show.scenes
    eng.patch = show.patch
    eng.chasers = show.chasers

    def survives(entry):
        kind, name = entry
        return name in (show.chasers if kind == "chaser" else show.scenes)

    dropped = [name for entry in eng.active if not survives(entry)
               for _, name in [entry]]
    eng.active = [entry for entry in eng.active if survives(entry)]
    eng.running = {name: st for name, st in eng.running.items()
                   if name in show.chasers}

    # A running chaser holds a reference to the Chaser object it started
    # with. After a reload that object is stale, so re-point it and clamp
    # the position -- otherwise an edited chaser keeps playing the old steps
    # until it is restarted, which looks exactly like the reload failing.
    for name, st in eng.running.items():
        st.chaser = show.chasers[name]
        if st.chaser.steps:
            st.index %= len(st.chaser.steps)

    # Fader state has exactly the same problem as the stale chaser above:
    # eng.levels/eng.scales hold the channel tuple captured at the last move,
    # resolved against the OLD patch. Re-patch a fixture and reload, and an
    # untouched fader would keep driving the old address -- which reads as
    # the re-patch having silently failed.
    #
    # The stored value IS the fader's physical position (both kinds arrive
    # through the same conversion in apply_fader), so it carries across a
    # change of binding: a fader re-typed from level to scale keeps the
    # position it is physically sitting at, and only its job changes.
    positions = {key: value for key, (_, value)
                 in list(eng.levels.items()) + list(eng.scales.items())}
    levels, scales = {}, {}
    unbound = []
    for key, value in sorted(positions.items()):
        binding = show.faders.get(key)
        kind = binding.kind if binding else None
        if kind == "level":
            levels[key] = (tuple(binding.channels), value)
        elif kind == "scale":
            scales[key] = (tuple(binding.channels), value)
        else:
            # Gone from mapping.csv, re-typed to master or bpm, or dropped by
            # the loader because its glob now matches no fixture. Either way
            # it drives nothing until it is bound and moved again.
            unbound.append(key)
    # master is deliberately NOT reconciled here. It is one scalar with no
    # per-fader memory, so a fader newly typed 'master' has no unambiguous
    # claim on it, and a master that moves on its own during a reload is the
    # kind of surprise invariant 10 exists to prevent. It keeps its value.
    eng.levels, eng.scales = levels, scales

    eng.dirty = True
    if dropped:
        message += f" (dropped active: {', '.join(dropped)})"
    if unbound:
        mod = surface_module()
        message += (" (faders dropped: " + ", ".join(
            mod.describe_control(("fader", number)) +
            (f"+{mod.LAYER_NAMES[lay]}" if lay else "")
            for number, lay in unbound) + ")")
    return True, message


def main():
    args = sys.argv[1:]
    check_only = "--check" in args
    no_midi = "--no-midi" in args
    no_dmx = "--no-dmx" in args
    use_sim = "--sim" in args
    log_beats = "--beats" in args

    os2l_port = None
    if "--os2l" in args:
        index = args.index("--os2l")
        nxt = args[index + 1] if index + 1 < len(args) else None
        try:
            os2l_port = int(nxt) if nxt and not nxt.startswith("-") else 0
        except ValueError:
            sys.exit("--os2l takes an optional port number")

    global _SURFACE
    if "--surface" in args:
        index = args.index("--surface")
        if index + 1 >= len(args) or args[index + 1].startswith("-"):
            sys.exit("--surface needs a name: " + ", ".join(sorted(SURFACES)))
        wanted = args[index + 1]
        if wanted not in SURFACES:
            # Loud, not clever. Guessing the device from whatever happens to
            # be plugged in would pick the wrong one exactly when it matters:
            # a mapping loaded against the wrong surface binds nothing, and a
            # surface that binds nothing looks like broken MIDI.
            sys.exit(f"unknown surface '{wanted}'. Options: "
                     + ", ".join(sorted(SURFACES)))
        _SURFACE = wanted
    elif use_sim:
        _SURFACE = "apcsim"
    vocab = surface_vocab()

    monitor_spec = None
    if "--monitor" in args:
        index = args.index("--monitor")
        nxt = args[index + 1] if index + 1 < len(args) else None
        monitor_spec = nxt if nxt and not nxt.startswith("-") else ""
    watching = "--watch" in args

    style = "intensity"
    if "--feedback" in args:
        index = args.index("--feedback")
        if index + 1 >= len(args):
            sys.exit("--feedback needs a value: rgb, intensity, pulse, "
                     "blink or fast-blink")
        style = args[index + 1]
        if style != "rgb" and style not in vocab.FEEDBACK:
            # The options come from the surface, because they differ: the
            # X-Touch's lamps are binary, so offering it 'pulse' would be a
            # lie that only showed up as a pad that never animated.
            sys.exit(f"unknown feedback style '{style}'. Options: "
                     + ", ".join(vocab.FEEDBACK))

    show = showfile.Show(SHOW_DIR, surface=vocab)
    try:
        warnings = show.load()
    except (OSError, ValueError, KeyError) as exc:
        sys.exit(f"Show file error: {exc}")
    for warning in warnings:
        print(f"warning: {warning}")

    eng = engine_mod.Engine(show.patch, show.scenes, show.chasers)

    print(f"\n{len(show.patch)} fixtures, {len(show.scenes)} scenes, "
          f"{len(show.chasers)} chasers, {len(show.bindings)} bindings")
    print(f"mapping: {show.mapping_path or 'NOT FOUND'}")
    synced = [n for n, c in show.chasers.items() if c.beat_synced]
    if synced:
        tempo_pads = [n for (n, _), b in show.faders.items()
                      if b.kind == "bpm"]
        has_tap = any(b.kind == "tap" for b in show.bindings.values())
        sources = []
        if os2l_port is not None:
            sources.append("VirtualDJ")
        if tempo_pads:
            where = vocab.describe_control(("fader", tempo_pads[0]))
            sources.append(f"bpm fader {where}")
        if has_tap:
            sources.append("tap pad")
        print(f"beat-synced chasers: {', '.join(synced)}")
        print(f"  tempo from: {', '.join(sources) or 'NOTHING -- they will hold'}")
    if not show.bindings:
        wanted = " or ".join(vocab.MAPPING_NAMES)
        print("\n  *** NO BINDINGS LOADED -- every press will do nothing.")
        print(f"  *** {wanted} must be in {SHOW_DIR}/ or beside "
              f"controller.py.\n")
    for problem in show.patch.conflicts():
        print(f"PATCH PROBLEM: {problem}")

    if check_only:
        width = max((len(vocab.LAYER_NAMES[i]) + 1
                     for i in range(1, vocab.LAYERS)), default=0)
        for (control, layer), b in sorted(show.bindings.items()):
            where = vocab.describe_control(control)
            prefix = f"{vocab.LAYER_NAMES[layer]}+" if layer else ""
            # Colour only where a lamp can show one. Printing 'white' next
            # to every binding on a surface with binary LEDs suggests a
            # setting that does nothing.
            colour = f" {b.colour}" if vocab.PADS else ""
            print(f"  {prefix.upper():<{width}}{where:<8} {b.mode:<7} "
                  f"{b.kind:<7} {b.target:<14}{colour}")
        print("\nCSVs parsed. No hardware touched.")
        return

    # After the --check return, so validating the CSVs needs nothing but the
    # standard library -- the same reason it touches no hardware.
    import dmx

    if no_dmx:
        sender = dmx.NullSender()
        print("DMX: dry run, no adapter used")
    else:
        port, problem = dmx.check_adapter()
        if problem:
            sys.exit(f"ADAPTER: {problem}")
        print(f"DMX on {port}")
        try:
            # Pass the port preflight just found. Without it DmxSender globs
            # again, and a device appearing or vanishing in between would
            # open a different adapter than the one that was checked.
            sender = dmx.DmxSender(port=port,
                                   on_status=lambda m: print(f"  [{m}]"))
        except dmx.AdapterError as exc:
            sys.exit(str(exc))

    import tempo
    internal = tempo.InternalClock(
        on_status=lambda m: print(f"  [clock: {m}]"))

    clock = None
    if os2l_port is not None:
        import os2l
        clock = os2l.BeatClock(
            port=os2l_port or os2l.DEFAULT_PORT,
            on_status=lambda m: print(f"  [os2l: {m}]"))
        clock.start()

    publisher = None
    if monitor_spec is not None:
        import monitor
        try:
            publisher = monitor.Publisher(monitor.parse_addr(monitor_spec))
        except ValueError as exc:
            sys.exit(f"--monitor: {exc}")
        print(f"Monitor: publishing to {publisher.addr[0]}:{publisher.addr[1]}"
              f"  (run: python3 dmxmon.py)")

    surface = None
    if not no_midi:
        apc_mod = surface_module()
        try:
            surface = apc_mod.Surface()
            print(f"surface in  {surface.input_name}")
            print(f"surface out {surface.output_name}")
        except Exception as exc:
            sys.exit(f"{_SURFACE}: {exc}")

        # Nothing tells the host where the physical faders are sitting, so
        # without this the master sits at whatever the software assumed --
        # typically full -- until you happen to touch it. The Introduction
        # message asks the device for all nine positions.
        faders = surface.introduce()
        if faders:
            # Every bound fader, not only master. A level fader physically
            # at full would otherwise read as 0 until you happened to move
            # it -- the same startup blindness the Introduction message
            # exists to cure, just one fader wider.
            startup = {"master_pending": None, "bpm_pending": None,
                       "internal": internal}
            for index, position in enumerate(faders):
                apply_fader(index + 1, 0, position, show, eng, startup)
            print(f"Faders: {faders}")
            for key in sorted(show.faders):
                binding = show.faders[key]
                detail = (f" ({binding.target})" if binding.target else "")
                where = vocab.describe_control(("fader", key[0]))
                prefix = (f"{vocab.LAYER_NAMES[key[1]]}+" if key[1] else "")
                print(f"  {prefix}{where} {binding.kind}{detail}")
        else:
            # Fail safe, not loud: an unexpected blackout is recoverable in
            # one gesture, an unexpected full blast is not.
            eng.set_master(0)
            print("Surface did not report where its faders are sitting.")
            print("  Master starts at 0 -- move the master control to sync"
                  " it.")

    stop = threading.Event()
    with sender:
        threading.Thread(target=sender.run_until, args=(stop,),
                         daemon=True).start()

        reload_requested = threading.Event()

        if surface:
            build_leds(surface, show, eng, style, vocab.LAYER_AT_START)
            print(f"\nFeedback: {style}. {vocab.LAYER_HINT}")
            print("Press pads. Ctrl-C to black out and exit.\n")
        else:
            print("\nNo MIDI. Ctrl-C to exit.\n")

        state = {"master_pending": None, "layer": vocab.LAYER_AT_START,
                 "held": {}, "relayout": False, "flash_until": 0.0,
                 "publish_at": 0.0, "music": None,
                 "clock_source": None, "bpm_pending": None,
                 "internal": internal}

        def flash_pad(note, colour_name):
            """Feedback on the reload control itself.

            You are looking at the APC when you press it, not the terminal,
            so success and failure have to be visible on the surface.

            Grid pads get real colour. Track and scene-launch buttons are
            single-colour in hardware -- red and green respectively, not
            changeable -- so there the difference has to be motion: steady
            for success, blinking for failure. Failure also holds four times
            longer, because a missed success costs nothing and a missed
            failure means playing a set on stale scenes.
            """
            if not surface:
                return
            apc_mod = surface_module()
            import colours
            ok = colour_name == "green"
            if note in apc_mod.GRID:
                surface.pad(note, colours.palette(colour_name),
                            apc_mod.SOLID_100)
            else:
                surface.button(note, 1 if ok else 2)
            state["flash_until"] = time.monotonic() + (0.7 if ok else 2.5)

        def do_reload(note=None):
            """The one place a reload happens, and it is on the main loop.

            note is the pad that asked, or None when the watcher did. Both
            triggers land here so the two paths cannot drift: the watcher
            used to have its own copy that skipped the warnings and the LED
            cache drop, which meant a watched edit could leave pads lit for
            bindings that no longer existed.
            """
            ok, message = apply_reload(show, eng)
            print(f"  {'reloaded' if ok else 'RELOAD FAILED'}: {message}")
            if not ok:
                # No warnings on this branch: a failed parse leaves
                # show.warnings describing the show that is still running,
                # and printing them under an error reads as if the error
                # produced them.
                print("    (running show kept unchanged)")
            else:
                for warning in show.warnings:
                    print(f"    warning: {warning}")
            if ok and surface:
                # Bindings may have changed wholesale, and the LED cache
                # describes the old layout. Drop it so the next paint
                # re-sends every pad rather than diffing against stale state.
                surface.refresh()
            if note is not None:
                flash_pad(note, "green" if ok else "red")

        actions = {"reload": do_reload}
        if watching:
            threading.Thread(target=watch_files, daemon=True,
                             args=(show, stop, reload_requested)).start()
            print("Watching show/ for changes.")

        try:
            while True:
                if surface:
                    for event in surface.poll():
                        handle(event, show, eng,
                               lambda m: print(f"  {m}"), state, actions)

                # The watcher only raises the flag; the reload itself belongs
                # here, between input and output, where nothing is mid-
                # iteration. Clear before reloading: a save that lands during
                # the parse then sets it again and gets its own reload rather
                # than being swallowed.
                if reload_requested.is_set():
                    reload_requested.clear()
                    print("\n  [show files changed]")
                    do_reload()

                now = time.monotonic()
                if state["flash_until"] and now > state["flash_until"]:
                    state["flash_until"] = 0.0
                    state["relayout"] = True

                if state["relayout"] and surface:
                    state["relayout"] = False
                    build_leds(surface, show, eng, style, state["layer"])

                # One clock at a time. VirtualDJ wins whenever it is
                # actually delivering beats; the internal clock covers the
                # gap and is silent until given a tempo. Whichever wins,
                # the engine sees identical Beat objects and cannot tell.
                beats = []
                live = clock is not None and clock.alive
                if clock is not None:
                    beats = clock.poll()
                if internal is not None:
                    fallback = internal.poll()
                    if not live:
                        beats.extend(fallback)

                using = ("virtualdj" if live else
                         ("internal" if internal and internal.armed else None))
                if using != state["clock_source"]:
                    state["clock_source"] = using
                    if using == "internal":
                        print(f"  [clock: internal, {internal.bpm} bpm "
                              f"via {internal.source}]")
                    elif using == "virtualdj":
                        print("  [clock: VirtualDJ]")
                    else:
                        print("  [clock: none -- beat chasers holding]")

                for beat in beats:
                    eng.on_beat(beat)
                    if log_beats:
                        print(f"  beat {beat.pos} {beat.bpm:g}bpm"
                              + ("  bar" if beat.is_bar else ""))
                if clock is not None:
                    for message in clock.poll_messages():
                        if log_beats:
                            print(f"  os2l {message}")

                pending = state.get("bpm_pending")
                if pending and time.monotonic() - pending > 0.3:
                    state["bpm_pending"] = None
                    if internal is not None:
                        print(f"  bpm {internal.bpm}")

                # Fire any due step timers before deciding what to output.
                # Cheap: one comparison per running chaser.
                eng.tick()

                pending = state.get("master_pending")
                if pending and time.monotonic() - pending > 0.25:
                    print(f"  master {eng.master}")
                    state["master_pending"] = None
                if eng.dirty:
                    sender.apply(eng.output())
                    if surface and not state["flash_until"]:
                        build_leds(surface, show, eng, style, state["layer"])

                if publisher is not None:
                    # Published on a timer rather than on change, so the
                    # viewer keeps a live frame rate and can tell "holding
                    # steady" apart from "controller died".
                    now = time.monotonic()
                    if now >= state["publish_at"]:
                        state["publish_at"] = now + 1.0 / 30
                        publisher.send(sender.snapshot())
                time.sleep(TICK_S)
        except KeyboardInterrupt:
            pass
        finally:
            print("\nBlacking out...")
            stop.set()
            if clock is not None:
                clock.stop()
            if publisher is not None:
                publisher.close()
            if surface:
                surface.close()


if __name__ == "__main__":
    main()
