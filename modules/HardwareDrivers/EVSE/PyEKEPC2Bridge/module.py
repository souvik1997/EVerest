# SPDX-License-Identifier: Apache-2.0
# EKEPC2Bridge - EVerest BSP module for EKEPC2 EVSE controller via ESP32 MQTT bridge
#
# Architecture:
#   EKEPC2 EVSE <--RS485 Modbus--> ESP32 <--WiFi/MQTT--> this module <--EVerest MQTT--> EvseManager
#
# The ESP32 polls the EKEPC2 controller over Modbus RTU and publishes telemetry as JSON
# to an external MQTT broker. This module subscribes to that telemetry, maps EKEPC2 status
# codes to IEC 61851 CP states, and translates EVerest commands into JSON commands for
# the ESP32 to execute as Modbus writes.

import json
import threading
import time
from datetime import datetime, timezone

from everest.framework import Module, RuntimeSession, log

try:
    import paho.mqtt.client as mqtt
except ImportError:
    log.error("paho-mqtt not installed. Run: pip install paho-mqtt")
    raise

# EKEPC2 status register (141) to IEC 61851 CP state mapping
EKEPC2_STATUS_TO_CP = {
    0: "F",   # Fault: power self-check failed
    1: "A",   # Ready: CP disconnected
    2: "B",   # RFID waiting: vehicle present, not authorized
    3: "B",   # Connected: CP diode + 2.7K ohm
    4: "C",   # Connected: CP diode + 1.3K ohm (requesting power)
    5: "C",   # Charging: CP diode + 2.7K parallel 1.3K
    6: "D",   # Fault: ventilation required
    7: "E",   # Fault: CP-PE short circuit
    8: "F",   # Fault: RCMU leakage or self-test failure
    9: "F",   # Fault: EV charging socket fault
    10: "F",  # Fault: PP wire split
    11: "F",  # Fault: electronic lock disabled
}

# Status 5 is the charging state (contactor closed)
EKEPC2_CHARGING_STATUS = 5


