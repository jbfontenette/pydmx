#!/usr/bin/env python3
"""
DMX cycling test for the DSD TECH SH-RS09B (FTDI USB-to-RS485).

Channel 65 holds at 50. Channel 67 steps through 1, 11, 21, 31 and repeats,
one second per step. Blacks out cleanly on Ctrl-C.

    pip install pyserial
    python3 dmx_cycle.py
    python3 dmx_cycle.py /dev/cu.usbserial-A50285BI
"""

import glob
import sys
import time

import serial

# --- the show ---------------------------------------------------------------
STATIC = {65: 50}
CYCLE_CHANNEL = 67
CYCLE_VALUES = [1, 11, 21, 31]
CYCLE_INTERVAL_S = 1.0
# For a ping-pong (1,11,21,31,21,11,...) instead of a loop, use:
#   CYCLE_VALUES = [1, 11, 21, 31, 21, 11]

# --- DMX512 timing ----------------------------------------------------------
BREAK_S = 0.002
MAB_S = 0.001
REFRESH_HZ = 30

# A 513-byte frame at 250k baud, 8N2 (11 bits/byte) occupies the wire for
# 22.6ms. write()/flush() return well before that -- they only drain the
# kernel buffer, while the FTDI FIFO keeps transmitting in the background.
# So we timestamp the write and refuse to assert the next break until the
# frame has had time to finish, plus a guard. This is what keeps the output
# clean when something else on the Mac (Virtual DJ, say) steals CPU time.
FRAME_WIRE_TIME = 513 * 11 / 250000
FIFO_GUARD_S = 0.003


def find_port():
    ports = sorted(glob.glob("/dev/cu.usbserial*"))
    if not ports:
        sys.exit("No adapter found. Plug it in, then check: ls /dev/cu.*")
    if len(ports) > 1:
        print(f"Multiple candidates: {ports} -- using {ports[0]}")
    return ports[0]


class DmxSender:
    """Owns the serial port and the 513-byte frame. Only this writes to DMX."""

    def __init__(self, port):
        self.ser = serial.Serial(
            port=port,
            baudrate=250000,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
            timeout=0,
        )
        # [0] is the start code, [1..512] are channels 1..512. DMX is
        # 1-indexed, so frame[65] genuinely is channel 65 -- no off-by-one.
        self.frame = bytearray(513)
        self._wire_free_at = 0.0

    def set(self, channel, value):
        if not 1 <= channel <= 512:
            raise ValueError(f"channel {channel} out of range 1-512")
        if not 0 <= value <= 255:
            raise ValueError(f"value {value} out of range 0-255")
        self.frame[channel] = value

    def send(self):
        """Wait for the wire to clear, then send one packet."""
        wait = self._wire_free_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)

        self.ser.break_condition = True
        time.sleep(BREAK_S)
        self.ser.break_condition = False
        time.sleep(MAB_S)
        self.ser.write(self.frame)
        self.ser.flush()
        self._wire_free_at = time.monotonic() + FRAME_WIRE_TIME + FIFO_GUARD_S

    def blackout(self):
        for i in range(1, 513):
            self.frame[i] = 0
        self.send()
        time.sleep(FRAME_WIRE_TIME + 0.05)

    def close(self):
        self.ser.close()


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else find_port()
    dmx = DmxSender(port)

    for channel, value in STATIC.items():
        dmx.set(channel, value)

    print(f"Port:   {port}")
    print("Static: " + ", ".join(f"ch{c}={v}" for c, v in sorted(STATIC.items())))
    print(f"Cycle:  ch{CYCLE_CHANNEL} = {CYCLE_VALUES} @ {CYCLE_INTERVAL_S}s/step")
    print("Ctrl-C to stop.\n")

    budget = 1.0 / REFRESH_HZ
    started = time.monotonic()
    frames = 0
    last_step = None
    last_report = started

    try:
        while True:
            cycle_start = time.monotonic()

            # Derive the step from the clock rather than counting frames, so
            # the timing stays true even if a frame runs late.
            elapsed = cycle_start - started
            step = int(elapsed / CYCLE_INTERVAL_S) % len(CYCLE_VALUES)
            if step != last_step:
                dmx.set(CYCLE_CHANNEL, CYCLE_VALUES[step])
                last_step = step

            dmx.send()
            frames += 1

            now = time.monotonic()
            if now - last_report >= 1.0:
                print(f"\r{frames} fps, ch{CYCLE_CHANNEL}={CYCLE_VALUES[step]:3d}   ",
                      end="", flush=True)
                frames = 0
                last_report = now

            time.sleep(max(0.0, budget - (time.monotonic() - cycle_start)))

    except KeyboardInterrupt:
        print("\nBlacking out...")
        dmx.blackout()
        dmx.close()


if __name__ == "__main__":
    main()
