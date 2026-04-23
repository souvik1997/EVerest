"""
SunSpec Modbus TCP Server for EVerest.

Subscribes to EVerest's internal MQTT broker, receives live EVSE telemetry,
and exposes it as SunSpec-compliant Modbus TCP registers so an external EMS
can discover the charger as a single-phase AC power source (Model 101).

Register layout (base address 40001, zero-indexed in the holding-register store):
  0-1     "SunS" discovery marker
  2-69    Model 1  (Common block, 66 regs + 2 header)
  70-121  Model 101 (Single-Phase Inverter, 50 regs + 2 header)
  122-123 End marker (0xFFFF, 0x0000)
"""

import json
import logging
import os
import struct
import threading
import time

import paho.mqtt.client as mqtt
from pymodbus.datastore import (
    ModbusServerContext,
    ModbusSlaveContext,
    ModbusSequentialDataBlock,
)
from pymodbus.server import StartTcpServer

# ---------------------------------------------------------------------------
# Configuration from environment (container-friendly)
# ---------------------------------------------------------------------------
MQTT_HOST = os.environ.get("MQTT_SERVER_ADDRESS", "mqtt-server")
MQTT_PORT = int(os.environ.get("MQTT_SERVER_PORT", "1883"))
MODBUS_PORT = int(os.environ.get("MODBUS_PORT", "502"))
MODULE_ID = os.environ.get("EVEREST_MODULE_ID", "ekepc2_bridge")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sunspec-bridge")

# ---------------------------------------------------------------------------
# MQTT topic paths (EVerest internal broker)
# ---------------------------------------------------------------------------
TOPIC_POWERMETER = f"everest/modules/{MODULE_ID}/impl/powermeter/var/powermeter"
TOPIC_EVENT = f"everest/modules/{MODULE_ID}/impl/board_support/var/event"
TOPIC_TELEMETRY = f"everest/modules/{MODULE_ID}/impl/board_support/var/telemetry"

# ---------------------------------------------------------------------------
# SunSpec register geometry
# ---------------------------------------------------------------------------
SUNSPEC_BASE = 0  # offset into our holding-register block (address 40001)
TOTAL_REGS = 124  # SunS(2) + Model1(68) + Model101(52) + End(2)

# SunSpec Model 101 offsets (relative to model start, after header)
# Using uint16 representation with scale factors, per SunSpec spec.
# For simplicity we store float values as pairs of uint16 (IEEE 754 big-endian).
MODEL1_OFFSET = 2
MODEL1_HEADER = 2   # model_id + length
MODEL1_LENGTH = 66  # payload registers
MODEL101_OFFSET = MODEL1_OFFSET + MODEL1_HEADER + MODEL1_LENGTH  # = 70
MODEL101_HEADER = 2
MODEL101_LENGTH = 50
END_OFFSET = MODEL101_OFFSET + MODEL101_HEADER + MODEL101_LENGTH  # = 122

# ---------------------------------------------------------------------------
# Helpers: pack values into uint16 register pairs
# ---------------------------------------------------------------------------

def pack_float_to_regs(value: float) -> tuple[int, int]:
    """Pack a 32-bit float into two uint16 registers (big-endian, high word first)."""
    raw = struct.pack(">f", value)
    hi, lo = struct.unpack(">HH", raw)
    return hi, lo


def pack_string_to_regs(s: str, num_regs: int) -> list[int]:
    """Pack a string into num_regs uint16 registers (2 chars per register, big-endian, null-padded)."""
    padded = s.encode("ascii", errors="replace")[:num_regs * 2].ljust(num_regs * 2, b"\x00")
    regs = []
    for i in range(0, len(padded), 2):
        regs.append((padded[i] << 8) | padded[i + 1])
    return regs


def pack_uint32_to_regs(value: int) -> tuple[int, int]:
    """Pack a 32-bit unsigned int into two uint16 registers (big-endian)."""
    raw = struct.pack(">I", value & 0xFFFFFFFF)
    hi, lo = struct.unpack(">HH", raw)
    return hi, lo


