#!/usr/bin/env python3
"""Drive chasers from Virtual DJ's beat clock. Standalone -- no hardware.

    python3 os2l_drive.py beatwalk              # print what the engine does
    python3 os2l_drive.py beatwalk --monitor    # + feed dmxmon.py
    python3 os2l_drive.py --list                # which chasers are beat-synced

Then in another terminal, optionally:
    python3 dmxmon.py

Nothing here touches controller.py. This is the same staging as the DMX and
MIDI work: prove the piece against real input before wiring it in.

REMEMBER: VirtualDJ will not connect until you press a DMX pad in it once.
"""

import argparse
import sys
import threading
import time

import dmx
import engine as engine_mod
import os2l
import showfile

SHOW_DIR = "show"

BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"


def list_chasers(show):
    print(f"\n{len(show.chasers)} chasers:\n")
    for name, chaser in show.chasers.items():
        if chaser.beat_synced:
            kind = f"{BOLD}beat-synced{RESET}, {chaser.cycle_beats}-beat cycle"
        else:
            kind = f"{DIM}timers/manual{RESET}"
        print(f"  {name:<16} {len(chaser)} steps   {kind}")
        for i, step in enumerate(chaser.steps):
            timing = (f"{step.beats} beat(s)" if step.beats
                      else (f"{step.duration_ms}ms" if step.duration_ms
                            else "hold"))
            print(f"      {i + 1}. {step.scene:<16} {timing}")
    print(f"\n{DIM}Only fully beat-synced chasers follow the music. See "
          f"show/chasers.csv.{RESET}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("chaser", nargs="?", help="chaser to run")
    parser.add_argument("--port", type=int, default=os2l.DEFAULT_PORT)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--monitor", action="store_true",
                        help="publish frames so dmxmon.py can show them")
    args = parser.parse_args()

    show = showfile.Show(SHOW_DIR)
    try:
        for warning in show.load():
            if "no pad" not in warning:
                print(f"warning: {warning}")
    except (OSError, ValueError, KeyError) as exc:
        sys.exit(f"Show file error: {exc}")

    if args.list or not args.chaser:
        list_chasers(show)
        return

    if args.chaser not in show.chasers:
        sys.exit(f"No chaser '{args.chaser}'. Known: "
                 f"{', '.join(show.chasers) or 'none'}")

    chaser = show.chasers[args.chaser]
    if not chaser.beat_synced:
        print(f"{BOLD}'{args.chaser}' is not beat-synced{RESET} -- every step "
              f"needs a beats value in chasers.csv.")
        print("It will run, but on its timers, ignoring the music.\n")

    eng = engine_mod.Engine(show.patch, show.scenes, show.chasers)
    eng.start_chaser(args.chaser)

    sender = dmx.NullSender()
    publisher = None
    if args.monitor:
        import monitor
        publisher = monitor.Publisher()
        print(f"Publishing frames to {publisher.addr[0]}:{publisher.addr[1]} "
              f"-- run: python3 dmxmon.py")

    clock = os2l.BeatClock(port=args.port,
                           on_status=lambda m: print(f"  {DIM}[{m}]{RESET}"))
    clock.start()

    print(f"\nRunning {BOLD}{args.chaser}{RESET}: {len(chaser)} steps over "
          f"{chaser.cycle_beats} beats")
    print("Ctrl-C to stop.\n")

    last_index = None
    was_alive = None
    publish_at = 0.0

    try:
        while True:
            for message in clock.poll_messages():
                print(f"  {DIM}{message}{RESET}")

            for beat in clock.poll():
                eng.on_beat(beat)
                index, total = eng.chaser_position(args.chaser)
                scene = chaser.steps[index - 1].scene

                marker = ("PHRASE" if beat.is_phrase
                          else ("bar" if beat.is_bar else ""))
                cells = "".join("X" if i == beat.in_bar else "."
                                for i in range(4))
                moved = index != last_index
                last_index = index

                bits = [f"pos {beat.pos:>5}", f"[{cells}]", f"{marker:<6}",
                        f"{beat.bpm:g}bpm"]
                if beat.strength is not None:
                    bits.append(f"str {beat.strength:.1f}")
                if beat.change:
                    bits.append(f"{BOLD}CHANGE{RESET}")
                if not beat.audible:
                    bits.append(f"{DIM}silent{RESET}")
                step = (f"{BOLD}-> step {index}/{total} {scene}{RESET}"
                        if moved else f"{DIM}   step {index}/{total}{RESET}")
                print(f"  {'  '.join(bits)}  {step}")

            eng.tick()
            if eng.dirty:
                sender.apply(eng.output())

            # Report the transport changing state, since a stalled clock
            # looks identical to a chaser that simply is not moving.
            alive = clock.alive
            if alive != was_alive:
                was_alive = alive
                if clock.connected:
                    print(f"  {DIM}[music {'playing' if alive else 'stopped'}"
                          f"{'' if alive else ' -- chaser holding'}]{RESET}")

            if publisher is not None:
                now = time.monotonic()
                if now >= publish_at:
                    publish_at = now + 1 / 30
                    publisher.send(sender.snapshot())

            time.sleep(0.005)
    except KeyboardInterrupt:
        print(f"\n\nbeats received: {clock.total_beats}")
    finally:
        clock.stop()
        if publisher:
            publisher.close()


if __name__ == "__main__":
    main()
