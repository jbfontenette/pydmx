"""Wire protocol between controller.py (--sim) and apcsim.py.

Two UDP sockets, mirroring the monitor design and for the same reason: the
controller must never stall because a UI process is slow, absent or dead.

    controller --LED updates--> port 9100 (apcsim listens)
    apcsim     --key events---> port 9101 (controller listens)

Messages, all prefixed with b"APC1":

  from controller           from simulator
    L <note><vel><chan>...    P <note>          press
    C                         R <note>          release
    ?  (fader enquiry)        F <n><value>      fader n (1-9) moved
                              I <f1..f9>        reply to fader enquiry
                              H                 hello / resync request

LED updates are batched: one datagram can carry many triples, because a full
repaint is 80 of them.

HELLO exists because UDP has no connection and this link has no retries. The
controller paints the whole surface once at startup; if the simulator is not
listening yet those datagrams simply vanish, and the controller's LED diff
cache then believes that state was delivered and never resends it. So the
simulator announces itself instead, and keeps doing so, and the controller
replays its cached state on hearing it. That also covers the reverse order
(controller restarted while the simulator kept running) with no extra logic.
"""

import socket

MAGIC = b"APC1"
LED_ADDR = ("127.0.0.1", 9100)
EVENT_ADDR = ("127.0.0.1", 9101)

LED = ord("L")
CLEAR = ord("C")
ENQUIRE = ord("?")
HELLO = ord("H")
PRESS = ord("P")
RELEASE = ord("R")
FADER = ord("F")
INTRO = ord("I")

MAX_PACKET = 2048


def parse_addr(spec, default):
    """'9100' or 'host:9100' -> (host, port). Empty or None -> default."""
    if spec is None:
        return default
    spec = str(spec).strip()
    if not spec:
        return default
    host, port = default[0], spec
    if ":" in spec:
        host, _, port = spec.rpartition(":")
        host = host or default[0]
    try:
        port = int(port)
    except ValueError:
        raise ValueError(f"bad address '{spec}' -- expected a port like 9100")
    if not 1 <= port <= 65535:
        raise ValueError(f"port {port} out of range 1-65535")
    return (host, port)


class Endpoint:
    """One bound socket that can also send. Never raises into the caller."""

    def __init__(self, bind_addr, send_addr):
        self.send_addr = send_addr
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(bind_addr)
        self.sock.setblocking(False)

    def send(self, payload):
        try:
            self.sock.sendto(MAGIC + bytes(payload), self.send_addr)
        except OSError:
            pass          # no listener yet, or buffer full -- not our problem

    def drain(self):
        """Every datagram waiting, oldest first, magic already stripped.

        Unlike the DMX monitor we keep ALL of them: these are discrete
        events, and dropping a keypress in favour of a newer one would lose
        it entirely. The DMX tap can discard stale frames because each frame
        is complete state; a press is not.
        """
        out = []
        while True:
            try:
                data = self.sock.recv(MAX_PACKET)
            except (BlockingIOError, OSError):
                break
            if data.startswith(MAGIC) and len(data) > len(MAGIC):
                out.append(data[len(MAGIC):])
        return out

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def encode_leds(updates):
    """[(note, velocity, channel), ...] -> payload bytes."""
    payload = bytearray([LED])
    for note, velocity, channel in updates:
        payload += bytes((note & 0x7F, velocity & 0x7F, channel & 0x0F))
    return bytes(payload)


def decode_leds(payload):
    body = payload[1:]
    return [(body[i], body[i + 1], body[i + 2])
            for i in range(0, len(body) - 2, 3)]
