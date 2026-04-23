# SPDX-License-Identifier: Apache-2.0
# PySunSpecSolarClient - Reads a SunSpec solar inverter and feeds available
# power into EVerest's energy tree as external limits.
#
# Flow:
#   Solar Inverter (SunSpec Modbus TCP)
#       -> this module reads Model 101 registers
#       -> calls set_external_limits on EnergyNode
#       -> EnergyManager adjusts EVSE charging current

import struct
import threading
import time
from datetime import datetime, timezone

from everest.framework import Module, RuntimeSession, log

try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    log.error("pymodbus not installed. Run: pip install pymodbus")
    raise

# SunSpec Model 101 register offsets (from model data start)
# Values are IEEE 754 float32 (2 registers each, big-endian)
M101_CURRENT = 0      # A  - Total AC current (2 regs)
M101_VOLTAGE = 14     # PhVphA - Phase A voltage (2 regs)
M101_POWER = 20       # W  - AC power (2 regs)
M101_FREQUENCY = 22   # Hz - Frequency (2 regs)
M101_STATE = 46       # St - Operating state (1 reg)

# SunSpec operating states
STATE_OFF = 1
STATE_SLEEPING = 2
STATE_MPPT = 4  # producing power


def regs_to_float(hi, lo):
    raw = struct.pack(">HH", hi, lo)
    return struct.unpack(">f", raw)[0]


class PySunSpecSolarClient:
    def __init__(self):
        self._session = RuntimeSession()
        m = Module(self._session)
        log.update_process_name(m.info.id)
        self._setup = m.say_hello()

        cfg = self._setup.configs.module
        self._host = cfg['sunspec_host']
        self._port = cfg['sunspec_port']
        self._poll_interval = cfg['poll_interval_s']
        self._nominal_voltage = cfg['nominal_voltage_V']

        self._mod = m
        self._modbus_client = None
        self._model101_base = None  # discovered at runtime

        # Get the fulfillment for our required connection
        connections = self._setup.connections
        fulfillments = connections.get('energy_node_external_limits', [])
        if not fulfillments:
            log.error("No fulfillment for energy_node_external_limits")
            self._limits_fulfillment = None
        else:
            self._limits_fulfillment = fulfillments[0]
            log.info(f"Will call external_limits on fulfillment: {self._limits_fulfillment}")

        m.init_done(self._ready)

    def _ready(self):
        log.info(f"PySunSpecSolarClient ready. Inverter at {self._host}:{self._port}")
        poller = threading.Thread(target=self._poll_loop, daemon=True)
        poller.start()

    def _connect(self):
        if self._modbus_client and self._modbus_client.connected:
            return True
        self._modbus_client = ModbusTcpClient(self._host, port=self._port, timeout=3)
        if not self._modbus_client.connect():
            log.error(f"Cannot connect to inverter at {self._host}:{self._port}")
            return False
        log.info(f"Connected to inverter at {self._host}:{self._port}")
        self._discover_sunspec()
        return True

    def _discover_sunspec(self):
        """Walk SunSpec register map to find Model 101."""
        r = self._modbus_client.read_holding_registers(0, 2)
        if r.isError():
            log.error("Cannot read SunSpec marker")
            return

        marker = chr(r.registers[0] >> 8) + chr(r.registers[0] & 0xFF) + \
                 chr(r.registers[1] >> 8) + chr(r.registers[1] & 0xFF)
        if marker != "SunS":
            log.error(f"Not a SunSpec device (marker: {marker})")
            return

        log.info("SunSpec device discovered")

        # Walk models
        offset = 2
        for _ in range(20):  # max 20 models
            r = self._modbus_client.read_holding_registers(offset, 2)
            if r.isError():
                break
            model_id = r.registers[0]
            model_len = r.registers[1]

            if model_id == 0xFFFF or model_id == 65535:
                break

            log.info(f"  Found Model {model_id}, length={model_len} at offset {offset}")

            if model_id in (101, 103):  # single or three-phase inverter
                self._model101_base = offset + 2  # skip header
                log.info(f"  -> Using Model {model_id} for solar data (base={self._model101_base})")

            offset += 2 + model_len

        if self._model101_base is None:
            log.warning("No inverter model (101/103) found in SunSpec device")

    def _read_solar(self):
        """Read current solar production from Model 101."""
        if self._model101_base is None:
            return None

        r = self._modbus_client.read_holding_registers(self._model101_base, 50)
        if r.isError():
            log.error(f"Failed to read Model 101: {r}")
            return None

        d = r.registers
        power_w = regs_to_float(d[M101_POWER], d[M101_POWER + 1])
        voltage = regs_to_float(d[M101_VOLTAGE], d[M101_VOLTAGE + 1])
        current = regs_to_float(d[M101_CURRENT], d[M101_CURRENT + 1])
        state = d[M101_STATE]

        return {
            "power_w": power_w,
            "voltage": voltage,
            "current": current,
            "state": state,
            "producing": state == STATE_MPPT,
        }

    def _set_energy_limits(self, solar_power_w):
        """Tell EnergyNode how much power is available from solar."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Calculate max current from solar power
        max_current_a = solar_power_w / self._nominal_voltage if self._nominal_voltage > 0 else 0
        max_current_a = max(0, max_current_a)

        limits = {
            "schedule_import": [
                {
                    "timestamp": now,
                    "limits_to_root": {
                        "total_power_W": {"value": solar_power_w, "source": "sunspec_solar"},
                        "ac_max_current_A": {"value": max_current_a, "source": "sunspec_solar"},
                    },
                    "limits_to_leaves": {
                        "total_power_W": {"value": solar_power_w, "source": "sunspec_solar"},
                        "ac_max_current_A": {"value": max_current_a, "source": "sunspec_solar"},
                    },
                }
            ],
            "schedule_export": [],
            "schedule_setpoints": [],
        }

        if self._limits_fulfillment is None:
            return
        try:
            self._mod.call_command(
                self._limits_fulfillment,
                "set_external_limits",
                {"value": limits}
            )
            log.info(f"  -> pushed limit: {solar_power_w:.0f}W / {max_current_a:.1f}A to EnergyNode")
        except Exception as e:
            log.error(f"Failed to push external limits: {e}")

    def _poll_loop(self):
        while True:
            try:
                if not self._connect():
                    time.sleep(5)
                    continue

                solar = self._read_solar()
                if solar:
                    log.info(
                        f"Solar: {solar['power_w']:.0f}W  "
                        f"{solar['voltage']:.0f}V  {solar['current']:.1f}A  "
                        f"({'producing' if solar['producing'] else 'idle'})"
                    )
                    self._set_energy_limits(solar['power_w'])
                else:
                    # No solar data, set limit to 0
                    self._set_energy_limits(0)

            except Exception as e:
                log.error(f"Poll error: {e}")
                self._modbus_client = None

            time.sleep(self._poll_interval)


client = PySunSpecSolarClient()
while True:
    time.sleep(1)
