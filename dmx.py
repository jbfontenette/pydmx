"""DMX512 output over an FTDI USB-to-RS485 adapter (DSD TECH SH-RS09B).

The adapter has no DMX firmware -- it is a plain RS485 bridge, so this module
generates the DMX512 protocol timing in software.

Timing notes earned the hard way:

  * A 513-byte frame at 250k baud, 8N2 (11 bits/byte) occupies the wire for
    22.6ms. That caps the frame rate near 44Hz.
  * write() and flush() return well before that. tcdrain empties the kernel
    buffer, not the FTDI chip's FIFO, so it is NOT a transmission barrier.
  * Asserting the next break while the FIFO is still transmitting truncates
    the frame and shows up as flicker. _wire_free_at is the guard against it.
  * 30Hz leaves ~10ms of slack, which survives CPU contention from other apps.
"""

import glob
import threading
import time

import serial


class AdapterError(RuntimeError):
    """The USB-RS485 adapter is missing, busy, or has gone away."""


BREAK_S = 0.002           # spec minimum 88us; longer helps cheap receivers
MAB_S = 0.001             # mark-after-break, spec minimum 8us
FRAME_WIRE_TIME = 513 * 11 / 250000   # 22.6ms
FIFO_GUARD_S = 0.003
DEFAULT_REFRESH_HZ = 30


def list_ports():
    """Every callout device macOS currently exposes."""
    return sorted(glob.glob("/dev/cu.*"))


def find_port():
    ports = sorted(glob.glob("/dev/cu.usbserial*"))
    if not ports:
        others = [p for p in list_ports() if "Bluetooth" not in p]
        detail = ("\n  Other serial devices present: " + ", ".join(others)
                  if others else "\n  No serial devices at all.")
        raise AdapterError(
            "No FTDI adapter found (looked for /dev/cu.usbserial*)." + detail
            + "\n  Check the USB cable, then run: ls /dev/cu.*")
    return ports[0]


def check_adapter(port=None):
    """Pre-flight check. Returns (port_name, problem_or_None).

    Deliberately does NOT open the port. An earlier version opened and closed
    it to detect a busy device, but that double-open was a plausible cause of
    a one-off failure that cleared on restart -- macOS FTDI ports can be
    sticky about a fast close-then-reopen. DmxSender's own open failure
    reports "busy" just as clearly, one open instead of two.
    """
    try:
        return (port or find_port()), None
    except AdapterError as exc:
        return None, str(exc)


