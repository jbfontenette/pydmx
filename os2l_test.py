#!/usr/bin/env python3
"""OS2L listener. Watch what Virtual DJ actually sends, before wiring it in.

    python3 os2l_test.py                 # listen on 127.0.0.1:9996
    python3 os2l_test.py --port 7350
    python3 os2l_test.py --raw           # also print the raw JSON
    python3 os2l_test.py --echo          # LATCH buttons and light them back
    python3 os2l_test.py --blink NAME    # flash one button on/off every 2s

VIRTUAL DJ SETUP
    Settings -> type "os2l" in the search box
      os2l           set to "auto"
      os2lDirectIp   set to 127.0.0.1:9996
    Restart VirtualDJ. Play a track; beats should start arriving.

WHO CONNECTS TO WHOM
    We are the SERVER. VirtualDJ is the client and connects to us -- the
    opposite of the DMX and MIDI work, where we opened the device.

    The protocol's own discovery is DNS-SD ("_os2l._tcp"), which this script
    will advertise if the `zeroconf` package is installed. os2lDirectIp
    bypasses discovery entirely and is one setting, so start there and treat
    Bonjour as an optimisation.

MESSAGE FRAMING
    The spec defines the JSON objects but not how they are delimited on the
    stream. Rather than assume newlines, this decodes objects one at a time
    by boundary, which works whether or not they are separated.
"""

import argparse
import json
import socket
import sys
import time

DEFAULT_PORT = 9996

BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"


class Stream:
    """Accumulates bytes and yields whole JSON objects.

    json.JSONDecoder.raw_decode parses one value from the front of a string
    and reports where it stopped, which is exactly what is needed for a
    stream of concatenated objects with no agreed delimiter.
    """

    def __init__(self):
        self.buffer = ""
        self.decoder = json.JSONDecoder()

    def feed(self, chunk):
        self.buffer += chunk
        out = []
        while True:
            stripped = self.buffer.lstrip()
            if not stripped:
                self.buffer = ""
                break
            try:
                value, end = self.decoder.raw_decode(stripped)
            except ValueError:
                # Incomplete object: keep it and wait for more bytes.
                self.buffer = stripped
                break
            out.append(value)
            self.buffer = stripped[end:]
        return out


