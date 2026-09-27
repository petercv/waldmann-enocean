"""Regenerate the screenshots in docs/images.

    python3 tools/screenshots.py

The page gets all its data from the JSON API, so stubbing `fetch` in front of
the app script is enough to render it fully populated without a bridge, a USB
stick or a broker - and with a made-up luminaire instead of a real one.

Needs Google Chrome; set $CHROME if it is somewhere else.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
INDEX = REPO / "src/waldmann_enocean/bridge/index.html"
OUT = REPO / "docs/images"
WORK = pathlib.Path(tempfile.mkdtemp(prefix="waldmann-shots-"))
CHROME = os.environ.get(
    "CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

DEVICE = "01A2B3C4"          # invented; a real one is the module's chip id
BASE = "waldmann-enocean"

STATE = {
    "uptime": 96_420,
    "port": "/dev/ttyUSB0",
    "base_id": "FFD4A100",
    "sender": "FFD4A100",
    "mqtt_connected": True,
    "mqtt_error": "",
    "mqtt_host": "192.168.1.20:1883",
    "base_topic": BASE,
    "discovery_prefix": "homeassistant",
    "pairing": False,
    "pairing_left": 0,
    "bridge_topics": {
        "availability": f"{BASE}/bridge/status",
        "pair": f"{BASE}/bridge/pair/set",
    },
    "log": [
        "[08:14:02] stick on /dev/ttyUSB0, Base ID FFD4A100, transmitting as FFD4A100",
        "[08:14:02] MQTT connected to 192.168.1.20:1883",
        "[08:14:02] published Home Assistant discovery",
        f"[08:14:03] {DEVICE} head 0 Unit Status: working light  62 %  3500 K",
        f"[09:31:17] {DEVICE} head 1 on dim=18%",
        f"[11:02:44] {DEVICE} head 0 set ct=3500K",
    ],
    "devices": [{
        "id": DEVICE,
        "eep": "D2-41-00",
        "paired_at": "2026-02-11T09:20:41+00:00",
        "refresh_topic": f"{BASE}/{DEVICE}/refresh/set",
        "units": [
            {
                "unit": 0, "last_seen": 7,
                "fields": {
                    "illumination_mode": "working light", "dim_percent": 62,
                    "color_temp_k": 3500, "vtl": "off", "presence": "presence",
                    "illumination_lx": 310, "temperature_c": 22.4,
                    "humidity_pct": 41, "noise_db_a": 38, "voc_ppb": 120,
                    "operating_hours": 4187, "power_w": 24.6, "energy_kwh": 96.2,
                },
                "topics": {
                    "state": f"{BASE}/{DEVICE}/unit0/state",
                    "set": f"{BASE}/{DEVICE}/unit0/set",
                    "vtl": f"{BASE}/{DEVICE}/unit0/vtl/set",
                },
            },
            {
                "unit": 1, "last_seen": 21,
                "fields": {
                    "illumination_mode": "reduced light", "dim_percent": 18,
                    "color_temp_k": 2700, "vtl": "off", "presence": "no presence",
                    "illumination_lx": 96, "temperature_c": 22.1, "humidity_pct": 42,
                },
                "topics": {
                    "state": f"{BASE}/{DEVICE}/unit1/state",
                    "set": f"{BASE}/{DEVICE}/unit1/set",
                    "vtl": f"{BASE}/{DEVICE}/unit1/vtl/set",
                },
            },
        ],
    }],
}

TELEGRAMS = [
    {"t": "11:02:39", "dir": "RX", "sender": DEVICE, "device": DEVICE, "unit": 0,
     "dbm": -58, "hex": "09 2F FF 7C 0D AC",
     "summary": "head 0 Unit Status: illumination_mode=working light  dim_percent=62 %  color_temp_k=3500 K"},
    {"t": "11:02:44", "dir": "TX", "sender": "", "device": DEVICE, "unit": 0,
     "dbm": None, "hex": "08 2F FF FF 0D AC", "summary": "head 0 set ct=3500K"},
    {"t": "11:02:44", "dir": "RX", "sender": DEVICE, "device": DEVICE, "unit": 0,
     "dbm": -57, "hex": "09 2F FF 7C 0D AC",
     "summary": "head 0 Unit Status: illumination_mode=working light  dim_percent=62 %  color_temp_k=3500 K"},
    {"t": "11:03:01", "dir": "RX", "sender": DEVICE, "device": DEVICE, "unit": 1,
     "dbm": -61, "hex": "1B 00", "summary": "head 1 Presence Data: presence=no presence"},
    {"t": "11:03:12", "dir": "RX", "sender": DEVICE, "device": DEVICE, "unit": 0,
     "dbm": -59, "hex": "0C 01 36 E0 16 29 78",
     "summary": "head 0 Environmental Data: illumination_lx=310  temperature_c=22.4 °C  humidity_pct=41 %"},
]

CONFIG = {
    "port": "", "baudrate": 57600, "sender_offset": 0,
    "mqtt_host": "192.168.1.20", "mqtt_port": 1883, "mqtt_username": "homeassistant",
    "mqtt_password_set": True, "mqtt_client_id": "waldmann-enocean",
    "mqtt_tls": False, "mqtt_tls_ca": "", "mqtt_tls_insecure": False,
    "base_topic": BASE, "discovery_prefix": "homeassistant",
    "poll_interval": 600, "maintenance_interval": 3600, "pair_window": 120,
    "web_host": "0.0.0.0", "web_port": 8099, "web_username": "admin",
}
PORTS = {"active": "/dev/ttyUSB0", "ports": ["/dev/ttyUSB0"]}

STUB = """
<script>
// screenshot harness: serve the page invented data instead of a live bridge
const FAKE = %s;
window.fetch = async (url, opts) => {
  const post = opts && opts.method === 'POST';
  let data = {};
  if (post) data = {ok: true, changed: []};
  else if (url.startsWith('/api/state')) data = FAKE.state;
  else if (url.startsWith('/api/telegrams')) data = FAKE.telegrams;
  else if (url.startsWith('/api/config')) data = FAKE.config;
  else if (url.startsWith('/api/ports')) data = FAKE.ports;
  return {ok: true, status: 200, json: async () => data};
};
try {
  localStorage.setItem('waldmann-enocean.token', 'screenshot');
  // The theme is applied from <head> before this runs, so set the attribute
  // directly as well as the stored preference the app reads back.
  localStorage.setItem('waldmann-enocean.theme', 'dark');
} catch (e) {}
document.documentElement.setAttribute('data-theme', 'dark');
</script>
"""

POST = """
<script>
// Pick the tab first: the telegram monitor skips its refresh while the tab is
// hidden, so it has to be visible before the data is pulled in.
setTimeout(() => {
  document.querySelectorAll('.tabs button').forEach(b => {
    if (b.dataset.tab === '%s') b.click();
  });
  const overlay = document.getElementById('login');
  if (overlay) overlay.hidden = true;
  refresh(); refreshTelegrams();
  setTimeout(() => { for (let i = 1; i < 9999; i++) clearInterval(i); }, 600);
}, 500);
</script>
"""

# heights trimmed to the rendered content, so the images carry no dead space
TABS = {
    "status": (1000, 745),
    "enocean": (1100, 1395),
    "mqtt": (1000, 925),
    "settings": (1100, 1680),
}


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    html = INDEX.read_text()
    marker = html.rindex("<script>")          # the app's own script block
    fake = json.dumps({"state": STATE, "telegrams": TELEGRAMS,
                       "config": CONFIG, "ports": PORTS})
    for tab, (width, height) in TABS.items():
        page = html[:marker] + (STUB % fake) + html[marker:]
        page = page.replace("</body>", (POST % tab) + "</body>")
        target = WORK / f"{tab}.html"
        target.write_text(page)
        out = OUT / f"{tab}.png"
        subprocess.run([
            CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            f"--window-size={width},{height}",
            "--virtual-time-budget=4000",
            f"--screenshot={out}", f"file://{target}",
        ], check=True, capture_output=True)
        size = out.stat().st_size
        print(f"  {out.name:14} {width}x{height}  {size // 1024} KB")
        if size < 5000:
            sys.exit(f"{out.name} looks empty - the page probably did not render")


if __name__ == "__main__":
    build()
