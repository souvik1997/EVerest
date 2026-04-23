"""
EMS Dashboard -- single-file FastAPI web app showing live solar, battery,
and EV charging state.

Data sources (all from MQTT -- no duplicate polling of the SunSpec gateway):
- ems/solar/state      (published by EVerest's PySunSpecSolarClient)
- evse/<device>/evse    (ESP32 EVSE telemetry)
- evse/<device>/cmd     (EVerest commands to the EVSE)
- evse/<device>/cmd_ack (ESP32 acks)

Run:
    pip install -r requirements.txt
    python dashboard.py

Then open http://localhost:8080
"""

import asyncio
import json
import os
import threading
import time
from collections import deque
from typing import Set

import paho.mqtt.client as mqtt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

# Configuration
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "mqttuser")
MQTT_PASS = os.environ.get("MQTT_PASS", "sunspec2025")
DEVICE_ID = os.environ.get("DEVICE_ID", "EKEPC2-DDS238_A4E8F8")
SOLAR_TOPIC = os.environ.get("SOLAR_TOPIC", "ems/solar/state")
HISTORY_POINTS = 240  # 8 minutes at 2s interval
SAMPLE_INTERVAL_S = 2.0
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))


# ---- Shared state ----
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.solar = {
            "pv_W": 0,
            "battery_soc": None,
            "battery_wh_avail": None,
            "battery_wh_rated": None,
            "inverter_state": None,
            "grid_connected": False,
            "voltage": 0,
            "frequency": 0,
            "updated": 0,
        }
        self.evse = {
            "status": None,
            "status_str": "",
            "is_charging": False,
            "is_connected": False,
            "charging_current": 0,
            "charging_voltage": 0,
            "charging_power": 0,
            "temperature": 0,
            "max_output_pwm_pct": 0,
            "updated": 0,
        }
        self.evse_limit_A = None
        self.last_cmd = None
        self.history = deque(maxlen=HISTORY_POINTS)

    def snapshot(self):
        with self.lock:
            return {
                "solar": dict(self.solar),
                "evse": dict(self.evse),
                "evse_limit_A": self.evse_limit_A,
                "last_cmd": self.last_cmd,
                "history": list(self.history),
            }

    def record_history(self):
        with self.lock:
            self.history.append({
                "t": int(time.time() * 1000),
                "pv_W": self.solar["pv_W"],
                "evse_W": self.evse["charging_power"],
                "evse_limit_A": self.evse_limit_A or 0,
                "battery_soc": self.solar.get("battery_soc") or 0,
            })


state = State()


