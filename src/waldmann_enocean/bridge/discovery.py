"""Home Assistant MQTT Discovery payloads.

HA never learns that EnOcean is involved: it sees a device with a light per
luminaire head plus its sensors, created from the retained config topics
published here.
"""

from __future__ import annotations

from typing import Any

from .config import Config

MANUFACTURER = "Waldmann"
MODEL = "TALK MODUL EnOcean (EEP D2-41-00)"
# Everything Home Assistant keys entities on: the node names in the discovery
# topics, the device identifiers and the unique_ids.  Changing NODE_PREFIX
# orphans the entities HA already has and creates duplicates beside them, so it
# is a breaking change - not something to tidy up in passing.
NODE_PREFIX = "waldmann_enocean"
BRIDGE_ID = f"{NODE_PREFIX}_bridge"

# Kelvin span offered to HA.  The profile allows 0..16000, but no luminaire
# covers that; this is a sane tunable-white range.
MIN_KELVIN = 2700
MAX_KELVIN = 6500

# The VTL chronotypes as light effects, so they can be picked in HA's light
# dialog.  "off" is HA's own name for "no effect" and switches VTL off.
VTL_EFFECTS = {"off": "off", "VTL normal": "normal", "VTL owl": "owl", "VTL lark": "lark"}
EFFECT_BY_VTL = {vtl: effect for effect, vtl in VTL_EFFECTS.items()}

# decoded field -> (HA component, label, extra discovery keys)
SENSORS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "illumination_lx": ("sensor", "Illuminance", {
        "device_class": "illuminance", "unit_of_measurement": "lx",
        "state_class": "measurement"}),
    "temperature_c": ("sensor", "Temperature", {
        "device_class": "temperature", "unit_of_measurement": "°C",
        "state_class": "measurement"}),
    "humidity_pct": ("sensor", "Humidity", {
        "device_class": "humidity", "unit_of_measurement": "%",
        "state_class": "measurement"}),
    "noise_db_a": ("sensor", "Noise", {
        "unit_of_measurement": "dB", "state_class": "measurement",
        "entity_category": "diagnostic"}),
    "voc_ppb": ("sensor", "VOC", {
        "device_class": "volatile_organic_compounds_parts",
        "unit_of_measurement": "ppb", "state_class": "measurement"}),
    "operating_hours": ("sensor", "Operating hours", {
        "unit_of_measurement": "h", "state_class": "total_increasing",
        "entity_category": "diagnostic"}),
    "operating_hours_active": ("sensor", "Active hours", {
        "unit_of_measurement": "h", "state_class": "total_increasing",
        "entity_category": "diagnostic"}),
    "power_w": ("sensor", "Power", {
        "device_class": "power", "unit_of_measurement": "W",
        "state_class": "measurement"}),
    "energy_kwh": ("sensor", "Energy", {
        "device_class": "energy", "unit_of_measurement": "kWh",
        "state_class": "total_increasing"}),
}


class Topics:
    """Every topic the bridge uses, derived from the configured base topic."""

    def __init__(self, config: Config) -> None:
        self.base = config.base_topic
        self.prefix = config.discovery_prefix

    def availability(self) -> str:
        return f"{self.base}/bridge/status"

    def pair_command(self) -> str:
        return f"{self.base}/bridge/pair/set"

    def state(self, device_id: str, unit: int) -> str:
        return f"{self.base}/{device_id}/unit{unit}/state"

    def light_command(self, device_id: str, unit: int) -> str:
        return f"{self.base}/{device_id}/unit{unit}/set"

    def vtl_command(self, device_id: str, unit: int) -> str:
        return f"{self.base}/{device_id}/unit{unit}/vtl/set"

    def refresh_command(self, device_id: str) -> str:
        return f"{self.base}/{device_id}/refresh/set"

    def discovery(self, component: str, node: str, obj: str) -> str:
        return f"{self.prefix}/{component}/{node}/{obj}/config"


def device_block(device_id: str) -> dict[str, Any]:
    return {
        "identifiers": [f"{NODE_PREFIX}_{device_id}"],
        "name": f"Waldmann {device_id}",
        "manufacturer": MANUFACTURER,
        "model": MODEL,
        "via_device": BRIDGE_ID,
    }


