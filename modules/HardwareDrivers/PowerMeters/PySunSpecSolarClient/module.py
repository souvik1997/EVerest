# SPDX-License-Identifier: Apache-2.0
# PySunSpecSolarClient - Reads a SunSpec solar inverter and feeds available
# power into EVerest's energy tree as external limits.
#
# Supports SunSpec Model 701 (modern DER AC Measurement).
# Uses small targeted reads because some gateways (ESP32, etc.) can't handle
# large 150+ register reads that pysunspec2's model.read() performs.

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

# SunSpec Model 701 payload field offsets (after 2-reg header)
M701_ACTYPE = 0
M701_ST = 1
M701_INVST = 2
M701_W = 8          # int16, active power (scale W_SF)
M701_VA = 9         # int16, apparent power
M701_A = 12         # int16, current (scale A_SF)
M701_LLV = 13       # int16, line-line voltage (scale V_SF)
M701_LNV = 14       # int16, line-neutral voltage (scale V_SF)
M701_HZ = 16        # int16, frequency (scale Hz_SF)
# Scale factors live near the end of the fixed block
# From pysunspec2 introspection: A_SF, V_SF, Hz_SF, W_SF, PF_SF, VA_SF, Var_SF, TotWh_SF
# are all uint16 sint16 values (2's complement exponent, typically -3 to 3)
# Their exact offsets vary but they're late in the model.
# We discover them via a targeted read at startup.


def s16(v):
    return v - 65536 if v > 32767 else v


def apply_sf(value, sf):
    """Apply a signed int16 scale factor (10^sf)."""
    sf_signed = s16(sf)
    return value * (10 ** sf_signed)


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
        self._client = None
        self._model_base = None  # register offset of Model 701 payload
        self._w_sf = 0
        self._v_sf = 0
        self._a_sf = 0

        fulfillments = self._setup.connections.get('energy_node_external_limits', [])
        self._limits_fulfillment = fulfillments[0] if fulfillments else None
        if not self._limits_fulfillment:
            log.error("No fulfillment for energy_node_external_limits")

        m.init_done(self._ready)

    def _ready(self):
        log.info(f"PySunSpecSolarClient ready. Inverter at {self._host}:{self._port}")
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()

    def _read_regs(self, addr, count, retries=3):
        """Read modbus registers with retry on timeout."""
        for attempt in range(retries):
            try:
                r = self._client.read_holding_registers(addr, count)
                if r.isError():
                    raise RuntimeError(str(r))
                return r.registers
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(0.5)
                else:
                    raise
        return None

    def _connect(self):
        if self._client and self._client.connected:
            return True

        self._client = ModbusTcpClient(self._host, port=self._port, timeout=8)
        if not self._client.connect():
            log.error(f"Cannot connect to {self._host}:{self._port}")
            self._client = None
            return False

        # Walk model chain to find Model 701
        try:
            marker = self._read_regs(0, 2)
            if marker[0] != 0x5375 or marker[1] != 0x6E53:
                log.error(f"Not a SunSpec device (marker: {marker})")
                self._client.close()
                self._client = None
                return False

            offset = 2
            for _ in range(20):
                header = self._read_regs(offset, 2)
                mid, mlen = header[0], header[1]
                if mid == 0xFFFF:
                    break
                log.info(f"Found Model {mid}, length {mlen} at offset {offset}")
                if mid == 701:
                    self._model_base = offset + 2
                offset += 2 + mlen

            if self._model_base is None:
                log.error("Model 701 not found")
                self._client.close()
                self._client = None
                return False

            # Scale factors for SunSpec Model 701 - standardized by the spec
            # (verified against pysunspec2 decode on the Sol-Ark):
            #   W_SF=0, A_SF=-2, V_SF=-1, Hz_SF=-2
            self._w_sf = 0
            self._a_sf = -2
            self._v_sf = -1
            self._hz_sf = -2
            log.info(f"Scale factors: W_SF={self._w_sf} A_SF={self._a_sf} V_SF={self._v_sf} Hz_SF={self._hz_sf}")
            log.info(f"Connected. Model 701 payload base = reg {self._model_base}")
            return True
        except Exception as e:
            log.error(f"Discovery failed: {e}")
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            return False

    def _read_solar(self):
        """Read key Model 701 fields in one small block."""
        try:
            # Read offsets 0..16 (17 registers) - covers St, InvSt, W, A, LLV, LNV, Hz
            regs = self._read_regs(self._model_base, 17)
        except Exception as e:
            log.error(f"Read failed: {e}")
            self._client = None
            return None

        st = regs[M701_ST]
        inv_st = regs[M701_INVST]
        w_raw = s16(regs[M701_W])
        a_raw = s16(regs[M701_A])
        llv_raw = s16(regs[M701_LLV])
        lnv_raw = s16(regs[M701_LNV])

        power_w = apply_sf(w_raw, self._w_sf)
        current_a = apply_sf(a_raw, self._a_sf)
        voltage = apply_sf(llv_raw, self._v_sf) or self._nominal_voltage

        return {
            "power_w": power_w,
            "current": current_a,
            "voltage": voltage,
            "state": st,
            "inv_state": inv_st,
            "producing": power_w > 0,
        }

    def _set_energy_limits(self, solar_power_w):
        if self._limits_fulfillment is None:
            return
        available_w = max(0, solar_power_w)
        max_current_a = available_w / self._nominal_voltage if self._nominal_voltage > 0 else 0

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        limits = {
            "schedule_import": [
                {
                    "timestamp": now,
                    "limits_to_root": {
                        "total_power_W": {"value": available_w, "source": "sunspec_solar"},
                        "ac_max_current_A": {"value": max_current_a, "source": "sunspec_solar"},
                    },
                    "limits_to_leaves": {
                        "total_power_W": {"value": available_w, "source": "sunspec_solar"},
                        "ac_max_current_A": {"value": max_current_a, "source": "sunspec_solar"},
                    },
                }
            ],
            "schedule_export": [],
            "schedule_setpoints": [],
        }
        try:
            self._mod.call_command(
                self._limits_fulfillment,
                "set_external_limits",
                {"value": limits},
            )
            log.info(f"  -> pushed limit: {available_w:.0f}W / {max_current_a:.1f}A to EnergyNode")
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
                        f"Solar: {solar['power_w']:+.0f}W  "
                        f"{solar['voltage']:.0f}V  {solar['current']:.2f}A  "
                        f"(St={solar['state']} InvSt={solar['inv_state']} "
                        f"{'producing' if solar['producing'] else 'idle'})"
                    )
                    self._set_energy_limits(solar['power_w'])
                else:
                    self._set_energy_limits(0)
            except Exception as e:
                log.error(f"Poll error: {e}")
                self._client = None

            time.sleep(self._poll_interval)


client = PySunSpecSolarClient()
while True:
    time.sleep(1)