class DmxSender:
    """Owns the serial port and the 513-byte frame.

    Thread-safe by design: any thread may call set()/apply(), but exactly one
    thread should call send(). That split is the whole architecture -- MIDI
    callbacks and chaser timers mutate state, and a single loop transmits it.
    Never let an input event trigger a write directly.
    """

    def __init__(self, port=None, on_status=None):
        self.port_name = port or find_port()
        self.on_status = on_status or (lambda msg: None)
        self.ser = self._open()
        self.connected = True
        self._retry_at = 0.0
        # [0] is the start code, [1..512] are channels 1..512. DMX is
        # 1-indexed, so frame[65] genuinely is channel 65 -- no off-by-one.
        self._frame = bytearray(513)
        self._lock = threading.Lock()
        self._wire_free_at = 0.0

    def _open(self):
        try:
            return serial.Serial(
                port=self.port_name, baudrate=250000,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_TWO, timeout=0,
            )
        except (serial.SerialException, OSError) as exc:
            raise AdapterError(
                f"Could not open {self.port_name}: {exc}"
                "\n  If this says busy, another process holds it -- a stale"
                "\n  python run, or DMX output enabled in Virtual DJ.")

    # --- state ------------------------------------------------------------
    def set(self, channel, value):
        if not 1 <= channel <= 512:
            raise ValueError(f"channel {channel} out of range 1-512")
        if not 0 <= value <= 255:
            raise ValueError(f"value {value} out of range 0-255")
        with self._lock:
            self._frame[channel] = value

    def apply(self, levels):
        """Replace the whole universe with `levels` ({channel: value}).

        Done under one lock so the transmit thread can never see a
        half-applied scene.
        """
        with self._lock:
            for i in range(1, 513):
                self._frame[i] = 0
            for channel, value in levels.items():
                self._frame[channel] = value

    def snapshot(self):
        with self._lock:
            return bytes(self._frame)

    # --- transmit ---------------------------------------------------------
    def send(self):
        """Send one packet. Call from exactly one thread.

        Survives the adapter being unplugged mid-show: the frame is dropped,
        state is kept, and reconnection is retried once a second. Lighting
        holds its last value while disconnected, so the rig freezes rather
        than blacking out.
        """
        if not self.connected:
            self._retry()
            return

        frame = self.snapshot()

        wait = self._wire_free_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)

        try:
            self.ser.break_condition = True
            time.sleep(BREAK_S)
            self.ser.break_condition = False
            time.sleep(MAB_S)
            self.ser.write(frame)
            self.ser.flush()
        except (serial.SerialException, OSError) as exc:
            self._drop(exc)
            return

        self._wire_free_at = time.monotonic() + FRAME_WIRE_TIME + FIFO_GUARD_S

    def _drop(self, exc):
        self.connected = False
        self._retry_at = time.monotonic() + 1.0
        try:
            self.ser.close()
        except Exception:
            pass
        self.on_status(f"adapter lost ({exc}) -- retrying every second")

    def _retry(self):
        now = time.monotonic()
        if now < self._retry_at:
            time.sleep(0.05)
            return
        self._retry_at = now + 1.0
        try:
            # It may come back on a different device name after a replug.
            self.port_name = find_port()
            self.ser = self._open()
        except AdapterError:
            return
        self.connected = True
        self._wire_free_at = 0.0
        self.on_status(f"adapter back on {self.port_name}")

    def run_until(self, stop_event, refresh_hz=DEFAULT_REFRESH_HZ, on_frame=None):
        """Transmit continuously until stop_event is set."""
        budget = 1.0 / refresh_hz
        while not stop_event.is_set():
            started = time.monotonic()
            if on_frame:
                on_frame()
            self.send()
            stop_event.wait(max(0.0, budget - (time.monotonic() - started)))

    def blackout(self):
        self.apply({})
        self.send()
        time.sleep(FRAME_WIRE_TIME + 0.05)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            if self.connected:
                self.blackout()
        finally:
            self.close()


class NullSender:
    """A DmxSender that holds state but drives no hardware.

    For working with no adapter attached -- writing scenes, checking merge
    behaviour, or driving the monitor. Deliberately a separate class rather
    than a flag inside DmxSender: the real sender's job is protocol timing on
    a serial port, and threading a "pretend" branch through that would put
    untested code paths in the one place that must stay reliable.
    """

    def __init__(self, on_status=None):
        self.port_name = "(none - dry run)"
        self.connected = True
        self.on_status = on_status or (lambda msg: None)
        self._frame = bytearray(513)
        self._lock = threading.Lock()

    def set(self, channel, value):
        if not 1 <= channel <= 512:
            raise ValueError(f"channel {channel} out of range 1-512")
        if not 0 <= value <= 255:
            raise ValueError(f"value {value} out of range 0-255")
        with self._lock:
            self._frame[channel] = value

    def apply(self, levels):
        with self._lock:
            for i in range(1, 513):
                self._frame[i] = 0
            for channel, value in levels.items():
                self._frame[channel] = value

    def snapshot(self):
        with self._lock:
            return bytes(self._frame)

    def send(self):
        # Returns immediately. run_until() paces itself and never calls this,
        # so the only callers are tools driving the sender by hand -- and
        # blocking them for a frame time buys nothing when there is no wire
        # to keep clear. A caller that wants a realistic frame rate should
        # sleep for it itself, visibly, rather than have send() do it.
        pass

    def run_until(self, stop_event, refresh_hz=DEFAULT_REFRESH_HZ, on_frame=None):
        budget = 1.0 / refresh_hz
        while not stop_event.is_set():
            started = time.monotonic()
            if on_frame:
                on_frame()
            stop_event.wait(max(0.0, budget - (time.monotonic() - started)))

    def blackout(self):
        self.apply({})

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.blackout()