def bridge_entities(topics: Topics) -> list[tuple[str, dict[str, Any]]]:
    """The bridge's own device, carrying the pairing button."""
    device = {
        "identifiers": [BRIDGE_ID],
        "name": "Waldmann EnOcean bridge",
        "manufacturer": MANUFACTURER,
        "model": "EnOcean/MQTT bridge",
    }
    return [(
        topics.discovery("button", BRIDGE_ID, "pair"),
        {
            "name": "Pair new luminaire",
            "unique_id": f"{BRIDGE_ID}_pair",
            "command_topic": topics.pair_command(),
            "availability_topic": topics.availability(),
            "device": device,
        },
    )]


def unit_entities(
    topics: Topics, device_id: str, unit: int, vtl_options: list[str]
) -> list[tuple[str, dict[str, Any]]]:
    """Entities that exist for every head, regardless of what it reports."""
    state = topics.state(device_id, unit)
    avail = topics.availability()
    device = device_block(device_id)
    uid = f"{NODE_PREFIX}_{device_id}_u{unit}"
    node = f"{NODE_PREFIX}_{device_id}"
    return [
        (topics.discovery("light", node, f"u{unit}"), {
            "schema": "json",
            "name": f"Head {unit}",
            "unique_id": f"{uid}_light",
            "state_topic": state,
            "command_topic": topics.light_command(device_id, unit),
            "brightness": True,
            "supported_color_modes": ["color_temp"],
            "color_temp_kelvin": True,
            "min_kelvin": MIN_KELVIN,
            "max_kelvin": MAX_KELVIN,
            "effect": True,
            "effect_list": list(VTL_EFFECTS),
            "availability_topic": avail,
            "device": device,
        }),
        (topics.discovery("select", node, f"u{unit}_vtl"), {
            "name": f"Head {unit} VTL",
            "unique_id": f"{uid}_vtl",
            "state_topic": state,
            "value_template": "{{ value_json.vtl }}",
            "command_topic": topics.vtl_command(device_id, unit),
            "options": vtl_options,
            "entity_category": "config",
            "availability_topic": avail,
            "device": device,
        }),
        (topics.discovery("binary_sensor", node, f"u{unit}_presence"), {
            "name": f"Head {unit} presence",
            "unique_id": f"{uid}_presence",
            "state_topic": state,
            "value_template": "{{ 'ON' if value_json.presence == 'presence' else 'OFF' }}",
            "device_class": "occupancy",
            "availability_topic": avail,
            "device": device,
        }),
    ]


def sensor_entity(
    topics: Topics, device_id: str, unit: int, key: str
) -> tuple[str, dict[str, Any]] | None:
    """A sensor, announced only once the luminaire has reported a real value."""
    spec = SENSORS.get(key)
    if spec is None:
        return None
    component, label, extra = spec
    node = f"{NODE_PREFIX}_{device_id}"
    return (
        topics.discovery(component, node, f"u{unit}_{key}"),
        {
            "name": f"Head {unit} {label}",
            "unique_id": f"{NODE_PREFIX}_{device_id}_u{unit}_{key}",
            "state_topic": topics.state(device_id, unit),
            "value_template": "{{ value_json.%s }}" % key,
            "availability_topic": topics.availability(),
            "device": device_block(device_id),
            **extra,
        },
    )


def device_entity_topics(topics: Topics, device_id: str, units: list[int]) -> list[str]:
    """Every discovery topic a device owns, for removal on unpair."""
    node = f"{NODE_PREFIX}_{device_id}"
    result = [topics.discovery("button", node, "refresh")]
    for unit in units:
        result += [
            topics.discovery("light", node, f"u{unit}"),
            topics.discovery("select", node, f"u{unit}_vtl"),
            topics.discovery("binary_sensor", node, f"u{unit}_presence"),
        ]
        for key, (component, _, _) in SENSORS.items():
            result.append(topics.discovery(component, node, f"u{unit}_{key}"))
    return result


def refresh_button(topics: Topics, device_id: str) -> tuple[str, dict[str, Any]]:
    return (
        topics.discovery("button", f"{NODE_PREFIX}_{device_id}", "refresh"),
        {
            "name": "Refresh",
            "unique_id": f"{NODE_PREFIX}_{device_id}_refresh",
            "command_topic": topics.refresh_command(device_id),
            "entity_category": "config",
            "availability_topic": topics.availability(),
            "device": device_block(device_id),
        },
    )
