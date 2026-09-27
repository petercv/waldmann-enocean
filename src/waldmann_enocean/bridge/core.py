"""The bridge itself: owns the USB stick, speaks MQTT, keeps state.

Threading rule: **only the main loop touches the serial port or changes
state.** MQTT callbacks and web requests append to `commands`, which the main
loop drains.  Other threads may only read, and then through copies (see
`snapshot`), because iterating a dict another thread is changing raises.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..protocol import (
    ENVIRONMENTAL_DATA,
    GET_ENVIRONMENTAL_DATA,
    GET_MAINTENANCE_DATA,
    GET_PRESENCE_DATA,
    GET_PRODUCT_STATUS,
    GET_UNIT_STATUS,
    ILLUMINATION_MODES,
    MAINTENANCE_DATA,
    MODE_BY_NAME,
    MODE_VALUES,
    NO_CHANGE_4,
    NO_CHANGE_CT,
    NO_CHANGE_DIM,
    PRESENCE_DATA,
    PRODUCT_STATUS,
    RORG_SIGNAL,
    RORG_UTE,
    RORG_VLD,
    UNIT_STATUS,
    VTL_BY_NAME,
    Device,
    Dongle,
    Radio,
    autodetect_port,
    build_ute_response,
    decode_d2_41_00,
    encode_get,
    encode_set_unit_data,
    load_devices,
    parse_ute,
    save_devices,
)

from .config import Config
from . import discovery as disc

try:
    import paho.mqtt.client as mqtt  # type: ignore[import-untyped]
except ImportError as exc:  # pragma: no cover
    raise SystemExit("paho-mqtt is missing.  Install it with: pip install paho-mqtt") from exc

LOG = logging.getLogger("waldmann_enocean.bridge")

GET_NAMES = {
    "status": GET_UNIT_STATUS,
    "presence": GET_PRESENCE_DATA,
    "environment": GET_ENVIRONMENTAL_DATA,
    "maintenance": GET_MAINTENANCE_DATA,
    "product": GET_PRODUCT_STATUS,
}


@dataclass
class UnitState:
    fields: dict[str, Any] = field(default_factory=dict)
    last_seen: float = 0.0
    announced: bool = False
    # Sensors are announced only once a real value has arrived, so fields the
    # luminaire does not support never appear as permanently "unknown".
    announced_sensors: set[str] = field(default_factory=set)


class Bridge:
    def __init__(self, config: Config, store_path: Path) -> None:
        self.config = config
        self.topics = disc.Topics(config)
        self.store_path = store_path
        self.devices = load_devices(store_path)
        self.units: dict[tuple[str, int], UnitState] = {}
        self.commands: queue.Queue[tuple[str, tuple[Any, ...]]] = queue.Queue()
        self.log_lines: deque[str] = deque(maxlen=300)
        self.telegrams: deque[dict[str, Any]] = deque(maxlen=200)
        self.pair_until = 0.0
        self.started = time.time()
        self.mqtt_connected = False
        self.mqtt_error = ""
        self.base_id = b""
        self.dongle: Dongle | None = None
        self.client: Any = None
        self._next_poll = 0.0
        self._next_maintenance = 0.0
        self._last_connect_warning = -1e9
        # (due_at, device, unit) re-reads queued after a command
        self._followups: list[tuple[float, str, int]] = []
        self._lock = threading.Lock()

    # -- helpers ------------------------------------------------------------

    def note(self, message: str) -> None:
        with self._lock:
            self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {message}")
        LOG.info("%s", message)

    def trace(self, direction: str, radio: Radio | None, raw: str, summary: str,
              device: str = "", unit: int | None = None) -> None:
        """Record one telegram for the monitor.

        `device` and `unit` are what the web UI filters on: for a reception
        they are who sent it, for a transmission who it was addressed to, which
        is why they cannot simply be read back off `radio`.
        """
        with self._lock:
            self.telegrams.append({
                "t": time.strftime("%H:%M:%S"),
                "dir": direction,
                "sender": radio.sender_id if radio else "",
                "device": device or (radio.sender_id if radio else ""),
                "unit": unit,
                "rorg": f"0x{radio.rorg:02X}" if radio else "0xD2",
                "dbm": radio.dbm if radio else None,
                "hex": raw,
                "summary": summary,
            })

    @property
    def pairing(self) -> bool:
        return time.monotonic() < self.pair_until

    def sender(self, offset: int | None = None) -> bytes:
        """Base ID + offset, by default the offset new pairings are made from.

        Called from the web thread too, so it must never touch the serial port;
        the Base ID is read once at startup.
        """
        if not self.base_id:
            return b""
        if offset is None:
            offset = self.config.sender_offset
        return self.base_id[:3] + bytes([(self.base_id[3] + offset) & 0xFF])

    def sender_for(self, device_id: str) -> bytes:
        """The address a luminaire was paired from, the only one it obeys."""
        device = self.devices.get(device_id)
        return self.sender(device.sender_offset if device else None)

    def known_units(self, device_id: str) -> list[int]:
        return sorted(u for (d, u) in self.units if d == device_id)

    # -- MQTT ---------------------------------------------------------------

    def connect_mqtt(self) -> None:
        cfg = self.config
        try:  # paho-mqtt 2.x wants the callback API version
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg.mqtt_client_id)
        except AttributeError:  # paho-mqtt 1.x
            client = mqtt.Client(client_id=cfg.mqtt_client_id)
        if cfg.mqtt_username:
            client.username_pw_set(cfg.mqtt_username, cfg.mqtt_password or None)
        if cfg.mqtt_tls:
            # A broker with a publicly trusted certificate needs nothing extra.
            # A self-signed one needs its CA file, which is the usual case for
            # a Mosquitto on the LAN.
            client.tls_set(ca_certs=cfg.mqtt_tls_ca or None)
            if cfg.mqtt_tls_insecure:
                client.tls_insecure_set(True)
                self.note("TLS certificate verification disabled")
        client.will_set(self.topics.availability(), "offline", retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.on_connect_fail = self._on_connect_fail
        self.client = client
        try:
            client.connect_async(cfg.mqtt_host, cfg.mqtt_port, keepalive=60)
            client.loop_start()
        except Exception as exc:  # bad host, DNS failure, ...
            self.mqtt_error = str(exc)
            self.note(f"MQTT setup failed: {exc}")

    def reconnect_mqtt(self) -> None:
        """Apply changed MQTT settings without restarting the service."""
        if self.client is not None:
            try:
                self.client.publish(self.topics.availability(), "offline", retain=True)
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                LOG.debug("error closing the previous MQTT client", exc_info=True)
        self.mqtt_connected = False
        # the base topic and discovery prefix may have changed too
        self.topics = disc.Topics(self.config)
        self.note("reconnecting to MQTT with the new settings")
        self.connect_mqtt()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        # Anything raised here propagates out of paho's network loop and kills
        # the client thread, so keep it contained.
        try:
            # A refused connection (bad credentials, not authorised) still calls
            # on_connect, with a failure code - treating it as success would
            # leave us publishing into a dead socket.
            failed = (reason_code.is_failure
                      if hasattr(reason_code, "is_failure") else reason_code != 0)
            if failed:
                self.mqtt_connected = False
                self.mqtt_error = f"broker refused the connection: {reason_code}"
                self.note(f"MQTT {self.mqtt_error}")
                return
            self.mqtt_connected = True
            self.mqtt_error = ""
            self.note(f"MQTT connected to {self.config.mqtt_host}:{self.config.mqtt_port}")
            client.publish(self.topics.availability(), "online", retain=True)
            base = self.config.base_topic
            # "+" must be a whole topic level - "unit+" is not a legal filter.
            # base/+/+/set covers both unitN/set and refresh/set, and
            # base/bridge/pair/set as well; _on_message sorts them out.
            for pattern in (f"{base}/+/+/set", f"{base}/+/+/vtl/set"):
                client.subscribe(pattern)
            # Not called directly: this runs on paho's thread, and discovery
            # walks state the main loop is changing.
            self.commands.put(("rediscover", ()))
        except Exception:
            LOG.exception("MQTT on_connect failed")

    def _on_connect_fail(self, client, userdata, *args) -> None:
        """paho retries quietly; say so at least once a minute."""
        self.mqtt_connected = False
        self.mqtt_error = (f"cannot reach {self.config.mqtt_host}:{self.config.mqtt_port}")
        now = time.monotonic()
        if now - self._last_connect_warning > 60:
            self._last_connect_warning = now
            self.note(f"MQTT {self.mqtt_error} - retrying")

    def _on_disconnect(self, client, userdata, *args) -> None:
        self.mqtt_connected = False
        self.note("MQTT disconnected")

    def _on_message(self, client, userdata, message) -> None:
        topic, payload = message.topic, message.payload.decode("utf-8", "replace").strip()
        LOG.debug("MQTT in: %s = %s", topic, payload)
        try:  # never let a malformed topic kill the client thread
            if topic == self.topics.pair_command():
                self.commands.put(("pair", ()))
                return
            # Strip the base topic first: it may have several levels itself.
            prefix = self.topics.base + "/"
            if not topic.startswith(prefix):
                return
            parts = topic[len(prefix):].split("/")
            device_id = parts[0].upper()
            if device_id not in self.devices:
                # Only paired luminaires, the same as the web API: anyone who
                # can publish to the broker shouldn't be able to make us
                # transmit to an arbitrary EnOcean id.
                LOG.warning("MQTT command for unknown luminaire %s ignored", parts[0])
                return
            if parts[1:] == ["refresh", "set"]:
                self.commands.put(("refresh", (device_id,)))
            elif len(parts) == 4 and parts[1].startswith("unit") and parts[2:] == ["vtl", "set"]:
                self.commands.put(("vtl", (device_id, int(parts[1][4:]), payload)))
            elif len(parts) == 3 and parts[1].startswith("unit") and parts[2] == "set":
                self.commands.put(("light", (device_id, int(parts[1][4:]), payload)))
            else:
                LOG.warning("unhandled MQTT topic %s", topic)
        except ValueError:
            LOG.warning("unhandled MQTT topic %s", topic)
        except Exception:
            LOG.exception("MQTT on_message failed for %s", topic)

    def publish(self, topic: str, payload: Any, retain: bool = True) -> None:
        if not self.mqtt_connected or self.client is None:
            return
        body = payload if isinstance(payload, str) else json.dumps(payload)
        self.client.publish(topic, body, retain=retain)

    # -- discovery ----------------------------------------------------------

    def republish_discovery(self) -> None:
        """Discovery configs, then the last known state of every head.

        Runs after every (re)connect.  State published while the broker was
        unreachable was dropped, and a restarted broker has lost the retained
        copies, so without the second half Home Assistant shows stale or
        unknown values until each luminaire happens to report again.
        """
        for topic, payload in disc.bridge_entities(self.topics):
            self.publish(topic, payload)
        for device_id in self.devices:
            self.publish(*disc.refresh_button(self.topics, device_id))
            for unit in self.known_units(device_id) or [0]:
                self.announce_unit(device_id, unit)
                entry = self.units.get((device_id, unit))
                if entry is not None:
                    for key in entry.announced_sensors:
                        built = disc.sensor_entity(self.topics, device_id, unit, key)
                        if built:
                            self.publish(*built)
                    if entry.fields:
                        self.publish(self.topics.state(device_id, unit),
                                     self.state_payload(entry))
        self.note("published Home Assistant discovery")

    def announce_unit(self, device_id: str, unit: int) -> None:
        for topic, payload in disc.unit_entities(
            self.topics, device_id, unit, list(VTL_BY_NAME)
        ):
            self.publish(topic, payload)

    def forget_discovery(self, device_id: str) -> None:
        """Retained empty payloads remove the entities from HA."""
        for topic in disc.device_entity_topics(
            self.topics, device_id, self.known_units(device_id) or [0]
        ):
            self.publish(topic, "")

    # -- state --------------------------------------------------------------

    def update_state(self, device_id: str, unit: int, fields: dict[str, Any]) -> None:
        entry = self.units.setdefault((device_id, unit), UnitState())
        entry.fields.update(fields)
        entry.last_seen = time.time()
        if not entry.announced:
            entry.announced = True
            self.announce_unit(device_id, unit)
        for key, value in fields.items():
            if value is not None and key not in entry.announced_sensors:
                built = disc.sensor_entity(self.topics, device_id, unit, key)
                if built:
                    entry.announced_sensors.add(key)
                    self.publish(*built)
        self.publish(self.topics.state(device_id, unit), self.state_payload(entry))

    @staticmethod
    def state_payload(entry: UnitState) -> dict[str, Any]:
        """The decoded fields plus the keys Home Assistant's JSON light reads."""
        payload = dict(entry.fields)
        mode = payload.get("illumination_mode")
        if mode is not None:
            payload["state"] = "OFF" if mode == "off" else "ON"
        dim = payload.get("dim_percent")
        if isinstance(dim, (int, float)):
            payload["brightness"] = max(0, min(255, round(dim * 255 / 100)))
        color = payload.get("color_temp_k")
        if isinstance(color, (int, float)) and color > 0:
            # HA needs color_mode declared before it treats color temperature
            # as active.  The discovery config sets color_temp_kelvin, so HA
            # reads color_temp as kelvin, not mireds.
            payload["color_mode"] = "color_temp"
            payload["color_temp"] = int(color)
        effect = disc.EFFECT_BY_VTL.get(payload.get("vtl"))
        if effect:
            payload["effect"] = effect
        return payload

    # -- radio --------------------------------------------------------------

    def send_vld(self, device_id: str, payload: bytes, summary: str = "") -> None:
        assert self.dongle is not None
        self.dongle.send_radio(
            RORG_VLD, payload, sender=self.sender_for(device_id),
            destination=bytes.fromhex(device_id),
        )
        self.trace("TX", None, payload.hex(" ").upper(), summary or f"-> {device_id}",
                   device=device_id, unit=payload[0] >> 4 if payload else None)

    def handle_radio(self, radio: Radio) -> None:
        device_id = radio.sender_id
        if radio.rorg == RORG_UTE:
            self.handle_ute(radio)
            return
        if radio.rorg == RORG_SIGNAL:
            self.trace("RX", radio, radio.payload.hex(" ").upper(), "signal telegram",
                       device=device_id)
            return
        if radio.rorg != RORG_VLD:
            self.trace("RX", radio, radio.payload.hex(" ").upper(), "other RORG",
                       device=device_id)
            return
        try:
            unit, command, fields = decode_d2_41_00(radio.payload)
        except ValueError as exc:
            self.trace("RX", radio, radio.payload.hex(" ").upper(), f"undecodable: {exc}",
                       device=device_id)
            return
        from ..protocol import COMMAND_NAMES, format_field

        self.trace("RX", radio, radio.payload.hex(" ").upper(),
                   f"head {unit} {COMMAND_NAMES.get(command, command)}: "
                   + "  ".join(f"{k}={format_field(k, v)}" for k, v in fields.items()),
                   device=device_id, unit=unit)
        if device_id not in self.devices:
            return
        if command == PRODUCT_STATUS:
            # How we learn which heads exist.  A head first seen here has not
            # been polled - the startup poll ran before we knew of it - so
            # queue one, or it stays blank until the luminaire next broadcasts.
            for active in fields.get("active_units", []):
                entry = self.units.setdefault((device_id, active), UnitState())
                if not entry.announced:
                    entry.announced = True
                    self.announce_unit(device_id, active)
                    self.commands.put(("poll_unit", (device_id, active, True)))
            return
        if command in (UNIT_STATUS, PRESENCE_DATA, ENVIRONMENTAL_DATA, MAINTENANCE_DATA):
            self.update_state(device_id, unit, fields)

    def handle_ute(self, radio: Radio) -> None:
        request = parse_ute(radio.payload)
        if request is None or request.command != 0:
            return
        device_id = radio.sender_id
        self.trace("RX", radio, radio.payload.hex(" ").upper(),
                   f"UTE {request.request_text} EEP={request.eep_text}", device=device_id)
        if not self.pairing:
            self.note(f"teach-in from {device_id} ignored - not in pairing mode")
            return
        if request.eep != (0xD2, 0x41, 0x00):
            self.note(f"teach-in from {device_id} has EEP {request.eep_text}, ignoring")
            return
        assert self.dongle is not None
        teach_out = request.request_type == 1
        if request.response_expected:
            self.dongle.send_radio(
                RORG_UTE, build_ute_response(request, 2 if teach_out else 1),
                sender=self.sender(), destination=radio.sender,
            )
        if teach_out:
            self.drop_device(device_id)
            self.note(f"unpaired {device_id}")
        else:
            self.devices[device_id] = Device(
                device_id=device_id, eep=request.eep_text,
                manufacturer=request.manufacturer,
                paired_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                # the response above went out from this address, so it's the
                # one the luminaire will take commands from
                sender_offset=self.config.sender_offset,
                notes={"channels": request.channels},
            )
            save_devices(self.store_path, self.devices)
            self.note(f"paired {device_id} - creating it in Home Assistant")
            self.publish(*disc.refresh_button(self.topics, device_id))
            self.announce_unit(device_id, 0)
            self.commands.put(("refresh", (device_id,)))
        self.pair_until = 0.0

    def drop_device(self, device_id: str) -> None:
        self.forget_discovery(device_id)
        self.devices.pop(device_id, None)
        for key in [k for k in self.units if k[0] == device_id]:
            del self.units[key]
        save_devices(self.store_path, self.devices)

    # -- commands -----------------------------------------------------------

    def run_command(self, name: str, args: tuple[Any, ...]) -> None:
        if name == "pair":
            window = args[0] if args else self.config.pair_window
            self.pair_until = time.monotonic() + window
            self.note(f"pairing open for {int(window)}s - briefly press key C on the module")
        elif name == "stop_pair":
            self.pair_until = 0.0
            self.note("pairing cancelled")
        elif name == "refresh":
            self.poll_device(args[0], include_maintenance=True)
        elif name == "poll_unit":
            self.poll_unit(*args)
        elif name == "light":
            self.apply_light(*args)
        elif name == "set_fields":
            self.set_fields(*args)
        elif name == "vtl":
            device_id, unit, value = args
            if value in VTL_BY_NAME:
                self.send_vld(device_id, encode_set_unit_data(
                    unit=unit, mode=NO_CHANGE_4, switch=1, vtl=VTL_BY_NAME[value]),
                    f"head {unit} set vtl={value}")
                self.schedule_followup(device_id, unit)
        elif name == "get":
            device_id, unit, which = args
            get = GET_NAMES.get(which)
            if get is not None:
                self.send_vld(device_id, encode_get(unit, get), f"head {unit} get {which}")
        elif name == "raw":
            device_id, payload = args
            self.send_vld(device_id, payload, "raw payload")
        elif name == "forget":
            self.drop_device(args[0])
            self.note(f"forgot {args[0]}")
        elif name == "rediscover":
            self.republish_discovery()
        elif name == "reconnect_mqtt":
            self.reconnect_mqtt()

    def lit_mode(self, device_id: str, unit: int) -> int:
        """A mode that produces light: the head's own, or working light.

        Used wherever a command has to name a mode without meaning to change
        one, so that a head set to reduced light is not quietly promoted.
        """
        entry = self.units.get((device_id, unit))
        name = entry.fields.get("illumination_mode") if entry else None
        current = MODE_VALUES.get(name) if isinstance(name, str) else None
        return MODE_BY_NAME["working"] if current in (None, 0) else current

    def set_fields(self, device_id: str, unit: int, fields: dict[str, Any]) -> None:
        """Set only the named fields; everything else goes out as "no change".

        Set Unit Data carries every setting in one telegram, but the profile
        has a no-change sentinel per field, so a single-field change really
        does leave the rest alone.  With one exception: a dimming level is
        discarded unless the telegram also names an illumination mode, so a
        brightness-only change carries the head's current mode along.
        """
        mode = NO_CHANGE_4
        if "mode" in fields:
            mode = MODE_BY_NAME.get(str(fields["mode"]), NO_CHANGE_4)
        dim_raw = NO_CHANGE_DIM
        if "brightness" in fields:  # percent from the UI, 0..200 on the wire
            dim_raw = max(0, min(200, round(float(fields["brightness"]) * 2)))
        color = NO_CHANGE_CT
        if "kelvin" in fields:
            color = max(0, min(16000, int(fields["kelvin"])))
        vtl = NO_CHANGE_4
        if "vtl" in fields:
            vtl = VTL_BY_NAME.get(str(fields["vtl"]), NO_CHANGE_4)

        carried = dim_raw != NO_CHANGE_DIM and mode == NO_CHANGE_4
        if carried:
            mode = self.lit_mode(device_id, unit)

        parts = []
        if mode != NO_CHANGE_4:
            label = ILLUMINATION_MODES[mode] if carried else fields["mode"]
            parts.append(f"mode={label}{' (carried)' if carried else ''}")
        if dim_raw != NO_CHANGE_DIM:
            parts.append(f"dim={dim_raw / 2:g}%")
        if color != NO_CHANGE_CT:
            parts.append(f"ct={color}K")
        if vtl != NO_CHANGE_4:
            parts.append(f"vtl={fields['vtl']}")
        summary = f"head {unit} set " + " ".join(parts)
        self.send_vld(device_id, encode_set_unit_data(
            unit=unit, mode=mode, switch=1, vtl=vtl,
            dim_raw=dim_raw, color_temp=color), summary)
        self.note(f"{device_id} {summary}")
        self.schedule_followup(device_id, unit)

    def apply_light(self, device_id: str, unit: int, raw: str) -> None:
        try:
            command = json.loads(raw)
        except json.JSONDecodeError:
            command = {"state": raw.upper()}
        state = str(command.get("state", "ON")).upper()
        dim_raw = NO_CHANGE_DIM
        if command.get("brightness") is not None:
            dim_raw = max(0, min(200, round(float(command["brightness"]) / 255 * 200)))
        color, vtl = NO_CHANGE_CT, NO_CHANGE_4
        if command.get("color_temp") is not None:  # kelvin, see state_payload
            color = max(0, min(16000, int(command["color_temp"])))
            vtl = VTL_BY_NAME["off"]  # the chronotype would override it otherwise
        chronotype = disc.VTL_EFFECTS.get(str(command.get("effect")))
        if chronotype:
            vtl = VTL_BY_NAME[chronotype]
        # Switching on keeps whatever mode the head is already in, so changing
        # the brightness of a reduced-light head does not promote it to working
        # light.  The mode has to be named either way: the luminaire drops the
        # dimming level from a telegram that leaves it at "no change".
        mode = 0 if state == "OFF" else self.lit_mode(device_id, unit)
        summary = (f"head {unit} {state.lower()}"
                   + (f" dim={dim_raw / 2:g}%" if dim_raw != NO_CHANGE_DIM else "")
                   + (f" ct={color}K" if color != NO_CHANGE_CT else "")
                   + (f" vtl={chronotype}" if chronotype else ""))
        self.send_vld(device_id, encode_set_unit_data(
            unit=unit, mode=mode, switch=1, vtl=vtl,
            dim_raw=dim_raw, color_temp=color), summary)
        self.note(f"{device_id} {summary}")
        self.schedule_followup(device_id, unit)

    def schedule_followup(self, device_id: str, unit: int) -> None:
        """Re-read a head shortly after commanding it.

        The luminaire reports its Unit Status the moment a change starts, so
        that telegram carries a mid-fade value and the settled one is never
        announced.  Without these re-reads Home Assistant shows the
        intermediate figure until the next poll, minutes later.
        """
        now = time.monotonic()
        self._followups += [(now + 5, device_id, unit), (now + 15, device_id, unit)]

    def poll_unit(self, device_id: str, unit: int, include_maintenance: bool = False) -> None:
        gets = [GET_UNIT_STATUS, GET_PRESENCE_DATA, GET_ENVIRONMENTAL_DATA]
        if include_maintenance:
            gets.append(GET_MAINTENANCE_DATA)
        for get in gets:
            self.send_vld(device_id, encode_get(unit, get), f"poll head {unit}")
            time.sleep(0.15)

    def poll_device(self, device_id: str, include_maintenance: bool = False) -> None:
        # Ask which heads exist first; new ones are polled from the handler.
        self.send_vld(device_id, encode_get(0, GET_PRODUCT_STATUS), "poll product status")
        for unit in self.known_units(device_id):
            self.poll_unit(device_id, unit, include_maintenance)

    # -- main loop ----------------------------------------------------------

    def run(self) -> None:
        port = self.config.port or autodetect_port()
        self.dongle = Dongle(port, self.config.baudrate)
        self.base_id = self.dongle.base_id()
        self.note(f"stick on {port}, Base ID {self.base_id.hex().upper()}, "
                  f"transmitting as {self.sender().hex().upper()}")
        self.connect_mqtt()
        try:
            while True:
                while True:
                    try:
                        name, args = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self.run_command(name, args)
                    except Exception:
                        LOG.exception("command %s failed", name)
                radio = self.dongle.read_radio(0.2)
                if radio is not None:
                    try:
                        self.handle_radio(radio)
                    except Exception:
                        LOG.exception("failed handling telegram")
                self.periodic()
        finally:
            self.publish(self.topics.availability(), "offline")
            self.dongle.close()

    def periodic(self) -> None:
        now = time.monotonic()
        if self._followups:
            due = [f for f in self._followups if f[0] <= now]
            if due:
                self._followups = [f for f in self._followups if f[0] > now]
                for _, device_id, unit in due:
                    self.send_vld(device_id, encode_get(unit, GET_UNIT_STATUS),
                                  f"re-read head {unit} after a command")
        if now >= self._next_poll:
            self._next_poll = now + self.config.poll_interval
            for device_id in list(self.devices):
                self.poll_device(device_id)
        if now >= self._next_maintenance:
            self._next_maintenance = now + self.config.maintenance_interval
            for device_id in list(self.devices):
                for unit in self.known_units(device_id):
                    self.send_vld(device_id, encode_get(unit, GET_MAINTENANCE_DATA),
                                  f"poll maintenance head {unit}")
                    time.sleep(0.15)

    # -- snapshot for the web UI -------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Everything the web UI shows.  Runs on a web thread.

        dict() copies of an existing dict are made in one step under the GIL,
        so they're safe to take while the main loop keeps changing the
        originals; iterating the originals directly is not.
        """
        with self._lock:
            log = list(self.log_lines)[-80:]
        units = dict(self.units)
        devices = dict(self.devices)
        return {
            "uptime": int(time.time() - self.started),
            "port": self.dongle.port if self.dongle else "",
            "base_id": self.base_id.hex().upper(),
            "sender": self.sender().hex().upper(),
            "mqtt_connected": self.mqtt_connected,
            "mqtt_error": self.mqtt_error,
            "mqtt_host": f"{self.config.mqtt_host}:{self.config.mqtt_port}",
            "base_topic": self.config.base_topic,
            "discovery_prefix": self.config.discovery_prefix,
            "pairing": self.pairing,
            "pairing_left": max(0, int(self.pair_until - time.monotonic())),
            # Reported rather than reassembled in the browser, so the topic
            # scheme is defined in exactly one place.
            "bridge_topics": {
                "availability": self.topics.availability(),
                "pair": self.topics.pair_command(),
            },
            "devices": [
                {
                    "id": device_id,
                    "eep": device.eep,
                    "paired_at": device.paired_at,
                    "refresh_topic": self.topics.refresh_command(device_id),
                    "units": [
                        {
                            "unit": unit,
                            "last_seen": int(time.time() - st.last_seen) if st.last_seen else None,
                            "fields": dict(st.fields),
                            "topics": {
                                "state": self.topics.state(device_id, unit),
                                "set": self.topics.light_command(device_id, unit),
                                "vtl": self.topics.vtl_command(device_id, unit),
                            },
                        }
                        for (d, unit), st in sorted(units.items()) if d == device_id
                    ],
                }
                for device_id, device in sorted(devices.items())
            ],
            "log": log,
        }

    def telegram_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.telegrams)
