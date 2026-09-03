"""OS2L beat clock. Receives musical timing from Virtual DJ.

Shaped by what the protocol testing actually showed:

  pos is authoritative, not a counter we keep. It survives a pause, goes
      NEGATIVE before the beat-grid origin, and resets per deck. Deriving
      chaser position from it means pause, seek and deck changes need no
      special handling -- there is nothing to drift.

  change=true marks both resume-from-pause and a deck switch. Since position
      is derived from pos every beat, resync is automatic and the flag is
      only worth reporting.

  a pause stops beats entirely. A silent intro does not -- beats keep coming
      with strength 0. Two different silences, so 'is the clock alive' is a
      timeout question and 'is anything audible' is a strength question.

  bpm switches instantly on a deck change (80 -> 132.7 in one message), so
      anything scaled by tempo must be recomputed, never smoothed.

THREADING
    The listener runs its own thread but never touches the engine. Beats go
    into a queue and the main loop drains them with poll(), exactly as MIDI
    events are polled. That keeps every mutation of engine state on one
    thread and removes the need for locks around it.

    Nothing a peer sends may kill that thread. _dispatch counts and reports
    what it cannot decode rather than raising: an exception would unwind the
    accept loop and the beats would just stop, with no error and no way back
    without a restart.

CONNECTION
    We are the server; VirtualDJ connects to us. It will not connect until a
    DMX pad is pressed in VirtualDJ at least once per session -- DNS-SD
    advertisement does not avoid this, it was tested. The accept loop keeps
    running, so a VirtualDJ restart reconnects on the next pad press.
"""

import json
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass

DEFAULT_PORT = 9996

# How many beat periods of silence before the clock counts as stalled.
# A crossfade produced one 871ms beat where 750ms was expected, so 2.5 is
# comfortably clear of a normal transition while still catching a pause fast.
STALL_BEATS = 2.5
FALLBACK_BPM = 120.0


@dataclass
class Beat:
    pos: int
    bpm: float
    strength: float
    change: bool
    at: float

    @property
    def in_bar(self):
        """0-3. Python's modulo is non-negative, so this is right for
        negative pos too -- never 'fix' it with abs()."""
        return self.pos % 4

    @property
    def is_bar(self):
        return self.pos % 4 == 0

    @property
    def is_phrase(self):
        return self.pos % 16 == 0

    @property
    def audible(self):
        """Beats keep arriving through a silent intro with strength 0."""
        return self.strength is None or self.strength > 0