class Stats:
    def __init__(self):
        self.beats = 0
        self.buttons = 0
        self.commands = 0
        self.last_beat_at = None
        self.intervals = []
        self.bpms = set()
        self.last_pos = None
        self.gaps = 0
        self.pauses = []
        self.by_tempo = {}

    def beat(self, pos, bpm):
        now = time.monotonic()
        if self.last_beat_at is not None:
            gap = now - self.last_beat_at
            self.intervals.append(gap)
            expected = 60.0 / float(bpm) if bpm else None
            # Bucket by tempo. A session spanning two decks contains two
            # different correct intervals, and pooling them reports the
            # tempo change itself as jitter.
            if bpm:
                self.by_tempo.setdefault(round(float(bpm), 1), []).append(gap)
            # A pause shows up as one enormous interval. Counting it as
            # jitter made the spread statistic meaningless -- a 12s pause
            # reported as "wide spread, expect drift" on a clock that was
            # in fact steady to 1.5%.
            if expected and gap > expected * 2.5:
                self.pauses.append(gap)
        self.last_beat_at = now
        self.beats += 1
        if bpm:
            self.bpms.add(round(float(bpm), 1))
        if self.last_pos is not None and pos is not None:
            if pos != self.last_pos + 1:
                self.gaps += 1
        self.last_pos = pos

    def report(self):
        print(f"\n\n--- session ---")
        print(f"  beats {self.beats}   buttons {self.buttons}   "
              f"commands {self.commands}")
        if self.gaps:
            print(f"  non-consecutive pos jumps: {self.gaps} "
                  f"(normal when seeking or changing track)")
        if self.bpms:
            print(f"  bpm seen: {sorted(self.bpms)}")
        for tempo in sorted(self.by_tempo):
            ordered = sorted(self.by_tempo[tempo])
            if len(ordered) < 3:
                continue
            median = ordered[len(ordered) // 2]
            # Pauses excluded: one 12s stop is not jitter.
            steady = [i for i in ordered if i <= median * 2.5]
            if len(steady) < 3:
                continue
            spread = steady[-1] - steady[0]
            print(f"  {tempo} bpm: median {median * 1000:.0f}ms, "
                  f"spread {spread * 1000:.0f}ms ({spread / median:.1%}) "
                  f"over {len(steady)} beats -> {60 / median:.1f} bpm implied")
            if spread > median * 0.15:
                print(f"  {DIM}    wide spread -- expect some drift on fast "
                      f"chases{RESET}")

        if self.pauses:
            lengths = ", ".join(f"{p:.1f}s" for p in self.pauses)
            print(f"  clock stalled {len(self.pauses)}x: {lengths}")
            print(f"  {DIM}beats stop entirely while paused and resume with "
                  f"CHANGE -- a beat-synced chaser needs a watchdog{RESET}")


def describe_beat(msg, stats):
    pos = msg.get("pos")
    bpm = msg.get("bpm")
    change = msg.get("change")
    strength = msg.get("strength")

    interval = ""
    if stats.last_beat_at is not None:
        interval = f" {DIM}{(time.monotonic() - stats.last_beat_at) * 1000:.0f}ms{RESET}"
    stats.beat(pos, bpm)

    # pos % 4 == 0 is a bar line, pos % 16 == 0 starts a 16-beat phrase.
    marker = ""
    if isinstance(pos, int):
        slot = pos % 4
        cells = "".join("X" if i == slot else "." for i in range(4))
        marker = f"[{cells}]"
        if pos % 16 == 0:
            marker = f"{BOLD}{marker} PHRASE{RESET}"
        elif pos % 4 == 0:
            marker = f"{BOLD}{marker} bar{RESET}"

    bits = [f"pos {pos}", marker, f"{bpm} bpm" if bpm else ""]
    if change:
        bits.append(f"{BOLD}CHANGE{RESET}")
    if strength is not None:
        bits.append(f"str {strength}")
    return "beat  " + "  ".join(b for b in bits if b) + interval


def send_feedback(client, name, state, page=None):
    """The only message we ever send. Lights a button in the DJ software."""
    msg = {"evt": "feedback", "name": name, "state": state}
    if page:
        msg["page"] = page
    try:
        client.sendall(json.dumps(msg).encode() + b"\n")
        return True
    except OSError:
        return False


def serve(port, raw, echo, blink):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("0.0.0.0", port))
    except OSError as exc:
        sys.exit(f"Cannot listen on port {port}: {exc}\n"
                 "  Another OS2L listener may already be running.")
    server.listen(1)
    server.settimeout(0.5)

    advertise(port)
    print(f"Listening on 0.0.0.0:{port}")
    print(f"In VirtualDJ set os2lDirectIp to 127.0.0.1:{port}, "
          f"os2l to auto, then restart it.")
    print("Ctrl-C to stop.\n")

    stats = Stats()
    try:
        while True:
            try:
                client, addr = server.accept()
            except socket.timeout:
                continue
            print(f"{BOLD}connected: {addr[0]}:{addr[1]}{RESET}\n")
            handle_client(client, stats, raw, echo, blink)
            print(f"\n{DIM}disconnected -- waiting for VirtualDJ again{RESET}\n")
    except KeyboardInterrupt:
        pass
    finally:
        stats.report()
        server.close()


def handle_client(client, stats, raw, echo, blink):
    client.settimeout(0.5)
    stream = Stream()
    lit_at = {}
    pending_off = {}
    next_blink = 0.0
    blink_state = "off"
    try:
        while True:
            if blink:
                now = time.monotonic()
                if now >= next_blink:
                    next_blink = now + 2.0
                    blink_state = "on" if blink_state == "off" else "off"
                    ok = send_feedback(client, blink, blink_state)
                    print(f"{time.strftime('%H:%M:%S')}  "
                          f"{BOLD}feedback{RESET} -> name={blink!r} "
                          f"state={blink_state}"
                          + ("" if ok else "  (send failed)"))
            # Release a deferred "off" once its button has been lit long
            # enough to see.
            if pending_off:
                now = time.monotonic()
                for name in [n for n, due in pending_off.items() if now >= due]:
                    del pending_off[name]
                    send_feedback(client, name, "off")
                    print(f"{time.strftime('%H:%M:%S')}  "
                          f"{DIM}feedback off (held) name={name!r}{RESET}")

            try:
                chunk = client.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                return
            for msg in stream.feed(chunk.decode("utf-8", errors="replace")):
                show(client, msg, stats, raw, echo, lit_at, pending_off)
    except (OSError, KeyboardInterrupt):
        raise
    finally:
        client.close()


MIN_HOLD_S = 1.2