# ---------------------------------------------------------------------------
# SunSpec operating state mapping from IEC 61851 CP states
# ---------------------------------------------------------------------------
# SunSpec Model 101 St field: 1=Off, 2=Sleeping, 3=Starting, 4=MPPT, 5=Throttled, 6=Shutting down, 7=Fault
CP_TO_SUNSPEC_STATE = {
    "A": 1,   # Disconnected -> Off
    "B": 3,   # Connected, not charging -> Starting
    "C": 4,   # Charging -> MPPT (producing/consuming power)
    "D": 4,   # Charging with ventilation -> MPPT
    "E": 7,   # Fault
    "F": 7,   # Fault
}

# ---------------------------------------------------------------------------
# Shared state (updated by MQTT thread, read by Modbus server)
# ---------------------------------------------------------------------------

class EvseState:
    def __init__(self):
        self.lock = threading.Lock()
        # Powermeter data
        self.voltage_l1 = 0.0
        self.current_l1 = 0.0
        self.power_w = 0.0
        self.energy_wh = 0.0
        # Board support
        self.cp_state = "A"
        self.temperature_c = 0.0
        self.frequency_hz = 60.0  # default; update if available
        self.last_update = 0.0

    def update_powermeter(self, data: dict):
        with self.lock:
            self.voltage_l1 = data.get("voltage_V", {}).get("L1", self.voltage_l1)
            self.current_l1 = data.get("current_A", {}).get("L1", self.current_l1)
            self.power_w = data.get("power_W", {}).get("total", self.power_w)
            energy_import = data.get("energy_Wh_import", {})
            if isinstance(energy_import, dict):
                self.energy_wh = energy_import.get("total", self.energy_wh)
            freq = data.get("frequency_Hz", {})
            if isinstance(freq, dict) and freq.get("L1") is not None:
                self.frequency_hz = freq["L1"]
            self.last_update = time.time()

    def update_event(self, data: dict):
        with self.lock:
            event = data.get("event", self.cp_state)
            # Only map CP states, ignore PowerOn/PowerOff events
            if event in CP_TO_SUNSPEC_STATE:
                self.cp_state = event

    def update_telemetry(self, data: dict):
        with self.lock:
            self.temperature_c = data.get("evse_temperature_C", self.temperature_c)

    def snapshot(self):
        with self.lock:
            return {
                "voltage_l1": self.voltage_l1,
                "current_l1": self.current_l1,
                "power_w": self.power_w,
                "energy_wh": self.energy_wh,
                "cp_state": self.cp_state,
                "temperature_c": self.temperature_c,
                "frequency_hz": self.frequency_hz,
            }


state = EvseState()

# ---------------------------------------------------------------------------
# Build the initial SunSpec register image
# ---------------------------------------------------------------------------

def build_initial_registers() -> list[int]:
    """Create the full register image with static content filled in."""
    regs = [0] * TOTAL_REGS

    # --- "SunS" marker ---
    regs[0] = 0x5375  # "Su"
    regs[1] = 0x6E53  # "nS"

    # --- Model 1 (Common) header ---
    base = MODEL1_OFFSET
    regs[base] = 1               # Model ID
    regs[base + 1] = MODEL1_LENGTH  # Length

    # Model 1 payload: strings packed into registers
    # Mn: Manufacturer (16 regs = 32 chars)
    # Md: Model       (16 regs = 32 chars)
    # Opt: Options    (8 regs = 16 chars)
    # Vr: Version     (8 regs = 16 chars)
    # SN: Serial      (16 regs = 32 chars)
    # DA: Address     (8 regs = 16 chars)  -- NOTE: Actual Model 1 is 66 regs total
    p = base + 2
    mn = pack_string_to_regs("EVerest EVSE Bridge", 16)
    regs[p:p+16] = mn; p += 16

    md = pack_string_to_regs("EKEPC2-SunSpec", 16)
    regs[p:p+16] = md; p += 16

    opt = pack_string_to_regs("AC Single Phase", 8)
    regs[p:p+8] = opt; p += 8

    vr = pack_string_to_regs("1.0.0", 8)
    regs[p:p+8] = vr; p += 8

    sn = pack_string_to_regs("EV-SUNSPEC-001", 16)
    regs[p:p+16] = sn; p += 16

    da = pack_string_to_regs("1", 8)
    regs[p:p+8] = da; p += 8
    # Remaining Model 1 payload registers stay 0 (padding to 66)

    # --- Model 101 (Single-Phase Inverter) header ---
    base = MODEL101_OFFSET
    regs[base] = 101             # Model ID
    regs[base + 1] = MODEL101_LENGTH  # Length

    # --- End marker ---
    regs[END_OFFSET] = -1  # 0xFFFF as signed int16
    regs[END_OFFSET + 1] = 0x0000

    return regs


