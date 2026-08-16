#!/usr/bin/env python3
"""Load a show from CSV and put one scene on the rig.

    pip install pyserial

    python3 play_scene.py                    # list the rig and its scenes
    python3 play_scene.py warm_wash          # hold that scene
    python3 play_scene.py -i                 # interactive: switch and reload
    python3 play_scene.py -i --watch         # auto-reload on file change
    python3 play_scene.py --check            # validate CSVs, no hardware

Bypassing the show files entirely -- use these when output stops and you need
to know whether the problem is the hardware or the CSVs:

    python3 play_scene.py --raw 65=50,67=11  # drive raw channels directly
    python3 play_scene.py --sweep 65-79      # find what each channel does
    python3 play_scene.py --sweep 66-71 --raw 65=255   # ...with dimmer up

Interactive commands: a scene name, or list / reload / watch / off / quit.

Architecture worth noting, because everything later depends on it: a daemon
thread does nothing but push frames at a fixed rate, forever. The main thread
only mutates state. When MIDI arrives it slots into the main-thread role and
nothing about the output path changes.
"""

import sys
import threading
import time

import dmx
import showfile

SHOW_DIR = "show"


def show_rig(show):
    print("Profiles:")
    for name, profile in sorted(show.profiles.items()):
        features = ", ".join(
            f"{f.name}{'*' if f.mode == showfile.SNAP else ''}"
            for f in sorted(profile.features.values(), key=lambda f: f.offset))
        print(f"  {name:<14} {profile.footprint}ch   {features}")
    print("  (* = snap channel: selector, must not fade)\n")

    print("Patch:")
    for name, fixture in sorted(show.patch.fixtures.items()):
        end = fixture.address + fixture.profile.footprint - 1
        print(f"  {name:<8} {fixture.profile.name:<14} "
              f"{fixture.address:>3}-{end:<3}")

    problems = show.patch.conflicts()
    if problems:
        print("\n  PATCH PROBLEMS:")
        for problem in problems:
            print(f"    {problem}")
    print()


def show_scenes(show, verbose=False):
    print(f"Scenes ({len(show.scenes)}):")
    for name, scene in show.scenes.items():
        print(f"  {name:<16} {len(scene):>3} channels")
        if verbose:
            for ch, val in sorted(scene.levels.items()):
                flag = ("" if show.patch.mode(ch) == showfile.FADE
                        else "   <- snap")
                print(f"      {show.patch.label(ch):<28} {val:>3}{flag}")
    print()


def describe(scene, patch, limit=6):
    parts = [f"{patch.label(ch)}={val}"
             for ch, val in sorted(scene.levels.items())]
    if len(parts) > limit:
        return ", ".join(parts[:limit]) + f", +{len(parts) - limit} more"
    return ", ".join(parts)


def parse_raw(spec):
    """'65=50,67=11' -> {65: 50, 67: 11}"""
    levels = {}
    for part in spec.split(","):
        channel, sep, value = part.partition("=")
        if not sep:
            raise ValueError(f"expected channel=value, got '{part}'")
        channel, value = int(channel), int(value)
        if not 1 <= channel <= 512:
            raise ValueError(f"channel {channel} out of range 1-512")
        if not 0 <= value <= 255:
            raise ValueError(f"value {value} out of range 0-255")
        levels[channel] = value
    return levels


def parse_range(spec):
    """'65-79' or '65' -> (65, 79) / (65, 65)"""
    if "-" in spec:
        start, end = spec.split("-", 1)
        return int(start), int(end)
    return int(spec), int(spec)


def sweep(sender, start, end, base):
    """Walk one channel at a time to full, to identify what it controls.

    This is how you write a real profiles.csv for a fixture whose chart you
    do not have. `base` stays held throughout -- most fixtures show nothing
    at all unless their dimmer is up, so try --raw 65=255 if a sweep looks
    completely dead.
    """
    if base:
        print(f"Holding {', '.join(f'{c}={v}' for c, v in sorted(base.items()))}"
              f" throughout.\n")
    for channel in range(start, end + 1):
        sender.apply({**base, channel: 255})
        print(f"  channel {channel:>3} = 255", end="")
        try:
            input("   -- what changed? Enter for next, Ctrl-C to stop --")
        except EOFError:
            time.sleep(1.0)
            print()
    sender.apply(base)
    print("\nSweep done. Write what you saw into show/profiles.csv as "
          "offsets from the fixture address.")


def preflight():
    """Report adapter status without opening it for real."""
    port, problem = dmx.check_adapter()
    if problem:
        print(f"ADAPTER: {problem}")
        return None
    print(f"ADAPTER: ok, {port}")
    return port


