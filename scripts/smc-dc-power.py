#!/usr/bin/env python3
"""Read the real system power draw from the Apple SMC (DC-in rail).

The SMC exposes iD0R (current) and vD0R (voltage) as IEEE-754 floats through
the key_at_index sysfs interface. Key indices are stable per machine, so this
script discovers them once and then samples.

Usage: sudo ./smc-dc-power.py [samples] [interval_s]
"""
import glob
import struct
import sys
import time

SMC = "/sys/devices/platform/applesmc.768"
KEYS = ("iD0R", "vD0R")


def find_indices():
    idx = {}
    kc = int(open(f"{SMC}/key_count").read())
    for i in range(kc):
        open(f"{SMC}/key_at_index", "w").write(str(i))
        try:
            name = open(f"{SMC}/key_at_index_name").read().strip()
        except OSError:
            continue
        if name in KEYS:
            idx[name] = i
    return idx


def read_key(i):
    open(f"{SMC}/key_at_index", "w").write(str(i))
    data = open(f"{SMC}/key_at_index_data", "rb").read()
    return struct.unpack("<f", data[:4])[0]


def main():
    samples = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    if not glob.glob(f"{SMC}/key_count"):
        sys.exit(f"applesmc not found at {SMC}")
    idx = find_indices()
    if not all(k in idx for k in KEYS):
        sys.exit(f"SMC keys {KEYS} not found")
    for _ in range(samples):
        amps = read_key(idx["iD0R"])
        volts = read_key(idx["vD0R"])
        print(f"{volts:.2f} V x {amps:.3f} A = {volts * amps:.1f} W")
        time.sleep(interval)


if __name__ == "__main__":
    main()