class _Stream:
    """Yields whole JSON objects from a byte stream.

    The spec defines the messages but not how they are delimited, so this
    decodes by object boundary rather than assuming newlines. Verified
    against newline-delimited, bare concatenation, and objects split across
    recv() calls.
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
                self.buffer = stripped
                break
            out.append(value)
            self.buffer = stripped[end:]
        return out


class BeatClock:
    def __init__(self, port=DEFAULT_PORT, advertise=True, on_status=None):
        self.port = port
        self.want_advertise = advertise
        self.on_status = on_status or (lambda msg: None)

        self._beats = deque(maxlen=64)
        self._others = deque(maxlen=64)
        self._stop = threading.Event()
        self._thread = None
        self._zc = None

        self.connected = False
        self.bpm = 0.0
        self.last_beat_at = 0.0
        self.total_beats = 0
        self.bad_messages = 0
        self._last_bad = None

    # --- lifecycle --------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        # Order matters: signal, wait for the thread to leave, and only then
        # close zeroconf. Closing it first can pull the registration out from
        # under _advertise while the listener thread is still inside it.
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.5)
        if self._zc:
            try:
                self._zc.close()
            except Exception:
                pass

    # --- main-thread interface -------------------------------------------
    def poll(self):
        """Beats received since the last call, oldest first.

        Every beat is kept rather than only the newest: a beat is an event,
        and dropping one would silently skip a chaser step.
        """
        out = []
        while self._beats:
            out.append(self._beats.popleft())
        return out

    def poll_messages(self):
        """btn and cmd events. Nothing acts on these; they are for logging."""
        out = []
        while self._others:
            out.append(self._others.popleft())
        return out

    @property
    def period(self):
        return 60.0 / (self.bpm or FALLBACK_BPM)

    @property
    def alive(self):
        """Is the music running? False while paused, stopped or disconnected."""
        if not self.connected or not self.last_beat_at:
            return False
        return (time.monotonic() - self.last_beat_at) < self.period * STALL_BEATS

    @property
    def stalled_for(self):
        if not self.last_beat_at:
            return None
        return time.monotonic() - self.last_beat_at

    # --- listener thread --------------------------------------------------
    def _run(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("0.0.0.0", self.port))
        except OSError as exc:
            self.on_status(f"cannot listen on port {self.port}: {exc}")
            return
        server.listen(1)
        server.settimeout(0.5)

        if self.want_advertise:
            self._zc = self._advertise()

        self.on_status(f"listening on port {self.port} "
                       f"(press a DMX pad in VirtualDJ to connect)")

        while not self._stop.is_set():
            try:
                client, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connected = True
            self.on_status(f"VirtualDJ connected from {addr[0]}")
            self._serve(client)
            self.connected = False
            self.last_beat_at = 0.0
            self.on_status("VirtualDJ disconnected")
        server.close()

    def _serve(self, client):
        client.settimeout(0.5)
        stream = _Stream()
        try:
            while not self._stop.is_set():
                try:
                    chunk = client.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                for msg in stream.feed(chunk.decode("utf-8", errors="replace")):
                    self._dispatch(msg)
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _dispatch(self, msg):
        """Handle one decoded message. Never raises.

        A malformed message must cost that message and nothing else. This
        runs on the listener thread, and an uncaught exception here would
        unwind _serve and _run and kill the thread: beats would simply stop,
        `connected` would stay True because nothing got to reset it, and no
        VirtualDJ restart could reconnect because the accept loop is gone.
        Silent death, mid-set. Every value in a beat message comes from
        another program's next release, so treat the whole decode as
        untrusted rather than guessing which field will change.
        """
        try:
            self._decode(msg)
        except Exception as exc:
            self._dropped(msg, exc)

    def _decode(self, msg):
        """Turn one message into a beat. Raises on anything unexpected."""
        if msg.get("evt") != "beat":
            self._others.append(msg)
            return
        pos = int(msg["pos"])
        bpm = float(msg.get("bpm") or 0) or self.bpm or FALLBACK_BPM
        strength = msg.get("strength")
        beat = Beat(pos=pos, bpm=bpm,
                    strength=None if strength is None else float(strength),
                    change=bool(msg.get("change")), at=time.monotonic())
        # Clock state is only touched once the whole beat has been built, so
        # a message that fails halfway leaves the tempo where it was.
        self.bpm = bpm
        self.last_beat_at = beat.at
        self.total_beats += 1
        self._beats.append(beat)

    def _dropped(self, msg, exc):
        """Count a bad message, and report each distinct fault once.

        VirtualDJ sends two of these a second; if one is malformed the rest
        usually are too, and a line per beat would bury everything else in
        the terminal. A new kind of fault reports again, so a second problem
        is not hidden by the first.
        """
        self.bad_messages += 1
        reason = f"{type(exc).__name__}: {exc}"
        if reason == self._last_bad:
            return
        self._last_bad = reason
        self.on_status(f"ignoring a message it cannot read -- {reason} "
                       f"in {repr(msg)[:60]} ({self.bad_messages} dropped)")

    def _advertise(self):
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            return None
        try:
            addresses = [socket.inet_aton("127.0.0.1")]
            try:
                probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                probe.connect(("8.8.8.8", 80))
                lan = probe.getsockname()[0]
                probe.close()
                if lan != "127.0.0.1":
                    addresses.append(socket.inet_aton(lan))
            except OSError:
                pass
            info = ServiceInfo("_os2l._tcp.local.", "pydmx._os2l._tcp.local.",
                               addresses=addresses, port=self.port,
                               properties={})
            zc = Zeroconf()
            zc.register_service(info)
            return zc
        except Exception:
            return None
