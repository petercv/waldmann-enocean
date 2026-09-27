"""MQTT bridge and web UI for Waldmann EnOcean luminaires (EEP D2-41-00).

Runs as a service, owns the EnOcean USB stick, and presents the luminaires to
Home Assistant purely as MQTT.  HA never learns that EnOcean is involved: the
bridge publishes MQTT Discovery configs, so pairing a luminaire makes a device
with its lights and sensors appear in HA by itself.

    waldmann-bridge --mqtt-host 192.168.1.10

Then open http://<host>:8099/ for status, pairing, debug tools and settings.
A web password is generated on first run and printed to the log.

Layout:
    ../protocol.py  the EnOcean protocol, shared with the `waldmann` CLI
    config.py       settings file and web authentication
    discovery.py    Home Assistant MQTT Discovery payloads
    core.py         the bridge: serial loop, MQTT, state
    web.py          the web UI and its JSON API
    index.html      the page
    service.py      --install-service, the systemd unit

Dependencies: pyserial, paho-mqtt.  The web UI is stdlib only.
"""

from __future__ import annotations

import argparse
import logging
import threading
from pathlib import Path

from ..paths import default_config, default_store
from .config import Config, ensure_password, set_password
from .core import Bridge
from .web import serve

LOG = logging.getLogger("waldmann_enocean.bridge")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="MQTT bridge and web UI for Waldmann EnOcean luminaires."
    )
    parser.add_argument("--config", type=Path, default=default_config(),
                        help="settings file (default: %(default)s)")
    parser.add_argument("--store", type=Path, default=default_store(),
                        help="paired-device file (default: %(default)s)")
    parser.add_argument("--port", help="serial port (default: autodetect)")
    parser.add_argument("--mqtt-host")
    parser.add_argument("--mqtt-port", type=int)
    parser.add_argument("--mqtt-username")
    parser.add_argument("--mqtt-password")
    parser.add_argument("--base-topic")
    parser.add_argument("--web-port", type=int)
    parser.add_argument("--web-password", help="set the web password and exit")
    parser.add_argument("--install-service", action="store_true",
                        help="install and start the systemd service (needs root)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.install_service:
        # before loading the config, which would create one in root's home
        from .service import install
        return install()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = Config.load(args.config)
    for key in ("port", "mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password",
                "base_topic", "web_port"):
        value = getattr(args, key, None)
        if value is not None:
            setattr(config, key, value)

    if args.web_password:
        set_password(config, args.web_password)
        config.save(args.config)
        print(f"Web password set for user {config.web_username!r}.")
        return 0

    config.save(args.config)
    generated = ensure_password(config, args.config)
    if generated:
        LOG.warning(
            "\n%s\n  Web UI password generated on first run:\n"
            "      username: %s\n      password: %s\n"
            "  Change it under Settings, or with --web-password.\n%s",
            "=" * 62, config.web_username, generated, "=" * 62,
        )

    bridge = Bridge(config, args.store)
    server = serve(bridge, args.config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    bridge.note(f"web UI on http://{config.web_host}:{config.web_port}/")

    try:
        bridge.run()
    except KeyboardInterrupt:
        bridge.note("stopping")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":  # python -m waldmann_enocean.bridge.main
    raise SystemExit(main())
