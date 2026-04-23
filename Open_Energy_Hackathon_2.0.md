# EVerest + MeshEMS Integration — EIOT Hackathon (April 22–23, 2026)

## Overview

This work connects an EKEPC2 EV charging controller to the EVerest charging framework via an ESP32 microcontroller, and integrates solar energy production into the charging pipeline. An external Energy Management System can connect to EVerest over Modbus TCP, discover it as a SunSpec-compliant power source, and manage it as an asset in its grid.

## Architecture

```
                         ┌───────────────────────┐
                         │  EKEPC2 EVSE          │
                         │  (charging controller)│
                         └──────────┬────────────┘
                                    │ RS-485 Modbus RTU
                                    ▼
                         ┌───────────────────────┐
                         │  ESP32-S3 (meshems)   │
                         │  Firmware on hardware │
                         └──────────┬────────────┘
                                    │ WiFi → MQTT
                                    ▼
                    ┌────────────────────────────────────┐
                    │  External MQTT Broker              │
                    │  (192.168.95.83:1883)              │
                    │  Topics: evse/{device_id}/evse     │
                    │          evse/{device_id}/cmd      │
                    └───────────────────┬────────────────┘
                                        │
         ┌──────────────────────────────┼──────────────────────────┐
         │                  EVerest (Docker)                       │
         │                                                         │
         │  ┌─────────────────────┐                                │
         │  │ PyEKEPC2Bridge      │  Subscribes to external MQTT   │
         │  │ (board_support +    │  Publishes to EVerest internal  │
         │  │  powermeter)        │  MQTT as board_support events   │
         │  └────┬──────────┬─────┘  and powermeter data            │
         │       │          │                                      │
         │       ▼          │                                      │
         │  ┌───────────┐   │   ┌──────────────────────┐           │
         │  │EvseManager│   │   │ SunSpec Bridge Server │           │
         │  │(connector)│   └──▶│ (Modbus TCP :502)     │◀──────────┼──  External EMS
         │  └─────┬─────┘       │ Model 1 + Model 101   │           │    polls SunSpec
         │        │             └──────────────────────┘           │    registers
         │        │ energy_grid                                    │
         │        ▼                                                │
         │  ┌──────────────────┐     ┌────────────────────────┐     │
         │  │ EnergyNode       │◀─── │ PySunSpecSolarClient   │     │
         │  │ (grid connection │      │ (reads solar inverter  │     │
         │  │  point, 32A/1φ)  │    │  via Modbus TCP :8502)  │     │
         │  └─────┬────────────┘    └─────┬──────────┬────────┘     │
         │        │                       │          │               │
         │        ▼                       │   publishes to           │
         │  ┌───────────────┐             │   ems/solar/state        │
         │  │ EnergyManager │             │          │               │
         │  │ (adjusts EVSE │             ▼          ▼               │
         │  │  current)     │   ┌──────────────┐  ┌────────────┐    │
         │  └───────────────┘   │ Sol-Ark via  │  │ External   │    │
         │                      │ ESP32 gateway│  │ MQTT Broker│    │
         │                      │ :8502        │  └──────┬─────┘    │
         │                      └──────────────┘         │          │
         └───────────────────────────────────────────────┼──────────┘
                                                         │
                                                         ▼
                                              ┌────────────────────┐
                                              │ EMS Dashboard      │
                                              │ http://localhost   │
                                              │ :8080              │
                                              └────────────────────┘
```

## What Was Built

### 1. ESP32 → EVerest MQTT Bridge (`PyEKEPC2Bridge`)

**Files:** `modules/HardwareDrivers/EVSE/PyEKEPC2Bridge/`

The ESP32 (meshems firmware) polls the EKEPC2 EVSE controller over RS-485 Modbus RTU every 5 seconds and publishes JSON telemetry to an external MQTT broker. The `PyEKEPC2Bridge` EVerest module subscribes to this telemetry and translates it into EVerest's internal interfaces:

- **EKEPC2 status codes (0–11) → IEC 61851 CP states (A/B/C/D/E/F)**
- **Charging voltage/current/power/energy → EVerest `powermeter` variable**
- **EVerest commands (enable, PWM, start/stop) → MQTT JSON commands back to ESP32**

MQTT topics:
| Direction | Topic | Content |
|-----------|-------|---------|
| ESP32 → EVerest | `evse/{device_id}/evse` | Charging status, V, A, W, kWh, temperature |
| EVerest → ESP32 | `evse/{device_id}/cmd` | `{"cmd": "start\|stop\|enable\|disable\|set_pwm\|write_register"}` |
| ESP32 → EVerest | `evse/{device_id}/cmd_ack` | Command acknowledgment |

### 2. SunSpec Modbus TCP Bridge (`sunspec-bridge`)

**Files:** `applications/containers/sunspec-bridge/sunspec_server.py`

A Python service that subscribes to EVerest's internal MQTT and exposes the EVSE as a SunSpec-compliant device over Modbus TCP (port 502). An external EMS connects as a Modbus TCP client, discovers the SunSpec marker, and reads the charger as a single-phase AC power source.

Register layout at Modbus address 40001:
| Offset | Content | Description |
|--------|---------|-------------|
| 0–1 | `"SunS"` | SunSpec discovery marker |
| 2–69 | Model 1 | Common block (manufacturer, model, serial) |
| 70–121 | Model 101 | Single-phase inverter (V, A, W, Hz, PF, Wh, state, temp) |
| 122–123 | End | `0xFFFF, 0x0000` |

Live data fields served in Model 101:
- Phase A voltage and current (from EVSE meter)
- AC power (total watts)
- Frequency
- Lifetime energy (Wh)
- Cabinet temperature
- Operating state (mapped from CP state: Off/Starting/MPPT/Fault)

### 3. SunSpec Solar Client (`PySunSpecSolarClient`)

**Files:** `modules/HardwareDrivers/PowerMeters/PySunSpecSolarClient/`

An EVerest module that reads a SunSpec-compliant solar inverter over Modbus TCP and feeds the available solar power into EVerest's energy tree as external limits. This allows the EnergyManager to dynamically adjust the EVSE charging current based on how much solar power is available.

- Polls solar inverter every 3 seconds via Modbus TCP
- Reads SunSpec Model 701 (DER AC Measurement) for solar power, voltage, current
- Reads SunSpec Model 713 (Battery) for battery SoC, rated/available Wh
- Calls `set_external_limits` on the grid connection EnergyNode
- **Republishes solar state to external MQTT** (`ems/solar/state`) for the dashboard, including:
  - `power_w`, `voltage`, `current`, `state`, `inv_state`, `producing`
  - `battery_soc`, `battery_wh_rated`, `battery_wh_avail` (when Model 713 is present)

Target inverter: Sol-Ark via ESP32 SunSpec gateway at `192.168.94.229:8502`

### 4. Solar Inverter Simulator (`sunspec_solar_sim.py`)

**Files:** `applications/containers/sunspec-bridge/sunspec_solar_sim.py`

A test tool that simulates a solar inverter producing variable power on a sine curve (day cycle compressed to minutes). Serves SunSpec Model 1 + Model 101 on Modbus TCP port 5020. Used for testing the PySunSpecSolarClient without real solar hardware.

### 5. Live EMS Dashboard

**Files:** `applications/dashboard/`

A Python web dashboard (served on `http://localhost:8080`) that visualizes live system state by subscribing to MQTT topics. Purely a subscriber — no conflicts with EVerest's polling.

MQTT topics consumed:
| Topic | Publisher | Content |
|-------|-----------|---------|
| `ems/solar/state` | PySunSpecSolarClient | Solar power, battery SoC |
| `evse/{device_id}/evse` | ESP32 firmware | EVSE charging state, current, power |
| `evse/{device_id}/cmd` | PyEKEPC2Bridge | Commands sent to EVSE |
| `evse/{device_id}/cmd_ack` | ESP32 firmware | Command acknowledgements |