def interactive(show, sender, watching):
    """Scene selection loop with live CSV reloading."""
    active = {"name": None}

    def activate(name):
        scene = show.scenes[name]
        sender.apply(scene.levels)
        active["name"] = name
        print(f"  -> {name}: {describe(scene, show.patch)}")

    def do_reload():
        ok, message, (added, removed, changed) = show.reload()
        if not ok:
            # The running show is untouched -- that is the point.
            print(f"  RELOAD FAILED, keeping current show:\n    {message}")
            return
        print(f"  reloaded: {message}")
        for warning in show.warnings:
            print(f"    warning: {warning}")
        for problem in show.patch.conflicts():
            print(f"    PATCH: {problem}")

        name = active["name"]
        if name is None:
            return
        if name in show.scenes:
            if name in changed:
                activate(name)          # re-apply so the edit is visible now
            else:
                print(f"  '{name}' still active, unchanged")
        else:
            print(f"  '{name}' no longer exists -- output held, 'off' to clear")
            active["name"] = None

    def watcher(stop):
        while not stop.wait(0.5):
            if show.changed_on_disk():
                print("\n  [file changed]")
                do_reload()
                print("scene> ", end="", flush=True)

    watch_stop = threading.Event()
    if watching:
        threading.Thread(target=watcher, args=(watch_stop,), daemon=True).start()
        print("Watching show/ for changes.")

    print("\nCommands: <scene name> | list | reload | watch | off | quit")
    print("          raw 65=50,67=11   (bypass the show files)\n")
    try:
        while True:
            entry = input("scene> ").strip()
            if not entry:
                continue
            if entry in ("quit", "exit"):
                break
            if entry == "list":
                print("  " + ", ".join(show.scenes))
            elif entry == "reload":
                do_reload()
            elif entry == "watch":
                if watch_stop.is_set() or not watching:
                    watch_stop.clear()
                    threading.Thread(target=watcher, args=(watch_stop,),
                                     daemon=True).start()
                    watching = True
                    print("  watching show/ for changes")
                else:
                    watch_stop.set()
                    watching = False
                    print("  stopped watching")
            elif entry.startswith("raw "):
                try:
                    levels = parse_raw(entry[4:].strip())
                except ValueError as exc:
                    print(f"  {exc}")
                    continue
                sender.apply(levels)
                active["name"] = None
                print("  -> raw " + ", ".join(f"ch{c}={v}"
                                              for c, v in sorted(levels.items())))
            elif entry == "off":
                sender.apply({})
                active["name"] = None
                print("  -> blackout")
            elif entry in show.scenes:
                activate(entry)
            else:
                print(f"  no scene '{entry}' -- try 'list'")
    finally:
        watch_stop.set()


def _option(args, name):
    """Pull '--name value' out of the argument list."""
    if name not in args:
        return None
    index = args.index(name)
    if index + 1 >= len(args):
        sys.exit(f"{name} needs a value")
    value = args[index + 1]
    del args[index:index + 2]
    return value


def diagnostics(raw_spec, sweep_spec):
    """Drive channels directly, ignoring the show files completely."""
    try:
        base = parse_raw(raw_spec) if raw_spec else {}
        span = parse_range(sweep_spec) if sweep_spec else None
    except ValueError as exc:
        sys.exit(f"Bad argument: {exc}")

    if preflight() is None:
        sys.exit("Cannot output without an adapter.")

    stop = threading.Event()
    try:
        sender = dmx.DmxSender(on_status=lambda m: print(f"\n  [{m}]"))
    except dmx.AdapterError as exc:
        sys.exit(str(exc))

    with sender:
        threading.Thread(target=sender.run_until, args=(stop,),
                         daemon=True).start()
        try:
            if span:
                sweep(sender, span[0], span[1], base)
                stop.wait()
            else:
                sender.apply(base)
                print("  -> " + ", ".join(f"ch{c}={v}"
                                          for c, v in sorted(base.items())))
                print("\nHolding raw channels. Ctrl-C to black out and exit.")
                stop.wait()
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            print("\nBlacking out...")
            stop.set()


def main():
    args = sys.argv[1:]
    raw_spec = _option(args, "--raw")
    sweep_spec = _option(args, "--sweep")
    check_only = "--check" in args
    watching = "--watch" in args
    is_interactive = "-i" in args or "--interactive" in args
    args = [a for a in args if not a.startswith("-")]

    # Raw and sweep bypass the show files on purpose -- that is what makes
    # them useful when the CSVs are the thing you suspect.
    if raw_spec or sweep_spec:
        diagnostics(raw_spec, sweep_spec)
        return

    show = showfile.Show(SHOW_DIR)
    try:
        warnings = show.load()
    except (OSError, ValueError, KeyError) as exc:
        sys.exit(f"Show file error: {exc}")
    for warning in warnings:
        print(f"warning: {warning}")

    if check_only or (not args and not is_interactive):
        show_rig(show)
        show_scenes(show, verbose=check_only)
        preflight()
        if not check_only:
            print("\nPass a scene name to output it, or -i to switch live.")
        return

    if args and args[0] not in show.scenes:
        sys.exit(f"No scene called '{args[0]}'. "
                 f"Known: {', '.join(show.scenes)}")

    if preflight() is None:
        sys.exit("Cannot output without an adapter. "
                 "Use --check to validate the CSVs meanwhile.")

    for problem in show.patch.conflicts():
        print(f"WARNING: {problem}")

    stop = threading.Event()
    try:
        sender = dmx.DmxSender(on_status=lambda m: print(f"\n  [{m}]"))
    except dmx.AdapterError as exc:
        sys.exit(str(exc))

    with sender:
        transmitter = threading.Thread(
            target=sender.run_until, args=(stop,), daemon=True)
        transmitter.start()

        try:
            if args:
                scene = show.scenes[args[0]]
                sender.apply(scene.levels)
                print(f"  -> {args[0]}: {describe(scene, show.patch)}")

            if is_interactive:
                interactive(show, sender, watching)
            else:
                print("\nHolding. Ctrl-C to black out and exit.")
                stop.wait()
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            print("\nBlacking out...")
            stop.set()
            transmitter.join(timeout=1.0)


if __name__ == "__main__":
    main()
