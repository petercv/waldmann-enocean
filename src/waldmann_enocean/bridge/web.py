"""Web UI: status, pairing, configuration and debug tools.

Authentication is a login form that returns a bearer token, rather than HTTP
Basic: browsers increasingly refuse to show the Basic auth dialog on
plain-HTTP origins, leaving you looking at "Authentication required" with
nowhere to type.  A token in localStorage also avoids cookie SameSite rules,
which silently drop the session when the page is framed.  Basic auth is still
*accepted* so curl and scripts keep working.

The page itself is public (it holds no data); every API endpoint requires a
session.  Nothing here touches the serial port - actions are queued for the
main loop, which is the only thread allowed to transmit.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Config, check_password, set_password
from .core import GET_NAMES, Bridge

LOG = logging.getLogger("waldmann_enocean.bridge.web")

INDEX = Path(__file__).with_name("index.html")

SESSION_TTL = 7 * 24 * 3600
_sessions: dict[str, float] = {}

# Every request body here is a small JSON object.  The limit is checked before
# authentication, so an anonymous client can't make a handler buffer gigabytes.
MAX_BODY = 64 * 1024

# Delay after a failed password check, for the login form and Basic auth alike,
# so neither can be used to guess passwords at full speed.
FAILED_LOGIN_DELAY = 0.5


def _new_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    for old, expiry in list(_sessions.items()):  # drop anything stale
        if expiry < time.time():
            _sessions.pop(old, None)
    return token


def make_handler(bridge: Bridge, config_path: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "waldmann-bridge"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            LOG.debug("web: " + fmt, *args)

        # -- plumbing -------------------------------------------------------

        def _authorised(self) -> bool:
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                return _sessions.get(header[7:].strip(), 0) > time.time()
            # Basic auth is still accepted, for curl and scripted use
            if header.startswith("Basic "):
                try:
                    user, _, password = base64.b64decode(header[6:]).decode().partition(":")
                except Exception:
                    return False
                if check_password(bridge.config, user, password):
                    return True
                time.sleep(FAILED_LOGIN_DELAY)
            return False

        def _deny(self) -> None:
            # No WWW-Authenticate: the page shows its own login form, and the
            # browser dialog would only compete with it.
            self._json({"error": "Sign in first."}, 401)

        def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, code: int = 200) -> None:
            self._send(json.dumps(payload).encode(), "application/json", code)

        def _body(self) -> dict[str, Any] | None:
            """The JSON body, {} if there is none, None if it is too large."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY:
                return None
            if length <= 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                return data if isinstance(data, dict) else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}

        # -- routing --------------------------------------------------------

        def do_GET(self) -> None:
            try:
                self._get()
            except Exception as exc:
                LOG.exception("GET %s failed", self.path)
                self._json({"error": str(exc)}, 500)

        def do_POST(self) -> None:
            try:
                self._post()
            except Exception as exc:
                LOG.exception("POST %s failed", self.path)
                self._json({"error": str(exc)}, 500)

        def _get(self) -> None:
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                try:
                    body = INDEX.read_bytes()
                except OSError as exc:
                    return self._json({"error": f"index.html missing: {exc}"}, 500)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not self._authorised():
                return self._deny()
            if path == "/api/state":
                return self._json(bridge.snapshot())
            if path == "/api/telegrams":
                return self._json(bridge.telegram_log())
            if path == "/api/config":
                return self._json(bridge.config.public())
            if path == "/api/ports":
                from ..protocol import candidate_ports

                return self._json({
                    "ports": candidate_ports(),
                    "active": bridge.dongle.port if bridge.dongle else "",
                })
            self._json({"error": "Not found."}, 404)

        def _post(self) -> None:
            path = self.path.split("?")[0]
            data = self._body()
            if data is None:
                self.close_connection = True  # the unread body is still in the socket
                return self._json({"error": "Request body too large."}, 413)

            if path == "/api/login":
                username = str(data.get("username", ""))
                password = str(data.get("password", ""))
                if not check_password(bridge.config, username, password):
                    time.sleep(FAILED_LOGIN_DELAY)
                    return self._json({"error": "Wrong username or password."}, 401)
                bridge.note(f"web login by {username!r}")
                return self._json({"ok": True, "token": _new_session()})
            if path == "/api/logout":
                header = self.headers.get("Authorization", "")
                if header.startswith("Bearer "):
                    _sessions.pop(header[7:].strip(), None)
                return self._json({"ok": True})

            if not self._authorised():
                return self._deny()

            if path == "/api/pair":
                if bridge.dongle is None:
                    return self._json({"error": "The USB stick isn't connected."})
                seconds = int(data.get("seconds") or bridge.config.pair_window)
                bridge.commands.put(("pair", (seconds,)))
                return self._json({"ok": True})
            if path == "/api/pair/stop":
                bridge.commands.put(("stop_pair", ()))
                return self._json({"ok": True})
            if path == "/api/forget":
                bridge.commands.put(("forget", (str(data.get("id", "")).upper(),)))
                return self._json({"ok": True})
            if path == "/api/rediscover":
                bridge.commands.put(("rediscover", ()))
                return self._json({"ok": True})
            if path == "/api/command":
                return self._json(self._command(data))
            if path == "/api/raw":
                return self._json(self._raw(data))
            if path == "/api/password":
                return self._json(self._password(data))
            if path == "/api/config":
                return self._json(self._config(data))
            self._json({"error": "Not found."}, 404)

        # -- actions --------------------------------------------------------

        def _command(self, data: dict[str, Any]) -> dict[str, Any]:
            device = str(data.get("device", "")).upper()
            unit = int(data.get("unit") or 0)
            action = str(data.get("action", ""))
            if device not in bridge.devices:
                return {"error": "Unknown luminaire."}
            if bridge.dongle is None:
                return {"error": "The USB stick isn't connected."}
            if action == "get":
                which = str(data.get("value", "status"))
                if which not in GET_NAMES:
                    return {"error": f"Unknown request: {which}."}
                bridge.commands.put(("get", (device, unit, which)))
            elif action == "refresh":
                bridge.commands.put(("refresh", (device,)))
            elif action == "vtl":
                bridge.commands.put(("vtl", (device, unit, str(data.get("value", "off")))))
            elif action == "set":
                fields = {k: data[k] for k in ("mode", "brightness", "kelvin", "vtl")
                          if data.get(k) not in (None, "")}
                if not fields:
                    return {"error": "Nothing to set."}
                bridge.commands.put(("set_fields", (device, unit, fields)))
            elif action == "light":
                payload = {"state": data.get("state", "ON")}
                if data.get("brightness") not in (None, ""):
                    payload["brightness"] = round(float(data["brightness"]) / 100 * 255)
                if data.get("kelvin") not in (None, ""):
                    payload["color_temp"] = int(data["kelvin"])
                bridge.commands.put(("light", (device, unit, json.dumps(payload))))
            else:
                return {"error": f"Unknown action: {action}."}
            return {"ok": True}

        def _raw(self, data: dict[str, Any]) -> dict[str, Any]:
            device = str(data.get("device", "")).upper()
            if device not in bridge.devices:
                return {"error": "Unknown luminaire."}
            if bridge.dongle is None:
                return {"error": "The USB stick isn't connected."}
            try:
                payload = bytes.fromhex(str(data.get("hex", "")).replace(" ", ""))
            except ValueError:
                return {"error": "The payload isn't valid hex."}
            if not payload:
                return {"error": "The payload is empty."}
            bridge.commands.put(("raw", (device, payload)))
            return {"ok": True, "sent": payload.hex(" ").upper()}

        def _password(self, data: dict[str, Any]) -> dict[str, Any]:
            """Change the username, the password, or both.

            An empty `new` keeps the current password, so the username can be
            changed on its own - but the current password is required either
            way, which is why this cannot go through _config.
            """
            current = str(data.get("current", ""))
            new = str(data.get("new", ""))
            if not check_password(bridge.config, bridge.config.web_username, current):
                return {"error": "The current password is wrong."}
            if new and len(new) < 8:
                return {"error": "The new password must be at least 8 characters."}
            username = str(data.get("username") or bridge.config.web_username)
            changed = []
            if username != bridge.config.web_username:
                bridge.config.web_username = username
                changed.append("username")
            if new:
                set_password(bridge.config, new)
                changed.append("password")
            if not changed:
                return {"ok": True, "changed": []}
            bridge.config.save(config_path)
            _sessions.clear()
            bridge.note(f"web {' and '.join(changed)} changed - all sessions signed out")
            return {"ok": True, "changed": changed}

        def _config(self, data: dict[str, Any]) -> dict[str, Any]:
            config = bridge.config
            # Check everything before changing anything: stopping halfway
            # would leave the running config different from both the file and
            # what the UI shows, without the MQTT reconnect.
            updates: dict[str, Any] = {}
            for key, value in data.items():
                if key in Config.SECRET_FIELDS or key in Config.PROTECTED_FIELDS:
                    continue
                if not hasattr(config, key):
                    continue
                try:
                    new = config.coerce(key, value)
                except ValueError as exc:
                    return {"error": str(exc), "field": key}
                if new != getattr(config, key):
                    updates[key] = new
            # the MQTT password is write-only: only set when non-empty
            if data.get("mqtt_password"):
                updates["mqtt_password"] = str(data["mqtt_password"])
            for key, new in updates.items():
                setattr(config, key, new)
            changed = list(updates)
            config.save(config_path)
            bridge.note(f"configuration updated: {', '.join(changed) or 'no changes'}")
            # MQTT and stick settings are applied live; only the web listener
            # itself needs the service restarted.
            if any(k.startswith("mqtt_") or k in ("base_topic", "discovery_prefix")
                   for k in changed):
                bridge.commands.put(("reconnect_mqtt", ()))
            if any(k in ("port", "baudrate") for k in changed):
                bridge.commands.put(("reopen_stick", ()))
            restart = any(k.startswith("web_") for k in changed)
            return {"ok": True, "changed": changed, "restart_required": restart}

    return Handler


def serve(bridge: Bridge, config_path: Path) -> ThreadingHTTPServer:
    handler = make_handler(bridge, config_path)
    server = ThreadingHTTPServer((bridge.config.web_host, bridge.config.web_port), handler)
    server.daemon_threads = True
    return server