# ---- MQTT subscriber (single data source) ----
def start_mqtt():
    def on_connect(client, userdata, flags, rc, properties=None):
        print(f"[MQTT] connected: {rc}")
        client.subscribe(SOLAR_TOPIC)
        client.subscribe(f"evse/{DEVICE_ID}/evse")
        client.subscribe(f"evse/{DEVICE_ID}/cmd")
        client.subscribe(f"evse/{DEVICE_ID}/cmd_ack")

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
        except Exception:
            return

        topic = msg.topic
        if topic == SOLAR_TOPIC:
            with state.lock:
                state.solar.update({
                    "pv_W": data.get("power_w", 0),
                    "battery_soc": data.get("battery_soc"),
                    "battery_wh_avail": data.get("battery_wh_avail"),
                    "battery_wh_rated": data.get("battery_wh_rated"),
                    "inverter_state": data.get("inv_state"),
                    "voltage": data.get("voltage", 0),
                    "updated": time.time(),
                })
        elif topic.endswith("/evse"):
            cur_pwm = data.get("current_output_pwm", 0) / 100.0  # 0.01% -> %
            max_pwm = data.get("max_output_pwm_duty", 0) / 100.0
            # IEC 61851: duty 10-85% -> amps = duty * 0.6
            #            duty 85-96% -> amps = (duty - 64) * 2.5
            def duty_to_amps(duty):
                if duty is None:
                    return None
                if 10 <= duty <= 85:
                    return duty * 0.6
                if 85 < duty <= 96:
                    return (duty - 64) * 2.5
                return None  # X1 (100%) or <10% = not actively signaling
            with state.lock:
                state.evse.update({
                    "status": data.get("current_status"),
                    "status_str": data.get("status_string", ""),
                    "is_charging": data.get("is_charging", False),
                    "is_connected": data.get("is_connected", False),
                    "charging_current": data.get("charging_current", 0),
                    "charging_voltage": data.get("charging_voltage", 0),
                    "charging_power": data.get("charging_power", 0),
                    "temperature": data.get("current_temperature", 0),
                    "max_output_pwm_pct": max_pwm,
                    "current_output_pwm_pct": cur_pwm,
                    "cp_active": 10 <= cur_pwm <= 96,  # True when actively signaling
                    "updated": time.time(),
                })
                # If the EVSE is actively signaling a current budget, use that.
                from_pwm = duty_to_amps(cur_pwm)
                if from_pwm is not None:
                    state.evse_limit_A = from_pwm
        elif topic.endswith("/cmd"):
            with state.lock:
                state.last_cmd = data
                if data.get("cmd") == "set_pwm":
                    duty_pct = data.get("value", 0) / 100.0
                    if 10 <= duty_pct <= 85:
                        state.evse_limit_A = duty_pct * 0.6
                elif data.get("cmd") == "write_register" and data.get("reg") == 88:
                    # ac_set_overcurrent_limit_A mapping (% of max rated current)
                    pct = data.get("value", 0)
                    state.evse_limit_A = pct * 32 / 100

    c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="ems-dashboard")
    if MQTT_USER:
        c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.on_connect = on_connect
    c.on_message = on_message
    c.reconnect_delay_set(min_delay=1, max_delay=30)
    c.connect_async(MQTT_HOST, MQTT_PORT)
    c.loop_start()
    return c


def history_recorder():
    while True:
        state.record_history()
        time.sleep(SAMPLE_INTERVAL_S)