# ---------------------------------------------------------------------------
# Update dynamic Model 101 registers from live EVSE state
# ---------------------------------------------------------------------------

# Model 101 register layout (offsets from model data start, i.e. MODEL101_OFFSET + 2):
#  0-1   A     Total AC Current          (float32)
#  2-3   AphA  Phase A Current           (float32)
#  4-5   AphB  Phase B Current           (float32) -- 0 for single phase
#  6-7   AphC  Phase C Current           (float32) -- 0 for single phase
#  8-9   PPVphAB  Phase Voltage AB       (float32) -- 0 for single phase
# 10-11  PPVphBC  Phase Voltage BC       (float32) -- 0 for single phase
# 12-13  PPVphCA  Phase Voltage CA       (float32) -- 0 for single phase
# 14-15  PhVphA   Phase Voltage AN       (float32)
# 16-17  PhVphB   Phase Voltage BN       (float32) -- 0
# 18-19  PhVphC   Phase Voltage CN       (float32) -- 0
# 20-21  W     AC Power                  (float32)
# 22-23  Hz    Frequency                 (float32)
# 24-25  VA    Apparent Power            (float32)
# 26-27  VAr   Reactive Power            (float32)
# 28-29  PF    Power Factor              (float32)
# 30-31  WH    Lifetime Energy           (float32)
# 32-33  DCA   DC Current                (float32) -- 0 for AC
# 34-35  DCV   DC Voltage                (float32) -- 0 for AC
# 36-37  DCW   DC Power                  (float32) -- 0 for AC
# 38-39  TmpCab  Cabinet Temperature     (float32)
# 40-41  TmpSnk  Heat Sink Temperature   (float32) -- 0
# 42-43  TmpTrns Transformer Temp        (float32) -- 0
# 44-45  TmpOt   Other Temperature       (float32) -- 0
# 46     St    Operating State           (uint16)
# 47     StVnd Vendor State              (uint16)
# 48-49  Evt1  Event bitfield            (uint32)

def update_model101_registers(regs: list[int], snap: dict):
    """Write live EVSE data into the Model 101 portion of the register image."""
    p = MODEL101_OFFSET + 2  # start of model data

    v = snap["voltage_l1"]
    a = snap["current_l1"]
    w = snap["power_w"]
    hz = snap["frequency_hz"]
    energy = snap["energy_wh"]
    temp = snap["temperature_c"]
    cp = snap["cp_state"]

    # Apparent power (rough: V*I for single phase)
    va = v * a if v > 0 and a > 0 else abs(w)
    # Power factor
    pf = (w / va) if va > 0 else 1.0
    pf = max(-1.0, min(1.0, pf))

    def put_f(offset, val):
        hi, lo = pack_float_to_regs(val)
        regs[p + offset] = hi
        regs[p + offset + 1] = lo

    put_f(0, a)       # A   - Total AC Current
    put_f(2, a)       # AphA - Phase A Current
    put_f(4, 0.0)     # AphB
    put_f(6, 0.0)     # AphC
    put_f(8, 0.0)     # PPVphAB
    put_f(10, 0.0)    # PPVphBC
    put_f(12, 0.0)    # PPVphCA
    put_f(14, v)      # PhVphA - Phase A Voltage
    put_f(16, 0.0)    # PhVphB
    put_f(18, 0.0)    # PhVphC
    put_f(20, w)      # W   - AC Power
    put_f(22, hz)     # Hz  - Frequency
    put_f(24, va)     # VA  - Apparent Power
    put_f(26, 0.0)    # VAr - Reactive Power
    put_f(28, pf)     # PF  - Power Factor
    put_f(30, energy) # WH  - Lifetime Energy (Wh)
    put_f(32, 0.0)    # DCA
    put_f(34, 0.0)    # DCV
    put_f(36, 0.0)    # DCW
    put_f(38, temp)   # TmpCab - Cabinet Temperature
    put_f(40, 0.0)    # TmpSnk
    put_f(42, 0.0)    # TmpTrns
    put_f(44, 0.0)    # TmpOt

    # Operating state (uint16)
    regs[p + 46] = CP_TO_SUNSPEC_STATE.get(cp, 1)
    # Vendor state
    regs[p + 47] = 0
    # Event bitfield (uint32 -> 2 regs)
    hi, lo = pack_uint32_to_regs(0)
    regs[p + 48] = hi
    regs[p + 49] = lo


