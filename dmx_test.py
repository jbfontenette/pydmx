#!/usr/bin/env python3
"""
DMX output smoke test for the DSD TECH SH-RS09B (FTDI USB-to-RS485).

Holds channel 65 at 50 and channel 67 at 11, refreshing ~30x per second.
Every other channel stays at 0. Blacks out cleanly on Ctrl-C.

    pip install pyserial
    python3 dmx_test.py                  # normal levels
    python3 dmx_test.py --full           # same channels at 255 (flicker test)
    python3 dmx_test.py /dev/cu.usbserial-A50285BI
"""

import glob
import sys
import time

import serial

# --- what we're sending -----------------------------------------------------
CHANNELS = {65: 50, 67: 11}

# --- DMX512 timing ----------------------------------------------------------
# A full 513-byte frame at 250k baud, 8N2 (11 bits/byte) takes 22.6ms on the
# wire. Break + MAB + settle push a cycle to ~26ms, so anything above ~35Hz
# risks asserting the next break while the FTDI FIFO is still transmitting --
# which truncates the frame and shows up as flicker. 30Hz leaves real slack.
BREAK_S = 0.002    # spec minimum 88us; longer helps cheap receivers resync
MAB_S = 0.001      # mark-after-break, spec minimum 8us
SETTLE_S = 0.002   # let the chip's FIFO drain before the next break
REFRESH_HZ = 30

FRAME_WIRE_TIME = 513 * 11 / 250000  # 22.6ms, for the headroom report


def find_port():
    ports = sorted(glob.glob("/dev/cu.usbserial*"))
    if not ports:
        sys.exit("No adapter found. Plug it in, then check: ls /dev/cu.*")
    if len(ports) > 1:
        print(f"Multiple candidates: {ports} -- using {ports[0]}")
    return ports[0]


def build_frame(values):
    """513 bytes: [0] is the start code, [1..512] are channels 1..512.

    DMX is 1-indexed, so frame[65] genuinely is channel 65 -- no off-by-one.
    """
    frame = bytearray(513)
    for channel, value in values.items():
        if not 1 <= channel <= 512:
            raise ValueError(f"channel {channel} out of range 1-512")
        if not 0 <= value <= 255:
            raise ValueError(f"value {value} out of range 0-255")
        frame[channel] = value
    return bytes(frame)


def send(ser, frame):
    """One DMX packet: break, mark-after-break, data, then settle."""
    ser.break_condition = True
    time.sleep(BREAK_S)
    ser.break_condition = False
    time.sleep(MAB_S)
    ser.write(frame)
    ser.flush()
    time.sleep(SETTLE_S)


def main():
    args = [a for a in sys.argv[1:]]
    full = "--full" in args
    args = [a for a in args if a != "--full"]
    port = args[0] if args else find_port()

    values = {ch: 255 for ch in CHANNELS} if full else dict(CHANNELS)
    frame = build_frame(values)

    ser = serial.Serial(
        port=port,
        baudrate=250000,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_TWO,
        timeout=0,
    )

    budget = 1.0 / REFRESH_HZ
    overhead = BREAK_S + MAB_S + SETTLE_S
    headroom = budget - FRAME_WIRE_TIME - overhead

    print(f"Port:     {port}")
    print("Sending:  " + ", ".join(f"ch{c}={v}" for c, v in sorted(values.items())))
    print(f"Target:   {REFRESH_HZ} fps ({budget*1000:.1f}ms budget, "
          f"{headroom*1000:.1f}ms headroom)")
    print("Ctrl-C to stop.\n")

    frames = 0
    worst = 0.0
    last_report = time.monotonic()

    try:
        while True:
            cycle_start = time.monotonic()
            send(ser, frame)

            elapsed = time.monotonic() - cycle_start
            worst = max(worst, elapsed)
            frames += 1

            now = time.monotonic()
            if now - last_report >= 1.0:
                # If fps sits well under target, or worst-case cycle exceeds
                # the budget, the loop is saturated -- lower REFRESH_HZ.
                flag = "  <-- OVER BUDGET" if worst > budget else ""
                print(f"\r{frames} fps, worst cycle {worst*1000:.1f}ms{flag}   ",
                      end="", flush=True)
                frames = 0
                worst = 0.0
                last_report = now

            time.sleep(max(0.0, budget - (time.monotonic() - cycle_start)))

    except KeyboardInterrupt:
        print("\nBlacking out...")
        send(ser, bytes(513))
        time.sleep(0.05)
        ser.close()


if __name__ == "__main__":
    main()