class EKEPC2Bridge:
    def __init__(self):
        self._session = RuntimeSession()
        m = Module(self._session)
        log.update_process_name(m.info.id)
        self._setup = m.say_hello()

        # Read config
        cfg = self._setup.configs.module
        self._device_id = cfg['device_id']
        self._ext_host = cfg['ext_broker_host']
        self._ext_port = cfg['ext_broker_port']
        self._ext_user = cfg['ext_broker_user']
        self._ext_pass = cfg['ext_broker_pass']
        self._max_current = cfg['max_current_A']
        self._num_phases = cfg['num_phases']
        self._staleness_timeout = cfg['staleness_timeout_s']

        # MQTT topics for ESP32
        self._topic_telemetry = f"evse/{self._device_id}/evse"
        self._topic_cmd = f"evse/{self._device_id}/cmd"
        self._topic_ack = f"evse/{self._device_id}/cmd_ack"

        # State tracking
        self._lock = threading.Lock()
        self._last_status = -1
        self._last_was_charging = False
        self._last_telemetry_time = 0.0
        self._ext_connected = False
        self._enabled = False
        self._cached_pwm_duty = 0.0
        self._telemetry = {}

        # Register command handlers for evse_board_support
        for cmd in m.implementations['board_support'].commands:
            handler = getattr(self, f'_handler_{cmd}', None)
            if handler:
                m.implement_command('board_support', cmd, handler)
            else:
                log.warning(f"No handler for board_support command: {cmd}")

        # Register powermeter commands if any
        if 'powermeter' in m.implementations:
            for cmd in m.implementations['powermeter'].commands:
                handler = getattr(self, f'_handler_pm_{cmd}', None)
                if handler:
                    m.implement_command('powermeter', cmd, handler)

        # External MQTT client (to ESP32 broker, NOT EVerest internal)
        self._ext_client = mqtt.Client(
            client_id=f"everest-ekepc2-{self._device_id}",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2
        )
        self._ext_client.username_pw_set(self._ext_user, self._ext_pass)
        self._ext_client.on_connect = self._on_ext_connect
        self._ext_client.on_disconnect = self._on_ext_disconnect
        self._ext_client.on_message = self._on_ext_message
        self._ext_client.reconnect_delay_set(min_delay=1, max_delay=30)

        self._mod = m
        m.init_done(self._ready)

    def _ready(self):
        log.info(f"EKEPC2Bridge ready. Device: {self._device_id}, Broker: {self._ext_host}:{self._ext_port}")

        # Publish hardware capabilities
        caps = {
            "max_current_A_import": float(self._max_current),
            "min_current_A_import": 6.0,
            "max_phase_count_import": self._num_phases,
            "min_phase_count_import": 1,
            "max_current_A_export": 0.0,
            "min_current_A_export": 0.0,
            "max_phase_count_export": 1,
            "min_phase_count_export": 1,
            "supports_changing_phases_during_charging": False,
            "supports_cp_state_E": False,
            "connector_type": "IEC62196Type2Cable"
        }
        self._mod.publish_variable('board_support', 'capabilities', caps)
        self._mod.publish_variable('board_support', 'ac_nr_of_phases_available', self._num_phases)
        self._mod.publish_variable('board_support', 'event', {"event": "A"})

        # Connect to external MQTT broker
        try:
            self._ext_client.connect_async(self._ext_host, self._ext_port)
            self._ext_client.loop_start()
        except Exception as e:
            log.error(f"Failed to start external MQTT: {e}")

        # Start staleness watchdog thread
        watchdog = threading.Thread(target=self._staleness_watchdog, daemon=True)
        watchdog.start()

    # ---- External MQTT callbacks (run on paho network thread) ----

    def _on_ext_connect(self, client, userdata, flags, reason_code, properties=None):
        log.info(f"Connected to ESP32 broker: {reason_code}")
        client.subscribe(self._topic_telemetry, qos=1)
        client.subscribe(self._topic_ack, qos=1)
        with self._lock:
            self._ext_connected = True

    def _on_ext_disconnect(self, client, userdata, flags, reason_code, properties=None):
        log.warning(f"Disconnected from ESP32 broker: {reason_code}")
        with self._lock:
            self._ext_connected = False

    def _on_ext_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.error(f"Bad payload on {msg.topic}: {e}")
            return

        if msg.topic == self._topic_telemetry:
            self._process_telemetry(payload)
        elif msg.topic == self._topic_ack:
            log.info(f"CMD ack: {payload}")

    # ---- Telemetry processing ----

    def _process_telemetry(self, data):
        with self._lock:
            self._last_telemetry_time = time.monotonic()
            self._telemetry = data

        status = data.get('current_status', -1)
        is_charging = data.get('is_charging', False)
        poll_ok = data.get('poll_success', False)

        if not poll_ok:
            return  # stale data, don't update state

        # Map status to CP event
        cp_event = EKEPC2_STATUS_TO_CP.get(status, "F")

        if status != self._last_status:
            log.info(f"EKEPC2 status: {self._last_status} -> {status} ({data.get('status_string', '')})")
            self._mod.publish_variable('board_support', 'event', {"event": cp_event})

            # Detect contactor transitions (PowerOn/PowerOff)
            if is_charging and not self._last_was_charging:
                log.info("Contactor closed -> PowerOn")
                self._mod.publish_variable('board_support', 'event', {"event": "PowerOn"})
            elif not is_charging and self._last_was_charging:
                log.info("Contactor opened -> PowerOff")
                self._mod.publish_variable('board_support', 'event', {"event": "PowerOff"})

            self._last_status = status
            self._last_was_charging = is_charging

        # Publish telemetry
        telemetry = {
            "evse_temperature_C": float(data.get('current_temperature', 0)),
            "fan_rpm": 0.0,
            "supply_voltage_12V": 12.0,
            "supply_voltage_minus_12V": -12.0,
            "relais_on": is_charging
        }
        self._mod.publish_variable('board_support', 'telemetry', telemetry)

        # Publish powermeter
        self._publish_powermeter(data)

    def _publish_powermeter(self, data):
        voltage = float(data.get('charging_voltage', data.get('meter_a_voltage', 0)))
        current = float(data.get('charging_current', data.get('meter_current', 0)))
        power = float(data.get('charging_power', data.get('meter_total_power', 0)))
        energy_kwh = float(data.get('meter_total_kwh', 0))

        pm = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "meter_id": f"EKEPC2-{self._device_id}",
            "energy_Wh_import": {"total": energy_kwh * 1000.0},
            "power_W": {"total": power},
            "voltage_V": {"L1": voltage},
            "current_A": {"L1": current},
        }
        self._mod.publish_variable('powermeter', 'powermeter', pm)

    def _staleness_watchdog(self):
        while True:
            time.sleep(3.0)
            with self._lock:
                elapsed = time.monotonic() - self._last_telemetry_time
                connected = self._ext_connected
                has_data = self._last_telemetry_time > 0

            if connected and has_data and elapsed > self._staleness_timeout:
                log.error(f"ESP32 telemetry stale ({elapsed:.1f}s), publishing error state")
                self._mod.publish_variable('board_support', 'event', {"event": "F"})
                self._last_status = -1  # force re-publish on recovery

    # ---- Send command to ESP32 ----

    def _send_cmd(self, cmd_dict):
        with self._lock:
            connected = self._ext_connected
        if not connected:
            log.error(f"Cannot send command, ESP32 broker not connected: {cmd_dict}")
            return
        self._ext_client.publish(self._topic_cmd, json.dumps(cmd_dict), qos=1)
        log.info(f"Sent to ESP32: {cmd_dict}")

    # ---- evse_board_support command handlers ----

    def _handler_enable(self, args):
        value = args['value']
        log.info(f"enable({value})")
        with self._lock:
            self._enabled = value
        if value:
            self._send_cmd({"cmd": "enable"})
            # Apply cached PWM if we had one
            if self._cached_pwm_duty > 0:
                reg_value = int(self._cached_pwm_duty * 100)
                self._send_cmd({"cmd": "set_pwm", "value": reg_value})
        else:
            self._send_cmd({"cmd": "disable"})

    def _handler_pwm_on(self, args):
        duty_pct = args['value']  # 0-100 percentage
        log.info(f"pwm_on({duty_pct}%)")
        self._cached_pwm_duty = duty_pct
        with self._lock:
            enabled = self._enabled
        if enabled:
            # Convert percentage to EKEPC2 register 109 value (0.01% units)
            reg_value = int(duty_pct * 100)
            self._send_cmd({"cmd": "set_pwm", "value": reg_value})

    def _handler_cp_state_X1(self, args):
        log.info("cp_state_X1 - constant high voltage")
        self._cached_pwm_duty = 100.0
        self._send_cmd({"cmd": "set_pwm", "value": 10000})

    def _handler_cp_state_F(self, args):
        log.info("cp_state_F - error state")
        self._cached_pwm_duty = 0
        self._send_cmd({"cmd": "set_pwm", "value": 0})
        self._send_cmd({"cmd": "stop"})

    def _handler_cp_state_E(self, args):
        log.info("cp_state_E - CP to 0V (not fully supported by EKEPC2)")
        self._cached_pwm_duty = 0
        self._send_cmd({"cmd": "set_pwm", "value": 0})

    def _handler_allow_power_on(self, args):
        value = args['value']
        allow = value['allow_power_on']
        reason = value['reason']
        log.info(f"allow_power_on({allow}, reason={reason})")
        if allow:
            self._send_cmd({"cmd": "start"})
        else:
            self._send_cmd({"cmd": "stop"})

    def _handler_ac_switch_three_phases_while_charging(self, args):
        log.warning("Phase switching not supported by EKEPC2, ignoring")

    def _handler_ac_set_overcurrent_limit_A(self, args):
        limit_a = args['value']
        log.info(f"ac_set_overcurrent_limit_A({limit_a})")
        # EKEPC2 register 88 is overcurrent percentage (relative to rated current)
        # This is an approximation -- convert absolute amps to percentage of max
        if self._max_current > 0:
            pct = int((limit_a / self._max_current) * 100)
            self._send_cmd({"cmd": "write_register", "reg": 88, "value": pct})

    # ---- powermeter command handlers ----

    def _handler_pm_start_transaction(self, args):
        log.info("powermeter: start_transaction")
        return {
            "status": "NOT_SUPPORTED",
            "error": "EKEPC2 does not support signed metering"
        }

    def _handler_pm_stop_transaction(self, args):
        log.info(f"powermeter: stop_transaction (id: {args.get('transaction_id', '')})")
        return {
            "status": "NOT_SUPPORTED",
            "error": "EKEPC2 does not support signed metering"
        }


# Module entry point
bridge = EKEPC2Bridge()
while True:
    time.sleep(1)