# ---- FastAPI ----
app = FastAPI(title="EMS Dashboard")
connections: Set[WebSocket] = set()


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EVerest + MeshEMS — Live Dashboard</title>
<style>
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --dim: #8b949e;
    --green: #3fb950;
    --yellow: #d29922;
    --red: #f85149;
    --blue: #58a6ff;
    --orange: #d18616;
    --purple: #bc8cff;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 20px;
  }
  h1 { font-size: 1.4em; margin-bottom: 4px; }
  .subtitle { color: var(--dim); font-size: 0.85em; margin-bottom: 20px; }
  .status-bar {
    display: flex; gap: 16px; align-items: center;
    margin-bottom: 20px; font-size: 0.8em;
  }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    display: inline-block; margin-right: 6px;
  }
  .dot-green { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot-red { background: var(--red); box-shadow: 0 0 6px var(--red); }
  .dot-yellow { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }

  .arch-flow {
    display: flex; align-items: center; justify-content: center;
    gap: 0; margin-bottom: 28px; flex-wrap: wrap;
  }
  .arch-node {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 16px;
    min-width: 140px; text-align: center;
  }
  .arch-node-label { font-size: 0.7em; color: var(--dim); margin-bottom: 4px; }
  .arch-node-value { font-size: 1.3em; font-weight: bold; }
  .arch-node-sub { font-size: 0.7em; color: var(--dim); margin-top: 4px; }
  .arch-arrow {
    font-size: 1.4em; color: var(--dim); padding: 0 8px;
    display: flex; flex-direction: column; align-items: center;
    min-width: 60px; transition: color 0.3s;
  }
  .arch-arrow-label { font-size: 0.5em; color: var(--dim); }
  .arch-arrow.active { color: var(--green); }
  .arch-arrow.active .arch-arrow-label { color: var(--green); }

  .node-solar { border-color: var(--yellow); }
  .node-solar .arch-node-value { color: var(--yellow); }
  .node-evse { border-color: var(--blue); }
  .node-evse .arch-node-value { color: var(--blue); }
  .node-everest { border-color: var(--purple); }
  .node-everest .arch-node-value { color: var(--purple); }
  .node-battery { border-color: var(--green); }
  .node-battery .arch-node-value { color: var(--green); }

  .panels {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 16px; margin-bottom: 20px;
  }
  .panel {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px 20px;
  }
  .panel-title {
    font-size: 0.85em; color: var(--dim); margin-bottom: 12px;
    display: flex; align-items: center;
  }
  .panel-icon { margin-right: 6px; font-size: 1.2em; }
  .metric-row {
    display: flex; justify-content: space-between;
    padding: 6px 0; font-size: 0.9em;
  }
  .metric-label { color: var(--dim); }
  .metric-value { font-weight: bold; font-size: 1.1em; }
  .metric-unit { color: var(--dim); font-size: 0.85em; margin-left: 4px; }

  .state-badge {
    display: inline-block; padding: 3px 10px; border-radius: 12px;
    font-size: 0.75em; font-weight: bold;
  }
  .state-off { background: #21262d; color: var(--dim); border: 1px solid var(--border); }
  .state-ready { background: rgba(88,166,255,0.15); color: var(--blue); border: 1px solid var(--blue); }
  .state-charging { background: rgba(63,185,80,0.15); color: var(--green); border: 1px solid var(--green); }
  .state-producing { background: rgba(210,153,34,0.15); color: var(--yellow); border: 1px solid var(--yellow); }
  .state-fault { background: rgba(248,81,73,0.15); color: var(--red); border: 1px solid var(--red); }

  .power-bar-container { margin-top: 10px; }
  .power-bar-bg { background: #0d1117; border: 1px solid var(--border); border-radius: 4px; height: 20px; position: relative; overflow: hidden; }
  .power-bar-fill { height: 100%; transition: width 0.4s ease; display: flex; align-items: center; padding: 0 6px; font-size: 0.7em; font-weight: bold; color: #fff; white-space: nowrap; }
  .power-bar-label { font-size: 0.7em; color: var(--dim); margin-top: 4px; display: flex; justify-content: space-between; }

  .chart-wrap { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 20px; }
  #chart { height: 260px !important; }
</style>
</head>
<body>

<h1>EVerest + MeshEMS — Live Dashboard</h1>
<div class="subtitle">Solar-aware EV charging · SunSpec Modbus · EKEPC2 over MQTT · EVerest energy tree</div>

<div class="status-bar">
  <span><span class="status-dot dot-yellow" id="dot-ws"></span><span id="ws-status">connecting</span></span>
  <span id="msg-count">0 updates</span>
  <span id="last-update" style="color: var(--dim);">—</span>
</div>

<div class="arch-flow">
  <div class="arch-node node-solar">
    <div class="arch-node-label">SOLAR (Sol-Ark)</div>
    <div class="arch-node-value" id="arch-solar">— W</div>
    <div class="arch-node-sub" id="arch-solar-sub">—</div>
  </div>

  <div class="arch-arrow" id="arrow-solar">
    <div class="arch-arrow-label">SunSpec 701</div>
    <div>&#x2192;</div>
  </div>

  <div class="arch-node node-everest">
    <div class="arch-node-label">EVEREST</div>
    <div class="arch-node-value" id="arch-limit">— A</div>
    <div class="arch-node-sub">energy manager</div>
  </div>

  <div class="arch-arrow" id="arrow-evse">
    <div class="arch-arrow-label">MQTT</div>
    <div>&#x2192;</div>
  </div>

  <div class="arch-node node-evse">
    <div class="arch-node-label">EVSE (EKEPC2)</div>
    <div class="arch-node-value" id="arch-evse">— W</div>
    <div class="arch-node-sub" id="arch-evse-sub">waiting...</div>
  </div>

  <div class="arch-arrow" id="arrow-battery">
    <div class="arch-arrow-label">battery</div>
    <div>&#x2193;</div>
  </div>

  <div class="arch-node node-battery">
    <div class="arch-node-label">BATTERY</div>
    <div class="arch-node-value" id="arch-soc">—%</div>
    <div class="arch-node-sub" id="arch-bat-sub">—</div>
  </div>
</div>

<div class="panels">

  <div class="panel">
    <div class="panel-title"><span class="panel-icon">&#9788;</span> Solar Production</div>
    <div class="metric-row">
      <span class="metric-label">Power</span>
      <span><span class="metric-value" id="solar-power">—</span><span class="metric-unit">W</span></span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Voltage</span>
      <span><span class="metric-value" id="solar-voltage">—</span><span class="metric-unit">V</span></span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Inverter</span>
      <span id="solar-status"><span class="state-badge state-off">unknown</span></span>
    </div>
    <div class="power-bar-container">
      <div class="power-bar-bg">
        <div class="power-bar-fill" id="solar-bar" style="width:0%; background: linear-gradient(90deg, var(--orange), var(--yellow));">0 W</div>
      </div>
      <div class="power-bar-label"><span>0 W</span><span>5000 W peak</span></div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title"><span class="panel-icon">&#9889;</span> EVSE Charging</div>
    <div class="metric-row">
      <span class="metric-label">Power</span>
      <span><span class="metric-value" id="evse-power">—</span><span class="metric-unit">W</span></span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Voltage</span>
      <span><span class="metric-value" id="evse-voltage">—</span><span class="metric-unit">V</span></span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Current</span>
      <span><span class="metric-value" id="evse-current">—</span><span class="metric-unit">A</span></span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Temperature</span>
      <span><span class="metric-value" id="evse-temp">—</span><span class="metric-unit">&deg;C</span></span>
    </div>
    <div class="metric-row">
      <span class="metric-label">State</span>
      <span id="evse-cp-state"><span class="state-badge state-off">—</span></span>
    </div>
    <div class="power-bar-container">
      <div class="power-bar-bg">
        <div class="power-bar-fill" id="evse-bar" style="width:0%; background: linear-gradient(90deg, #1f6feb, var(--blue));">0 W</div>
      </div>
      <div class="power-bar-label"><span>0 W</span><span>7680 W (32A &#215; 240V)</span></div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title"><span class="panel-icon">&#9879;</span> Energy Manager</div>
    <div class="metric-row">
      <span class="metric-label">Solar available</span>
      <span><span class="metric-value" id="em-solar-limit">—</span><span class="metric-unit">W</span></span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Max current</span>
      <span><span class="metric-value" id="em-max-current">—</span><span class="metric-unit">A</span></span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Fuse limit</span>
      <span><span class="metric-value">32.0</span><span class="metric-unit">A</span></span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Phases</span>
      <span><span class="metric-value">1</span></span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Solar surplus</span>
      <span><span class="metric-value" id="em-net-power">—</span><span class="metric-unit">W</span></span>
    </div>
    <div class="power-bar-container">
      <div class="power-bar-bg">
        <div class="power-bar-fill" id="net-bar" style="width:50%; background: var(--dim);">+0 W</div>
      </div>
      <div class="power-bar-label"><span>EV draws battery</span><span>solar charges battery</span></div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title"><span class="panel-icon">&#128267;</span> Battery</div>
    <div class="metric-row">
      <span class="metric-label">State of Charge</span>
      <span><span class="metric-value" id="bat-soc">—</span><span class="metric-unit">%</span></span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Available</span>
      <span><span class="metric-value" id="bat-avail">—</span><span class="metric-unit">kWh</span></span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Rated</span>
      <span><span class="metric-value" id="bat-rated">—</span><span class="metric-unit">kWh</span></span>
    </div>
    <div class="power-bar-container">
      <div class="power-bar-bg">
        <div class="power-bar-fill" id="bat-bar" style="width:0%; background: linear-gradient(90deg, #238636, var(--green));">0 %</div>
      </div>
      <div class="power-bar-label"><span>empty</span><span>full</span></div>
    </div>
  </div>
</div>

<div class="chart-wrap">
  <canvas id="chart"></canvas>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/luxon@3.4.4/build/global/luxon.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-luxon@1.3.1/dist/chartjs-adapter-luxon.umd.min.js"></script>

<script>
const SOLAR_PEAK = 5000;
const EVSE_MAX = 7680;

const ctx = document.getElementById('chart').getContext('2d');
const chart = new Chart(ctx, {
  type: 'line',
  data: {
    datasets: [
      { label: 'Solar (W)', data: [], borderColor: '#d29922', backgroundColor: 'rgba(210,153,34,0.12)', tension: 0.2, fill: true, yAxisID: 'y', pointRadius: 0 },
      { label: 'EV (W)', data: [], borderColor: '#58a6ff', backgroundColor: 'rgba(88,166,255,0.12)', tension: 0.2, fill: true, yAxisID: 'y', pointRadius: 0 },
      { label: 'Battery SoC (%)', data: [], borderColor: '#3fb950', tension: 0.2, yAxisID: 'y2', pointRadius: 0 },
    ]
  },
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    scales: {
      x: { type: 'time', time: { unit: 'second' }, ticks: { color: '#8b949e', maxTicksLimit: 8 }, grid: { color: '#222' } },
      y: { position: 'left', ticks: { color: '#8b949e' }, grid: { color: '#222' }, title: { display: true, text: 'Watts', color: '#8b949e' } },
      y2: { position: 'right', ticks: { color: '#8b949e' }, grid: { display: false }, title: { display: true, text: 'SoC %', color: '#8b949e' }, min: 0, max: 100 }
    },
    plugins: { legend: { labels: { color: '#e6edf3' } }, tooltip: { mode: 'index', intersect: false } }
  }
});

const CP_MAP = {
  0: ['F','fault','state-fault'],
  1: ['A','idle','state-off'],
  2: ['B','waiting','state-ready'],
  3: ['B','connected','state-ready'],
  4: ['C','ready','state-charging'],
  5: ['C','charging','state-charging'],
  6: ['D','ventilation','state-charging'],
  7: ['E','short','state-fault'],
};

function fmt(v, d=0) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  return Number(v).toFixed(d);
}
function setText(id, t) { const el = document.getElementById(id); if (el) el.textContent = t; }
function setHTML(id, h) { const el = document.getElementById(id); if (el) el.innerHTML = h; }
function badge(text, cls) { return `<span class="state-badge ${cls}">${text}</span>`; }
function setBar(id, value, max, label) {
  const el = document.getElementById(id); if (!el) return;
  const pct = Math.max(0, Math.min(100, (Math.abs(value) / max) * 100));
  el.style.width = Math.max(2, pct) + '%';
  el.textContent = label;
}

let msgCount = 0;

function render(msg) {
  document.getElementById('dot-ws').className = 'status-dot dot-green';
  document.getElementById('ws-status').textContent = 'live';
  msgCount++;
  setText('msg-count', `${msgCount} updates`);
  setText('last-update', new Date().toLocaleTimeString());

  const s = msg.solar || {};
  const e = msg.evse || {};
  const pv = Math.max(0, s.pv_W || 0);

  // Architecture
  setText('arch-solar', `${Math.round(pv)} W`);
  const invSt = s.inverter_state;
  const invTxt = invSt === 7 ? 'running' : (invSt === 2 ? 'sleeping' : 'state ' + invSt);
  setText('arch-solar-sub', `${invTxt} · ${fmt(s.voltage, 0)}V`);
  setText('arch-evse', `${fmt(e.charging_power, 0)} W`);
  setText('arch-soc', `${fmt(s.battery_soc, 0)}%`);
  const batKwh = s.battery_wh_avail;
  setText('arch-bat-sub', batKwh != null ? `${fmt(batKwh, 1)} kWh` : '—');

  const limitA = msg.evse_limit_A;
  const cpActive = e.cp_active;
  let archLimitTxt;
  if (cpActive && limitA != null) archLimitTxt = `${fmt(limitA, 1)} A`;
  else if (e.is_connected) archLimitTxt = 'setting...';
  else archLimitTxt = 'idle';
  setText('arch-limit', archLimitTxt);

  // Arrows active on flow
  document.getElementById('arrow-solar').classList.toggle('active', pv > 50);
  document.getElementById('arrow-evse').classList.toggle('active', !!e.is_charging);

  // Solar panel
  setText('solar-power', fmt(pv, 0));
  setText('solar-voltage', fmt(s.voltage, 1));
  setBar('solar-bar', pv, SOLAR_PEAK, `${fmt(pv, 0)} W`);
  if (pv > 50) setHTML('solar-status', badge('producing', 'state-producing'));
  else if (invSt === 7) setHTML('solar-status', badge('running (idle)', 'state-off'));
  else if (invSt === 2) setHTML('solar-status', badge('sleeping', 'state-off'));
  else setHTML('solar-status', badge('unknown', 'state-off'));

  // EVSE panel
  setText('evse-power', fmt(e.charging_power, 0));
  setText('evse-voltage', fmt(e.charging_voltage, 1));
  setText('evse-current', fmt(e.charging_current, 2));
  setText('evse-temp', fmt(e.temperature, 1));
  setBar('evse-bar', Math.abs(e.charging_power || 0), EVSE_MAX, `${fmt(e.charging_power, 0)} W`);

  const [cpLetter, cpLabel, cpClass] = CP_MAP[e.status] || ['?','unknown','state-off'];
  let stateTxt;
  if (e.is_charging) stateTxt = 'CHARGING';
  else if (e.is_connected) stateTxt = cpLabel.toUpperCase();
  else if (e.status !== null && e.status !== undefined) stateTxt = cpLabel.toUpperCase();
  else stateTxt = 'OFFLINE';
  setHTML('evse-cp-state', badge(`CP ${cpLetter} · ${stateTxt}`, cpClass));
  setText('arch-evse-sub', `CP ${cpLetter} · ${cpLabel}`);

  // Energy manager: what EVerest pushes as "available from solar" is just max(0, pv_W).
  // The max current is that divided by nominal voltage (240V default).
  const solarAvailW = Math.max(0, pv);
  const solarAvailA = solarAvailW / 240;
  setText('em-solar-limit', fmt(solarAvailW, 0));
  setText('em-max-current', fmt(solarAvailA, 2));
  const net = pv - Math.abs(e.charging_power || 0);
  setText('em-net-power', `${net >= 0 ? '+' : ''}${fmt(net, 0)}`);
  const emNetEl = document.getElementById('em-net-power');
  emNetEl.style.color = net >= 0 ? 'var(--green)' : 'var(--red)';
  const netRange = Math.max(EVSE_MAX, SOLAR_PEAK);
  const netPct = 50 + (net / netRange) * 50;
  const netBar = document.getElementById('net-bar');
  netBar.style.width = Math.max(5, Math.min(95, netPct)) + '%';
  netBar.style.background = net >= 0
    ? 'linear-gradient(90deg, #238636, var(--green))'
    : 'linear-gradient(90deg, #da3633, var(--red))';
  netBar.textContent = net >= 0
    ? `+${fmt(net, 0)} W to battery`
    : `${fmt(net, 0)} W from battery`;

  // Battery
  const soc = s.battery_soc;
  if (soc !== null && soc !== undefined) {
    setText('bat-soc', fmt(soc, 1));
    setBar('bat-bar', soc, 100, `${fmt(soc, 1)} %`);
  }
  setText('bat-avail', s.battery_wh_avail != null ? fmt(s.battery_wh_avail, 2) : '—');
  setText('bat-rated', s.battery_wh_rated != null ? fmt(s.battery_wh_rated, 2) : '—');

  // Chart
  if (msg.history && msg.history.length) {
    chart.data.datasets[0].data = msg.history.map(p => ({ x: p.t, y: p.pv_W }));
    chart.data.datasets[1].data = msg.history.map(p => ({ x: p.t, y: p.evse_W }));
    chart.data.datasets[2].data = msg.history.map(p => ({ x: p.t, y: p.battery_soc }));
    chart.update('none');
  }
}

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = ev => { try { render(JSON.parse(ev.data)); } catch(err){} };
  ws.onclose = () => {
    document.getElementById('dot-ws').className = 'status-dot dot-red';
    document.getElementById('ws-status').textContent = 'disconnected, retrying...';
    setTimeout(connect, 2000);
  };
  ws.onerror = () => {
    document.getElementById('dot-ws').className = 'status-dot dot-red';
  };
}
connect();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    connections.add(ws)
    try:
        while True:
            await asyncio.sleep(1.0)
            await ws.send_json(state.snapshot())
    except WebSocketDisconnect:
        pass
    finally:
        connections.discard(ws)


def main():
    print(f"MQTT:        {MQTT_HOST}:{MQTT_PORT}")
    print(f"Device ID:   {DEVICE_ID}")
    print(f"Solar topic: {SOLAR_TOPIC}")
    print(f"Serving:     http://localhost:{HTTP_PORT}")

    start_mqtt()
    threading.Thread(target=history_recorder, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT, log_level="warning")


if __name__ == "__main__":
    main()
