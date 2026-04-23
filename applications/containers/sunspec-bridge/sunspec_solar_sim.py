"""
Simulated SunSpec Solar Inverter (Modbus TCP Server).

Pretends to be a solar inverter producing varying power on a sine curve
(simulating a day cycle compressed into minutes). Serves SunSpec Model 1
(Common) + Model 101 (Single-Phase Inverter) on Modbus TCP port 5020.

An EVerest module can read this to get "solar availability" and feed it
into the energy tree.

Usage:
    python sunspec_solar_sim.py
    # Serves on 0.0.0.0:5020
"""

import math
import struct
import threading
import time
import logging
import os

from pymodbus.datastore import (
    ModbusServerContext,
    ModbusSlaveContext,
    ModbusSequentialDataBlock,
)
from pymodbus.server import StartTcpServer

MODBUS_PORT = int(os.environ.get("MODBUS_PORT", "5020"))
PEAK_POWER_W = float(os.environ.get("PEAK_POWER_W", "5000"))
CYCLE_SECONDS = float(os.environ.get("CYCLE_SECONDS", "120"))  # full day cycle in seconds

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("solar-sim")

TOTAL_REGS = 124
MODEL1_OFFSET = 2
MODEL1_LENGTH = 66
MODEL101_OFFSET = 70
MODEL101_LENGTH = 50
END_OFFSET = 122


def pack_float(value):
    raw = struct.pack(">f", value)
    hi, lo = struct.unpack(">HH", raw)
    return hi, lo


def pack_string(s, num_regs):
    padded = s.encode("ascii", errors="replace")[:num_regs * 2].ljust(num_regs * 2, b"\x00")
    return [(padded[i] << 8) | padded[i + 1] for i in range(0, len(padded), 2)]


def build_registers():
    regs = [0] * TOTAL_REGS

    # SunS marker
    regs[0] = 0x5375  # "Su"
    regs[1] = 0x6E53  # "nS"

    # Model 1 (Common)
    regs[2] = 1
    regs[3] = MODEL1_LENGTH
    p = 4
    regs[p:p+16] = pack_string("SolarSim Inc.", 16); p += 16
    regs[p:p+16] = pack_string("SIM-5KW-AC", 16); p += 16
    regs[p:p+8] = pack_string("Single Phase PV", 8); p += 8
    regs[p:p+8] = pack_string("1.0.0", 8); p += 8
    regs[p:p+16] = pack_string("SIM-SOLAR-001", 16); p += 16
    regs[p:p+8] = pack_string("1", 8); p += 8

    # Model 101 (Single-Phase Inverter)
    regs[MODEL101_OFFSET] = 101
    regs[MODEL101_OFFSET + 1] = MODEL101_LENGTH

    # End marker (-1 = 0xFFFF as signed int16)
    regs[END_OFFSET] = -1
    regs[END_OFFSET + 1] = 0

    return regs


def solar_output(t):
    """Simulate solar output: sine wave from 0 to peak, 0 at night."""
    phase = (t % CYCLE_SECONDS) / CYCLE_SECONDS  # 0.0 to 1.0
    # Sine curve: 0 at start/end (night), peak at 0.5 (noon)
    power = max(0, math.sin(phase * math.pi)) * PEAK_POWER_W
    return power


def update_loop(context):
    regs = build_registers()
    store = context[0x00]
    store.setValues(3, 0, regs)
    t0 = time.time()

    while True:
        t = time.time() - t0
        power_w = solar_output(t)
        voltage = 240.0 if power_w > 0 else 0.0
        current = power_w / voltage if voltage > 0 else 0.0
        frequency = 60.0

        p = MODEL101_OFFSET + 2
        def put(offset, val):
            hi, lo = pack_float(val)
            regs[p + offset] = hi
            regs[p + offset + 1] = lo

        put(0, current)       # A - total current
        put(2, current)       # AphA
        put(4, 0.0)           # AphB
        put(6, 0.0)           # AphC
        put(14, voltage)      # PhVphA
        put(20, power_w)      # W - AC power
        put(22, frequency)    # Hz
        put(24, voltage * current)  # VA
        put(28, 1.0)          # PF
        put(30, 0.0)          # WH (not tracking cumulative)

        # State: 4=MPPT (producing) if power > 0, else 2=Sleeping
        regs[p + 46] = 4 if power_w > 100 else 2

        store.setValues(3, 0, regs)

        phase_pct = ((t % CYCLE_SECONDS) / CYCLE_SECONDS) * 100
        log.info(f"Solar: {power_w:.0f}W  {voltage:.0f}V  {current:.1f}A  (cycle {phase_pct:.0f}%)")
        time.sleep(2)


def main():
    log.info(f"SunSpec Solar Inverter Simulator")
    log.info(f"Peak power: {PEAK_POWER_W}W, cycle: {CYCLE_SECONDS}s, port: {MODBUS_PORT}")

    initial = build_registers()
    store = ModbusSlaveContext(
        hr=ModbusSequentialDataBlock(0, initial),
        ir=ModbusSequentialDataBlock(0, [0] * 10),
        di=ModbusSequentialDataBlock(0, [0] * 10),
        co=ModbusSequentialDataBlock(0, [0] * 10),
    )
    context = ModbusServerContext(slaves=store, single=True)

    updater = threading.Thread(target=update_loop, args=(context,), daemon=True)
    updater.start()

    log.info(f"Serving on 0.0.0.0:{MODBUS_PORT}")
    StartTcpServer(context=context, address=("0.0.0.0", MODBUS_PORT))


if __name__ == "__main__":
    main()