def show(client, msg, stats, raw, echo, lit_at=None, pending_off=None):
    stamp = time.strftime("%H:%M:%S")
    event = msg.get("evt")

    if event == "beat":
        line = describe_beat(msg, stats)
    elif event == "btn":
        stats.buttons += 1
        page = f" page={msg.get('page')}" if msg.get("page") else ""
        line = (f"{BOLD}btn{RESET}   name={msg.get('name')!r} "
                f"state={msg.get('state')}{page}")
        if echo:
            # Mirror the state, but never unlight a button sooner than
            # MIN_HOLD_S after lighting it.
            #
            # VirtualDJ pads come in both flavours: some are momentary and
            # send on/off in the same millisecond, some latch and send the
            # off many seconds later. Pure mirroring makes the momentary
            # ones invisible; pure latching leaves the latching ones stuck
            # on, because their release arrives as state:off rather than as
            # a second press. Deferring only the too-early offs handles
            # both without needing to know which kind a pad is.
            name = msg.get("name")
            state = msg.get("state")
            lit_at = lit_at if lit_at is not None else {}
            pending_off = pending_off if pending_off is not None else {}

            if state == "on":
                pending_off.pop(name, None)
                lit_at[name] = time.monotonic()
                ok = send_feedback(client, name, "on", msg.get("page"))
                line += (f"  {BOLD}-> feedback on{RESET}" if ok
                         else "  (feedback send failed)")
            elif state == "off":
                held = time.monotonic() - lit_at.get(name, 0)
                if held < MIN_HOLD_S:
                    pending_off[name] = lit_at.get(name, 0) + MIN_HOLD_S
                    line += f"  {DIM}-> off deferred {MIN_HOLD_S}s{RESET}"
                else:
                    ok = send_feedback(client, name, "off", msg.get("page"))
                    line += (f"  {BOLD}-> feedback off{RESET}" if ok
                             else "  (feedback send failed)")
    elif event == "cmd":
        stats.commands += 1
        line = (f"{BOLD}cmd{RESET}   id={msg.get('id')} "
                f"param={msg.get('param')}")
        if echo:
            # feedback is addressed by NAME. A cmd carries only a numeric
            # id, so these pads cannot be lit at all -- not a configuration
            # problem, just a gap in the protocol.
            line += f"  {DIM}(no name -- feedback impossible){RESET}"
    else:
        line = f"{DIM}unknown evt {event!r}: {msg}{RESET}"

    print(f"{stamp}  {line}")
    if raw:
        print(f"          {DIM}{json.dumps(msg)}{RESET}")


def advertise(port):
    """Register _os2l._tcp so VirtualDJ can find us without os2lDirectIp.

    Optional on purpose: it needs the zeroconf package, and os2lDirectIp is
    one setting in VirtualDJ that removes the whole discovery question. If
    the import fails, say so and carry on -- discovery is a convenience,
    not a requirement.
    """
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        print(f"{DIM}(zeroconf not installed -- no DNS-SD advertisement. "
              f"Use os2lDirectIp, or: pip install zeroconf){RESET}")
        return None
    try:
        # Advertise loopback AND the LAN address. Which one VirtualDJ will
        # act on is not specified, and a record carrying only 127.0.0.1 is
        # useless to a VDJ running on another machine -- cheap to offer both.
        addresses = [socket.inet_aton("127.0.0.1")]
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))       # no traffic, just routing
            lan = probe.getsockname()[0]
            probe.close()
            if lan != "127.0.0.1":
                addresses.append(socket.inet_aton(lan))
        except OSError:
            lan = None

        info = ServiceInfo(
            "_os2l._tcp.local.",
            "pydmx._os2l._tcp.local.",
            addresses=addresses,
            port=port,
            properties={},
        )
        zc = Zeroconf()
        zc.register_service(info)
        where = "127.0.0.1" + (f" and {lan}" if lan and lan != "127.0.0.1" else "")
        print(f"{DIM}advertising _os2l._tcp on {where}:{port}{RESET}")
        return zc
    except Exception as exc:
        print(f"{DIM}(DNS-SD advertisement failed: {exc}){RESET}")
        return None


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--raw", action="store_true",
                        help="also print the raw JSON of every message")
    parser.add_argument("--echo", action="store_true",
                        help="latch each button and light it back via feedback")
    parser.add_argument("--blink", metavar="NAME",
                        help="flash one button on/off every 2s, no input needed")
    args = parser.parse_args()
    serve(args.port, args.raw, args.echo, args.blink)


if __name__ == "__main__":
    main()
