"""Shared wire format for the DMX monitor tap.

The controller publishes each frame as a UDP datagram to localhost; dmxmon.py
subscribes. UDP was chosen deliberately:

  * The controller must never block or slow down because a viewer is slow,
    absent, or has crashed. UDP send to a closed port is a no-op.
  * The viewer can start and stop freely without the controller noticing.
  * Frames are a continuous stream of complete state -- a dropped datagram
    costs one refresh, not consistency. There is nothing to retransmit.

Datagram: b"DMX1" + 513 frame bytes (start code + channels 1..512).
"""

import socket

MAGIC = b"DMX1"
DEFAULT_ADDR = ("127.0.0.1", 9000)
FRAME_LEN = 513
PACKET_LEN = len(MAGIC) + FRAME_LEN


def parse_addr(spec):
    """'9000' or 'host:9000' -> (host, port). Empty or None -> the default.

    Empty string is a real case, not a defensive nicety: a bare --monitor
    flag with no value arrives here as "".
    """
    if spec is None:
        return DEFAULT_ADDR
    spec = str(spec).strip()
    if not spec:
        return DEFAULT_ADDR

    host, port = DEFAULT_ADDR[0], spec
    if ":" in spec:
        host, _, port = spec.rpartition(":")
        host = host or DEFAULT_ADDR[0]
    try:
        port = int(port)
    except ValueError:
        raise ValueError(f"bad monitor address '{spec}' -- "
                         f"expected a port like 9000, or host:9000")
    if not 1 <= port <= 65535:
        raise ValueError(f"port {port} out of range 1-65535")
    return (host, port)


class Publisher:
    """Fire-and-forget frame sender. Never raises into the caller."""

    def __init__(self, addr=DEFAULT_ADDR):
        self.addr = addr
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sent = 0

    def send(self, frame):
        try:
            self.sock.sendto(MAGIC + bytes(frame[:FRAME_LEN]), self.addr)
            self.sent += 1
        except OSError:
            pass          # no listener, buffer full -- neither is our problem

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Receiver:
    """Reads frames. recv() returns the newest available, or None."""

    def __init__(self, addr=DEFAULT_ADDR, timeout=0.2):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(addr)
        self.sock.settimeout(timeout)
        self.received = 0

    def recv(self):
        """Drain the queue and return only the latest frame.

        Draining matters: if the viewer redraws slower than 30fps it would
        otherwise fall progressively further behind, displaying history.
        """
        newest = None
        try:
            data = self.sock.recv(PACKET_LEN + 64)
        except (socket.timeout, OSError):
            return None
        while True:
            if data.startswith(MAGIC) and len(data) >= PACKET_LEN:
                newest = data[len(MAGIC):PACKET_LEN]
                self.received += 1
            self.sock.settimeout(0)
            try:
                data = self.sock.recv(PACKET_LEN + 64)
            except (socket.timeout, BlockingIOError, OSError):
                break
            finally:
                self.sock.settimeout(0.2)
        return newest

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
