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

Tempo fallback: bind a fader to 'bpm' and a pad to 'tap' in mapping.csv.
The internal clock stays silent until you give it a tempo, and yields to
VirtualDJ whenever VirtualDJ is actually delivering beats.
    python3 controller.py --check       # validate CSVs, touch no hardware
    python3 controller.py --feedback pulse   # active pads pulse instead
    python3 controller.py --feedback blink   # active pads blink instead
    python3 controller.py --feedback rgb     # EXPERIMENTAL, SysEx 24-bit

--feedback is a controller.py flag only. apc_leds.py does not accept it.

Three threads, each with exactly one job:

    DMX thread    pushes a frame every 33ms, forever. Never blocks on input.
    main loop     polls MIDI, updates the engine, refreshes LEDs.
    watcher       optional, reloads the CSVs when they change on disk.

Input events never write to the port directly -- they mutate engine state,
and the DMX thread transmits whatever it finds. That separation is what keeps
the output steady when the Mac is busy.
"""

import sys
import threading
import time

import dmx
import engine as engine_mod
import showfile

SHOW_DIR = "show"

# Which module provides the control surface: apc (real hardware) or
# virtualapc (apcsim.py over UDP). Resolved once in main(); every other
# call site goes through surface_module() and never learns which it got.
_SURFACE_MODULE = None


def surface_module():
    global _SURFACE_MODULE
    if _SURFACE_MODULE is None:
        import apc
        _SURFACE_MODULE = apc
    return _SURFACE_MODULE
TICK_S = 0.005          # 200Hz input poll -- well under human perception


def build_leds(apc, show, eng, style="intensity", shift=False):
    """Paint the surface to match engine state.

    Same colour for idle and active; only the brightness changes. That keeps
    the layout readable at a glance -- you learn where red lives, and its
    brightness tells you whether it is running.

    'intensity' is the default: the fixed palette, 10%% when idle and 100%%
    when active. 'rgb' uses SysEx for exact 24-bit colours, but SysEx pad
    colouring is unverified on this unit -- treat it as experimental.
    """
    apc_mod = surface_module()
    import colours

    visible = show.layer(shift)

    def is_on(binding):
        # Chasers light exactly like scenes -- is_active() covers both, so
        # the LED layer never has to care which kind a pad points at.
        return (binding.kind in ("scene", "chaser")
                and eng.is_active(binding.target))

    if style == "rgb":
        # Whole grid in two SysEx messages: blanket off, then bound pads.
        apc.pads_rgb([(0, 63, (0, 0, 0))])
        entries = []
        for note, binding in visible.items():
            if note not in apc_mod.GRID:
                continue
            scale = 1.0 if is_on(binding) else colours.IDLE_SCALE
            entries.append((note, note, colours.rgb(binding.colour, scale)))
        apc.pads_rgb(entries)
    else:
        active_behaviour = apc_mod.FEEDBACK[style]
        for note in apc_mod.GRID:
            binding = visible.get(note)
            if binding is None:
                apc.pad(note, apc_mod.OFF)
            else:
                index = colours.palette(binding.colour)
                apc.pad(note, index,
                        active_behaviour if is_on(binding) else apc_mod.IDLE)

    for note in list(apc_mod.TRACK_BUTTONS) + list(apc_mod.SCENE_BUTTONS):
        apc.button(note, 1 if visible.get(note) else 0)


def apply_fader(number, value, show, eng, state):
    """Route one fader position. Shared by live moves and the startup sync.

    Which fader does what comes from mapping.csv. "Master" was never
    hardware behaviour -- CC 0x38 is an ordinary fader -- so there was no
    reason for it to be the one hardwired choice.
    """
    binding = show.faders.get(number)
    if binding is None:
        if 9 not in show.faders and number == 9:
            eng.set_master(round(value / 127 * 255))     # legacy default
            state["master_pending"] = time.monotonic()
        return

    if binding.kind == "master":
        eng.set_master(round(value / 127 * 255))
        # A sweep is ~127 messages. Logging each one buries everything
        # else, so defer and print once the move settles.
        state["master_pending"] = time.monotonic()
    elif binding.kind == "level":
        eng.set_level(number, binding.channels, round(value / 127 * 255))
    elif binding.kind == "scale":
        eng.set_scale(number, binding.channels, round(value / 127 * 255))
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
        apply_fader(number, value, show, eng, state)
        return

    apc_mod = surface_module()
    note = event[1]

    if note == apc_mod.SHIFT:
        # SHIFT has no LED, so the grid repainting IS the feedback.
        state["shift"] = (kind == "press")
        state["relayout"] = True
        return

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

    binding = show.binding_for(note, state["shift"])
    if binding is None:
        # Unmapped presses are logged deliberately. Silence here is what made
        # a missing mapping.csv look like broken MIDI.
        row, col = divmod(note, 8)
        where = f"r{row}c{col}" if note < 64 else f"note {note}"
        layer = "shift+" if state["shift"] else ""
        log(f"{layer}{where} pressed -- no binding in mapping.csv")
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


def apply_reload(show, eng):
    """Reload the CSVs and reconcile engine state. Returns (ok, message).

    Atomic: if any file fails to parse the running show is untouched, so a
    typo mid-set costs you nothing. Active scenes survive if they still
    exist; ones that vanished are dropped.
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

    eng.dirty = True
    if dropped:
        message += f" (dropped active: {', '.join(dropped)})"
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

    if use_sim:
        global _SURFACE_MODULE
        import virtualapc
        _SURFACE_MODULE = virtualapc

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
        _apc_check = surface_module()
        if style != "rgb" and style not in _apc_check.FEEDBACK:
            sys.exit(f"unknown feedback style '{style}'. Options: rgb, "
                     + ", ".join(_apc_check.FEEDBACK))

    show = showfile.Show(SHOW_DIR)
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
        tempo_pads = [k for k, b in show.faders.items() if b.kind == "bpm"]
        has_tap = any(b.kind == "tap" for b in show.bindings.values())
        sources = []
        if os2l_port is not None:
            sources.append("VirtualDJ")
        if tempo_pads:
            sources.append(f"bpm fader f{tempo_pads[0]}")
        if has_tap:
            sources.append("tap pad")
        print(f"beat-synced chasers: {', '.join(synced)}")
        print(f"  tempo from: {', '.join(sources) or 'NOTHING -- they will hold'}")
    if not show.bindings:
        print("\n  *** NO BINDINGS LOADED -- every pad press will do nothing.")
        print("  *** mapping.csv must be in show/ or beside controller.py.\n")
    for problem in show.patch.conflicts():
        print(f"PATCH PROBLEM: {problem}")

    if check_only:
        for (note, shift), b in sorted(show.bindings.items()):
            row, col = divmod(note, 8)
            where = (f"r{row}c{col}" if note < 64 else f"note {note}")
            layer = "SHIFT+" if shift else "      "
            print(f"  {layer}{where:<8} {b.mode:<7} {b.kind:<7} "
                  f"{b.target:<14} {b.colour}")
        print("\nCSVs parsed. No hardware touched.")
        return

    if no_dmx:
        sender = dmx.NullSender()
        print("DMX: dry run, no adapter used")
    else:
        port, problem = dmx.check_adapter()
        if problem:
            sys.exit(f"ADAPTER: {problem}")
        print(f"DMX on {port}")
        try:
            sender = dmx.DmxSender(on_status=lambda m: print(f"  [{m}]"))
        except dmx.AdapterError as exc:
            sys.exit(str(exc))

    import tempo
    internal = tempo.InternalClock()

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
            surface = apc_mod.APC()
            print(f"APC in  {surface.input_name}")
            print(f"APC out {surface.output_name}")
        except Exception as exc:
            sys.exit(f"APC: {exc}")

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
                apply_fader(index + 1, position, show, eng, startup)
            print(f"Faders: {faders}")
            for number in sorted(show.faders):
                binding = show.faders[number]
                detail = (f" ({binding.target})" if binding.target else "")
                print(f"  f{number} {binding.kind}{detail}")
        else:
            # Fail safe, not loud: an unexpected blackout is recoverable in
            # one gesture, an unexpected full blast is not.
            eng.set_master(0)
            print("Device did not answer the Introduction message.")
            print("  Master starts at 0 -- move fader 9 to sync it.")

    stop = threading.Event()
    with sender:
        threading.Thread(target=sender.run_until, args=(stop,),
                         daemon=True).start()

        def watcher():
            while not stop.wait(0.5):
                if show.changed_on_disk():
                    ok, message = apply_reload(show, eng)
                    if ok:
                        print(f"\n  reloaded: {message}")
                    else:
                        print(f"\n  RELOAD FAILED, keeping show:\n    {message}")

        if surface:
            build_leds(surface, show, eng, style, False)
            print(f"\nFeedback: {style}. Hold SHIFT for the second layer.")
            print("Press pads. Ctrl-C to black out and exit.\n")
        else:
            print("\nNo MIDI. Ctrl-C to exit.\n")

        state = {"master_pending": None, "shift": False,
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

        def reload_action(note):
            ok, message = apply_reload(show, eng)
            print(f"  {'reloaded' if ok else 'RELOAD FAILED'}: {message}")
            if not ok:
                print("    (running show kept unchanged)")
            for warning in show.warnings:
                print(f"    warning: {warning}")
            if ok and surface:
                # Bindings may have changed wholesale, and the LED cache
                # describes the old layout. Drop it so the next paint
                # re-sends every pad rather than diffing against stale state.
                surface.refresh()
            flash_pad(note, "green" if ok else "red")

        actions = {"reload": reload_action}
        if watching:
            threading.Thread(target=watcher, daemon=True).start()
            print("Watching show/ for changes.")

        try:
            while True:
                if surface:
                    for event in surface.poll():
                        handle(event, show, eng,
                               lambda m: print(f"  {m}"), state, actions)

                now = time.monotonic()
                if state["flash_until"] and now > state["flash_until"]:
                    state["flash_until"] = 0.0
                    state["relayout"] = True

                if state["relayout"] and surface:
                    state["relayout"] = False
                    build_leds(surface, show, eng, style, state["shift"])

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

                if True:
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
                        build_leds(surface, show, eng, style, state["shift"])

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