# ---------------------------------------------------------------------------
# MQTT client
# ---------------------------------------------------------------------------

def on_connect(client, userdata, flags, reason_code, properties):
    log.info("Connected to MQTT broker at %s:%s (rc=%s)", MQTT_HOST, MQTT_PORT, reason_code)
    client.subscribe(TOPIC_POWERMETER)
    client.subscribe(TOPIC_EVENT)
    client.subscribe(TOPIC_TELEMETRY)
    log.info("Subscribed to: %s", TOPIC_POWERMETER)
    log.info("Subscribed to: %s", TOPIC_EVENT)
    log.info("Subscribed to: %s", TOPIC_TELEMETRY)


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload)
        data = payload.get("data", payload)

        if msg.topic == TOPIC_POWERMETER:
            state.update_powermeter(data)
            log.debug("Powermeter update: V=%.1f A=%.1f W=%.1f",
                      data.get("voltage_V", {}).get("L1", 0),
                      data.get("current_A", {}).get("L1", 0),
                      data.get("power_W", {}).get("total", 0))
        elif msg.topic == TOPIC_EVENT:
            state.update_event(data)
            log.debug("Event update: %s", data.get("event"))
        elif msg.topic == TOPIC_TELEMETRY:
            state.update_telemetry(data)
            log.debug("Telemetry update: temp=%.1f", data.get("evse_temperature_C", 0))
    except Exception:
        log.exception("Failed to process MQTT message on %s", msg.topic)


def start_mqtt():
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="sunspec-bridge")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client


# ---------------------------------------------------------------------------
# Register refresh thread
# ---------------------------------------------------------------------------

def register_refresh_loop(context: ModbusServerContext, interval: float = 1.0):
    """Periodically copy live EVSE state into the Modbus register store."""
    regs = build_initial_registers()
    # Write initial image
    store = context[0x00]  # slave unit 1
    store.setValues(3, 0, regs)  # function code 3 = holding registers

    while True:
        snap = state.snapshot()
        update_model101_registers(regs, snap)
        store.setValues(3, 0, regs)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("SunSpec Modbus TCP Bridge for EVerest")
    log.info("MQTT broker: %s:%d", MQTT_HOST, MQTT_PORT)
    log.info("Modbus TCP port: %d", MODBUS_PORT)
    log.info("EVerest module: %s", MODULE_ID)

    # Build initial register image
    initial_regs = build_initial_registers()

    # Modbus datastore: address 0 maps to Modbus address 40001
    store = ModbusSlaveContext(
        hr=ModbusSequentialDataBlock(0, initial_regs),
        ir=ModbusSequentialDataBlock(0, [0] * 10),
        di=ModbusSequentialDataBlock(0, [0] * 10),
        co=ModbusSequentialDataBlock(0, [0] * 10),
    )
    context = ModbusServerContext(slaves=store, single=True)

    # Start MQTT subscriber
    start_mqtt()

    # Start register refresh thread
    refresh = threading.Thread(
        target=register_refresh_loop,
        args=(context, 1.0),
        daemon=True,
    )
    refresh.start()

    log.info("Starting Modbus TCP server on 0.0.0.0:%d ...", MODBUS_PORT)
    log.info("SunSpec discovery: read holding registers starting at 40001")
    log.info("Register map: SunS(0-1) Model1(2-69) Model101(70-121) End(122-123)")

    StartTcpServer(
        context=context,
        address=("0.0.0.0", MODBUS_PORT),
    )


if __name__ == "__main__":
    main()