What it shows:
- **Solar Production** — current PV output in watts, inverter state
- **Battery** — SoC percentage with fill bar, kWh available/rated
- **EV Charger** — state (idle/connected/charging), current draw, temperature
- **Charge Limit** — amps EVerest is allowing the EV to draw
- **Time-series chart** — solar vs EV charging overlaid, battery SoC over last ~8 minutes

To run:
```bash
cd applications/dashboard
pip install -r requirements.txt
python dashboard.py
# Open http://localhost:8080
```

### 6. ESP32 Firmware Changes (meshems)

**Repo:** `github.com/energy-iot/meshems` branch `eiot_hackathon`

- Fixed inverted `strstr` NULL checks in MQTT subscriber callback
- Set up MQTT publish of full EVSE telemetry (status, charging data, meter readings, dial settings)
- Implemented command queue for deferred MQTT command execution (start/stop/enable/disable/set_pwm/write_register)
- Configured to connect to hackathon MQTT broker

## EVerest Configuration

**File:** `config/config-ekepc2.yaml`

Module wiring:
```
ekepc2_bridge (PyEKEPC2Bridge)
  ├── board_support  ──▶  connector_1 (EvseManager) bsp
  └── powermeter     ──▶  connector_1 (EvseManager) powermeter_grid_side

connector_1 (EvseManager)
  └── energy_grid    ──▶  grid_connection_point (EnergyNode) energy_consumer

grid_connection_point (EnergyNode, 32A/1-phase fuse)
  └── external_limits ◀── solar_client (PySunSpecSolarClient)

energy_manager (EnergyManager)
  └── energy_trunk   ──▶  grid_connection_point energy_grid
```

Key settings:
- Device ID: `EKEPC2-DDS238_A4E8F8`
- External MQTT broker: `host.docker.internal:1883`
- Max charging current: 32A, single phase
- Solar inverter: `192.168.94.229:8502` (Sol-Ark via ESP32 SunSpec gateway)
- Solar MQTT republish topic: `ems/solar/state`

## Docker Services

Defined in `.devcontainer/docker-compose.yml` (profiles: `all`, `sil`):

| Service | Port | Purpose |
|---------|------|---------|
| `mqtt-server` | 1883, 9001 | Mosquitto MQTT broker (EVerest internal) |
| `sunspec-bridge` | 502 | SunSpec Modbus TCP server for external EMS |
| `mqtt-explorer` | 4000 | MQTT topic browser |
| `nodered` | 1880 | Node-RED dashboard |

## How to Run

```bash
# Start all services
cd .devcontainer
docker compose --profile sil up --build -d

# Verify sunspec-bridge is connected
docker compose --profile sil logs sunspec-bridge

# Test SunSpec registers from host
pip install pymodbus
python applications/containers/sunspec-bridge/test_sunspec.py

# Watch MQTT traffic
docker compose --profile sil exec mqtt-server mosquitto_sub -t "everest/#" -v
```

## Protocol Flow Summary

| Hop | Protocol | From | To |
|-----|----------|------|----|
| 1 | Modbus RTU (RS-485) | EKEPC2 EVSE | ESP32 (EVSE bridge) |
| 2 | MQTT JSON (WiFi) | ESP32 | External MQTT broker |
| 3 | MQTT JSON (Docker network) | External broker | PyEKEPC2Bridge |
| 4 | EVerest internal MQTT | PyEKEPC2Bridge | EvseManager + SunSpec bridge |
| 5 | Modbus TCP (port 502) | External EMS (master) | SunSpec bridge (server) |
| 6 | Modbus TCP (port 8502) | PySunSpecSolarClient (master) | Sol-Ark via ESP32 gateway |
| 7 | EVerest `set_external_limits` | PySunSpecSolarClient | EnergyNode → EnergyManager |
| 8 | MQTT JSON (`ems/solar/state`) | PySunSpecSolarClient | External MQTT broker |
| 9 | MQTT subscribe | External broker | EMS Dashboard (http://localhost:8080) |

## Contributors

- Souvik Banerjee
- Sunita Bhattacharya
