"""
Quick test client for the SunSpec Modbus TCP bridge.

Usage:
    pip install pymodbus
    python test_sunspec.py [host] [port]

Defaults to localhost:502
"""

import struct
import sys

from pymodbus.client import ModbusTcpClient

HOST = sys.argv[1] if len(sys.argv) > 1 else "localhost"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 502


def regs_to_float(hi: int, lo: int) -> float:
    raw = struct.pack(">HH", hi, lo)
    return struct.unpack(">f", raw)[0]


def regs_to_string(regs: list[int]) -> str:
    chars = []
    for r in regs:
        chars.append(chr((r >> 8) & 0xFF))
        chars.append(chr(r & 0xFF))
    return "".join(chars).rstrip("\x00")


def main():
    client = ModbusTcpClient(HOST, port=PORT)
    if not client.connect():
        print(f"FAIL: Could not connect to {HOST}:{PORT}")
        sys.exit(1)

    print(f"Connected to {HOST}:{PORT}\n")

    # --- Read SunS marker ---
    r = client.read_holding_registers(0, 2)
    if r.isError():
        print(f"FAIL: Could not read registers: {r}")
        sys.exit(1)

    marker = chr(r.registers[0] >> 8) + chr(r.registers[0] & 0xFF) + \
             chr(r.registers[1] >> 8) + chr(r.registers[1] & 0xFF)
    if marker == "SunS":
        print(f"[OK] SunSpec marker found: '{marker}'")
    else:
        print(f"FAIL: Expected 'SunS', got '{marker}' ({hex(r.registers[0])}, {hex(r.registers[1])})")
        sys.exit(1)

    # --- Read Model 1 (Common) ---
    r = client.read_holding_registers(2, 2)
    model_id = r.registers[0]
    model_len = r.registers[1]
    print(f"\n--- Model {model_id} (Common), length={model_len} ---")

    r = client.read_holding_registers(4, 16)
    print(f"  Manufacturer: {regs_to_string(r.registers)}")

    r = client.read_holding_registers(20, 16)
    print(f"  Model:        {regs_to_string(r.registers)}")

    r = client.read_holding_registers(36, 8)
    print(f"  Options:      {regs_to_string(r.registers)}")

    r = client.read_holding_registers(44, 8)
    print(f"  Version:      {regs_to_string(r.registers)}")

    r = client.read_holding_registers(52, 16)
    print(f"  Serial:       {regs_to_string(r.registers)}")

    # --- Read Model 101 (Inverter) ---
    r = client.read_holding_registers(70, 2)
    model_id = r.registers[0]
    model_len = r.registers[1]
    print(f"\n--- Model {model_id} (Single-Phase Inverter), length={model_len} ---")

    # Read the full model data (50 regs starting at offset 72)
    r = client.read_holding_registers(72, 50)
    d = r.registers

    ac_current   = regs_to_float(d[0], d[1])
    phase_a_curr = regs_to_float(d[2], d[3])
    phase_a_volt = regs_to_float(d[14], d[15])
    ac_power     = regs_to_float(d[20], d[21])
    frequency    = regs_to_float(d[22], d[23])
    apparent_pwr = regs_to_float(d[24], d[25])
    reactive_pwr = regs_to_float(d[26], d[27])
    power_factor = regs_to_float(d[28], d[29])
    energy_wh    = regs_to_float(d[30], d[31])
    cabinet_temp = regs_to_float(d[38], d[39])
    op_state     = d[46]

    state_names = {1: "Off", 2: "Sleeping", 3: "Starting", 4: "MPPT", 5: "Throttled", 6: "Shutting down", 7: "Fault"}

    print(f"  AC Current:     {ac_current:.2f} A")
    print(f"  Phase A Current:{phase_a_curr:.2f} A")
    print(f"  Phase A Voltage:{phase_a_volt:.1f} V")
    print(f"  AC Power:       {ac_power:.1f} W")
    print(f"  Frequency:      {frequency:.1f} Hz")
    print(f"  Apparent Power: {apparent_pwr:.1f} VA")
    print(f"  Reactive Power: {reactive_pwr:.1f} VAr")
    print(f"  Power Factor:   {power_factor:.3f}")
    print(f"  Energy (total): {energy_wh:.1f} Wh")
    print(f"  Cabinet Temp:   {cabinet_temp:.1f} C")
    print(f"  Operating State:{op_state} ({state_names.get(op_state, 'Unknown')})")

    # --- Verify end marker ---
    r = client.read_holding_registers(122, 2)
    if r.isError():
        print(f"\nWARN: Could not read end marker at register 122: {r}")
    elif r.registers[0] in (0xFFFF, 65535) and r.registers[1] == 0x0000:
        print(f"\n[OK] End marker found at register 122")
    else:
        print(f"\nWARN: Expected end marker (0xFFFF, 0x0000), got ({hex(r.registers[0])}, {hex(r.registers[1])})")

    print("\n--- Test complete ---")
    client.close()


if __name__ == "__main__":
    main()
