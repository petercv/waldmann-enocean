"""Configuration and web authentication for the bridge."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from ..paths import write_atomic

LOG = logging.getLogger("waldmann_enocean.bridge.config")

PBKDF2_ROUNDS = 200_000

# Allowed range per numeric setting.  Outside these the bridge would misbehave
# rather than fail: a poll interval of 0 polls on every loop iteration, and a
# sender offset past 127 leaves the stick's Base ID block.
LIMITS = {
    "baudrate": (1200, 1_000_000),
    "sender_offset": (0, 127),
    "mqtt_port": (1, 65535),
    "poll_interval": (30, 86_400),
    "maintenance_interval": (60, 7 * 86_400),
    "pair_window": (10, 3600),
    "web_port": (1, 65535),
}
TOPIC_FIELDS = ("base_topic", "discovery_prefix")


@dataclass
class Config:
    # radio
    port: str = ""  # empty = autodetect
    baudrate: int = 57600
    sender_offset: int = 0

    # mqtt
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_tls: bool = False
    mqtt_tls_ca: str = ""  # CA file; empty = the system trust store
    mqtt_tls_insecure: bool = False  # skip hostname/chain checks
    mqtt_client_id: str = "waldmann-enocean"
    base_topic: str = "waldmann-enocean"
    discovery_prefix: str = "homeassistant"

    # behaviour
    poll_interval: int = 600  # s; the luminaire also broadcasts on its own
    maintenance_interval: int = 3600  # s; never sent unsolicited
    pair_window: int = 120  # s a pairing session stays open

    # web ui
    web_host: str = "0.0.0.0"
    web_port: int = 8099
    web_username: str = "admin"
    web_password_hash: str = ""  # pbkdf2, set on first run
    web_salt: str = ""

    # fields never sent to the browser
    SECRET_FIELDS = ("mqtt_password", "web_password_hash", "web_salt")
    # readable, but only changeable through the credentials endpoint, which
    # demands the current password first
    PROTECTED_FIELDS = ("web_username",)

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("cannot read %s: %s - using defaults", path, exc)
            return cls()
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path) -> None:
        # 0600: it holds the MQTT password and the web password hash
        write_atomic(path, json.dumps(asdict(self), indent=2) + "\n", mode=0o600)

    def coerce(self, key: str, value: object) -> object:
        """`value` converted to the type of setting `key`, or ValueError.

        The messages leave out the setting's name, so the web UI can put its
        own label in front of them.
        """
        current = getattr(self, key)
        if isinstance(current, bool):
            return value in (True, "true", "on", "1", 1)
        if isinstance(current, int):
            try:
                number = int(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise ValueError("must be a whole number.") from None
            low, high = LIMITS.get(key, (None, None))
            if low is not None and not low <= number <= high:
                raise ValueError(f"must be between {low} and {high}.")
            return number
        text = str(value).strip()
        if key in TOPIC_FIELDS:
            if not text or text.startswith("/") or text.endswith("/"):
                raise ValueError("can't be empty or start or end with a /.")
            if "+" in text or "#" in text:
                raise ValueError("can't contain the MQTT wildcards + or #.")
        return text

    def public(self) -> dict[str, object]:
        """Everything the web UI may see, with secrets masked."""
        data = asdict(self)
        for name in self.SECRET_FIELDS:
            data.pop(name, None)
        data["mqtt_password_set"] = bool(self.mqtt_password)
        return data


# ---------------------------------------------------------------------------
# Web authentication
# ---------------------------------------------------------------------------


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS
    ).hex()


def set_password(config: Config, password: str) -> None:
    config.web_salt = secrets.token_hex(16)
    config.web_password_hash = hash_password(password, config.web_salt)


def check_password(config: Config, username: str, password: str) -> bool:
    if not config.web_password_hash:
        return False
    # compare_digest refuses str with non-ASCII characters, so compare bytes
    if not hmac.compare_digest(username.encode(), config.web_username.encode()):
        return False
    return hmac.compare_digest(
        hash_password(password, config.web_salt), config.web_password_hash
    )


def ensure_password(config: Config, path: Path) -> str | None:
    """Generate a password on first run.  Returns it once, for logging."""
    if config.web_password_hash:
        return None
    generated = secrets.token_urlsafe(12)
    set_password(config, generated)
    config.save(path)
    return generated
