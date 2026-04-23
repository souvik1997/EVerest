# EMS Dashboard

Single-file web dashboard showing live solar, battery, and EV charging state.

**All data comes from MQTT** -- the dashboard is purely a subscriber. This
avoids conflicts with EVerest's solar_client polling the same SunSpec gateway.

## Topics consumed

| Topic | Publisher | What |
|---|---|---|
| `ems/solar/state` | EVerest PySunSpecSolarClient | Solar power + battery SoC JSON |
| `evse/<device>/evse` | ESP32 EKEPC2 firmware | EVSE telemetry (charging state, current, power) |
| `evse/<device>/cmd` | EVerest PyEKEPC2Bridge | Commands being sent to the EVSE |
| `evse/<device>/cmd_ack` | ESP32 EKEPC2 firmware | Command acknowledgements |

## Run

```bash
cd applications/dashboard
pip install -r requirements.txt
python dashboard.py
```

Then open http://localhost:8080

## Configuration

Override defaults with env vars:

| Variable | Default | Description |
|---|---|---|
| `MQTT_HOST` | `localhost` | MQTT broker |
| `MQTT_PORT` | `1883` | |
| `MQTT_USER` | `mqttuser` | |
| `MQTT_PASS` | `sunspec2025` | |
| `DEVICE_ID` | `EKEPC2-DDS238_A4E8F8` | ESP32 device ID for topic filtering |
| `SOLAR_TOPIC` | `ems/solar/state` | Topic the solar client publishes to |
| `HTTP_PORT` | `8080` | Where to serve the dashboard |

## What it shows

- **Solar Production** -- current PV output in watts + inverter state
- **Battery** -- SoC percentage with fill bar + kWh available/rated
- **EV Charger** -- state pill (IDLE/CONNECTED/CHARGING) + current draw + temperature
- **Charge Limit** -- amps EVerest is allowing the EV to draw + equivalent watts
- **Time-series chart** -- solar vs EV charging overlaid + battery SoC over last ~8 minutes

## How the data flows

```
Sol-Ark <- RS485 -- ESP32 gateway -- SunSpec Modbus TCP -->  EVerest solar_client
                                                                    |
                                                           publishes ems/solar/state
                                                                    v
EKEPC2 <- RS485 -- ESP32 EVSE bridge -- MQTT (evse/*) --------+
                                                              v
                                                          Mac's mosquitto
                                                              ^
                                                              |
                                                     subscribes to everything
                                                              |
                                                      EMS Dashboard (you)
```

The solar_client is the single source of truth for SunSpec data. The dashboard
just subscribes to its MQTT output.
