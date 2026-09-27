"""EnOcean ESP3 and EEP D2-41-00, spoken directly to a USB stick.

The protocol layer: framing, the radio telegrams, the profile's encoders and
decoders, the UTE teach-in and the device store.  No command line and no MQTT,
so both front ends sit on exactly the same implementation.

Sources:
  * EEP D2-41-00 "Status Data, Sensor Data, Maintenance Data, Light Control"
    (submitter Waldmann GmbH, EnOcean Alliance, 2022-09-05).
  * Waldmann TALK MODUL EnOcean manual 405488810, ch. 7 "Communicating with an
    external receiver" - the luminaire sends the UTE teach-in to us.

Nothing here is taken on faith from other tooling: every field offset below is
the one printed in the EEP table, and telegram decoding cross-checks the
payload length so a mis-framed telegram is reported instead of guessed at.
"""

from __future__ import annotations

import glob
import json
import logging
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .paths import write_atomic

try:
    import serial  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    sys.exit("pyserial is missing.  Install it with:  pip install pyserial")

LOG = logging.getLogger("waldmann_enocean")

BROADCAST = b"\xff\xff\xff\xff"
WALDMANN_MANUFACTURER = 0x02E

# ---------------------------------------------------------------------------
# ESP3 serial protocol
# ---------------------------------------------------------------------------

PACKET_RADIO_ERP1 = 0x01
PACKET_RESPONSE = 0x02
PACKET_EVENT = 0x04
PACKET_COMMON_COMMAND = 0x05

# Far above anything the stick sends (a radio telegram is under 40 bytes, the
# largest common-command response about 33), far below the 64 KB the length
# field allows.
MAX_FRAME_DATA = 1024

CO_RD_VERSION = 0x03
CO_RD_IDBASE = 0x08

RORG_RPS = 0xF6
RORG_ADT = 0xA6
RORG_SIGNAL = 0xD0
RORG_VLD = 0xD2
RORG_UTE = 0xD4

RETURN_CODES = {
    0x00: "OK",
    0x01: "ERROR",
    0x02: "NOT_SUPPORTED",
    0x03: "WRONG_PARAM",
    0x04: "OPERATION_DENIED",
    0x05: "LOCK_SET",
    0x06: "BUFFER_TOO_SMALL",
    0x07: "NO_FREE_BUFFER",
}


def _crc8_table() -> list[int]:
    table = []
    for value in range(256):
        crc = value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return table


_CRC8 = _crc8_table()


def crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = _CRC8[crc ^ byte]
    return crc


@dataclass
class Packet:
    packet_type: int
    data: bytes
    optional: bytes

    def __str__(self) -> str:
        return (
            f"type=0x{self.packet_type:02X} "
            f"data={self.data.hex(' ').upper()} opt={self.optional.hex(' ').upper()}"
        )


@dataclass
class Radio:
    """An ERP1 radio telegram: RORG + payload + sender + status."""

    rorg: int
    payload: bytes
    sender: bytes
    status: int
    destination: bytes | None = None
    dbm: int | None = None

    @property
    def sender_id(self) -> str:
        return self.sender.hex().upper()


def parse_radio(packet: Packet) -> Radio | None:
    if packet.packet_type != PACKET_RADIO_ERP1 or len(packet.data) < 6:
        return None
    rorg = packet.data[0]
    payload = packet.data[1:-5]
    sender = packet.data[-5:-1]
    status = packet.data[-1]
    destination = dbm = None
    if len(packet.optional) >= 6:
        destination = packet.optional[1:5]
        dbm = -packet.optional[5]
    # Addressed telegrams may arrive still wrapped in ADT (RORG 0xA6): the inner
    # telegram is followed by its 4-byte destination.
    if rorg == RORG_ADT and len(payload) >= 5:
        destination = payload[-4:]
        rorg, payload = payload[0], payload[1:-4]
    return Radio(rorg, payload, sender, status, destination, dbm)


class Dongle:
    def __init__(self, port: str, baudrate: int = 57600, dump: bool = False) -> None:
        self.port = port
        self.dump = dump
        self.serial = serial.Serial(port, baudrate, timeout=0.05)
        self._buffer = bytearray()
        self._radio_queue: list[Radio] = []
        self._base_id: bytes | None = None

    def close(self) -> None:
        self.serial.close()

    # -- raw framing --------------------------------------------------------

    def _send(self, packet_type: int, data: bytes, optional: bytes = b"") -> None:
        header = struct.pack(">HBB", len(data), len(optional), packet_type)
        frame = (
            b"\x55"
            + header
            + bytes([crc8(header)])
            + data
            + optional
            + bytes([crc8(data + optional)])
        )
        if self.dump:
            LOG.info("TX ESP3: %s", frame.hex(" ").upper())
        self.serial.write(frame)
        self.serial.flush()

    def _decode_buffer(self) -> Packet | None:
        buf = self._buffer
        while True:
            start = buf.find(0x55)
            if start < 0:
                buf.clear()
                return None
            if start:
                del buf[:start]
            if len(buf) < 7:
                return None
            data_len = (buf[1] << 8) | buf[2]
            opt_len = buf[3]
            packet_type = buf[4]
            # The header CRC is only 8 bits, so a stray 0x55 in the byte stream
            # passes it 1 time in 256.  Real frames are small; without this cap
            # a bogus length makes us wait for up to 64 KB before resyncing,
            # holding back every telegram behind it.
            if crc8(bytes(buf[1:5])) != buf[5] or data_len + opt_len > MAX_FRAME_DATA:
                del buf[0]  # not a real frame start, resync
                continue
            total = 6 + data_len + opt_len + 1
            if len(buf) < total:
                return None
            data = bytes(buf[6 : 6 + data_len])
            optional = bytes(buf[6 + data_len : 6 + data_len + opt_len])
            if crc8(data + optional) != buf[total - 1]:
                LOG.debug("ESP3 data CRC mismatch, resyncing")
                del buf[0]
                continue
            del buf[:total]
            packet = Packet(packet_type, data, optional)
            if self.dump:
                LOG.info("RX ESP3: %s", packet)
            return packet

    def read_packet(self, timeout: float) -> Packet | None:
        deadline = time.monotonic() + timeout
        while True:
            packet = self._decode_buffer()
            if packet is not None:
                return packet
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            chunk = self.serial.read(max(1, self.serial.in_waiting or 1))
            if chunk:
                self._buffer.extend(chunk)

    # -- higher level -------------------------------------------------------

    def read_radio(self, timeout: float) -> Radio | None:
        if self._radio_queue:
            return self._radio_queue.pop(0)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            packet = self.read_packet(remaining)
            if packet is None:
                return None
            if packet.packet_type == PACKET_EVENT:
                LOG.debug("ESP3 event: %s", packet)
                continue
            radio = parse_radio(packet)
            if radio is not None:
                return radio

    def command(self, payload: bytes, timeout: float = 1.0) -> Packet:
        """Send a common command and return its RESPONSE packet."""
        self._send(PACKET_COMMON_COMMAND, payload)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no ESP3 response from the USB stick")
            packet = self.read_packet(remaining)
            if packet is None:
                continue
            if packet.packet_type == PACKET_RESPONSE:
                return packet
            radio = parse_radio(packet)
            if radio is not None:  # don't drop telegrams that raced the response
                self._radio_queue.append(radio)

    def base_id(self) -> bytes:
        """The stick's Base ID.  Fixed in the chip, so read it only once."""
        if self._base_id is None:
            response = self.command(bytes([CO_RD_IDBASE]))
            if not response.data or response.data[0] != 0x00 or len(response.data) < 5:
                raise RuntimeError(f"CO_RD_IDBASE failed: {response}")
            self._base_id = response.data[1:5]
        return self._base_id

    def version(self) -> dict[str, object]:
        response = self.command(bytes([CO_RD_VERSION]))
        if not response.data or response.data[0] != 0x00 or len(response.data) < 33:
            raise RuntimeError(f"CO_RD_VERSION failed: {response}")
        body = response.data
        return {
            "app_version": ".".join(str(b) for b in body[1:5]),
            "api_version": ".".join(str(b) for b in body[5:9]),
            "chip_id": body[9:13].hex().upper(),
            "chip_version": body[13:17].hex().upper(),
            "description": body[17:33].rstrip(b"\x00\xff").decode("ascii", "replace"),
        }

    def send_radio(
        self,
        rorg: int,
        payload: bytes,
        sender: bytes,
        destination: bytes = BROADCAST,
        status: int = 0x00,
    ) -> None:
        data = bytes([rorg]) + payload + sender + bytes([status])
        optional = bytes([0x03]) + destination + bytes([0xFF, 0x00])
        self._send(PACKET_RADIO_ERP1, data, optional)
        deadline = time.monotonic() + 1.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                LOG.warning("USB stick did not acknowledge the transmission")
                return
            packet = self.read_packet(remaining)
            if packet is None:
                continue
            if packet.packet_type == PACKET_RESPONSE:
                code = packet.data[0] if packet.data else 0xFF
                if code != 0x00:
                    LOG.error(
                        "USB stick rejected the telegram: %s",
                        RETURN_CODES.get(code, f"0x{code:02X}"),
                    )
                return
            radio = parse_radio(packet)
            if radio is not None:
                self._radio_queue.append(radio)


# ---------------------------------------------------------------------------
# Bit helpers - EEP offsets count bits from the MSB of the first payload byte
# ---------------------------------------------------------------------------


def get_bits(data: bytes, offset: int, size: int) -> int:
    if (offset + size) > len(data) * 8:
        raise ValueError(f"payload too short for bits {offset}+{size}")
    value = int.from_bytes(data, "big")
    return (value >> (len(data) * 8 - offset - size)) & ((1 << size) - 1)


class BitWriter:
    def __init__(self) -> None:
        self._value = 0
        self._bits = 0

    def add(self, value: int, size: int) -> "BitWriter":
        self._value = (self._value << size) | (int(value) & ((1 << size) - 1))
        self._bits += size
        return self

    def bytes(self) -> bytes:
        if self._bits % 8:
            raise ValueError("payload is not a whole number of bytes")
        return self._value.to_bytes(self._bits // 8, "big")


# ---------------------------------------------------------------------------
# EEP D2-41-00
# ---------------------------------------------------------------------------

GET_PRODUCT_STATUS = 0
GET_UNIT_STATUS = 1
GET_PRESENCE_DATA = 2
GET_ENVIRONMENTAL_DATA = 3
GET_MAINTENANCE_DATA = 4
PRODUCT_STATUS = 5
TRIGGER_UNIT_OPERATING_STATE = 6
UNIT_OPERATING_STATE = 7
SET_UNIT_DATA = 8
UNIT_STATUS = 9
SET_OCCUPANCY = 10
PRESENCE_DATA = 11
ENVIRONMENTAL_DATA = 12
MAINTENANCE_DATA = 13

COMMAND_NAMES = {
    0: "Get Product Status",
    1: "Get Unit Status",
    2: "Get Presence Data",
    3: "Get Environmental Data",
    4: "Get Maintenance Data",
    5: "Product Status",
    6: "Trigger Unit Operating State",
    7: "Unit Operating State",
    8: "Set Unit Data",
    9: "Unit Status",
    10: "Set Occupancy",
    11: "Presence Data",
    12: "Environmental Data",
    13: "Maintenance Data",
}

# Payload length in bytes for every telegram the luminaire sends back.  Used to
# sanity-check the unit/command nibble split instead of trusting it blindly.
INBOUND_LENGTHS = {
    PRODUCT_STATUS: 3,
    UNIT_OPERATING_STATE: 2,
    UNIT_STATUS: 6,
    PRESENCE_DATA: 2,
    ENVIRONMENTAL_DATA: 8,
    MAINTENANCE_DATA: 9,
}

ILLUMINATION_MODES = {0: "off", 1: "reduced light", 2: "working light", 3: "service light"}
MODE_VALUES = {name: value for value, name in ILLUMINATION_MODES.items()}
MODE_BY_NAME = {
    "off": 0,
    "reduced": 1,
    "working": 2,
    "service": 3,
}
SWITCH_CONTROL = {0: "store", 1: "switch", 15: "not used"}
VTL_BY_NAME = {"off": 0, "normal": 1, "owl": 2, "lark": 3}
VTL_MODES = {0: "off", 1: "normal", 2: "owl", 3: "lark"}
FADING_MODES = {0: "direct", 1: "runtime"}
TRIGGER_STATES = {
    "off": 0,
    "reduced": 1,
    "working": 2,
    "service": 3,
    "clear-off": 4,
    "clear-reduced": 5,
    "clear-working": 6,
    "clear-service": 7,
}

NO_CHANGE_4 = 15
NO_CHANGE_DIM = 255
NO_CHANGE_CT = 16383


def encode_get(unit: int, command: int) -> bytes:
    """Get Command: Unit Index (bits 0-3) + Command ID (bits 4-7)."""
    return BitWriter().add(unit, 4).add(command, 4).bytes()


def encode_set_unit_data(
    unit: int,
    mode: int = NO_CHANGE_4,
    switch: int = 1,
    vtl: int = NO_CHANGE_4,
    fading: int = NO_CHANGE_4,
    dim_raw: int = NO_CHANGE_DIM,
    color_temp: int = NO_CHANGE_CT,
) -> bytes:
    return (
        BitWriter()
        .add(unit, 4)
        .add(SET_UNIT_DATA, 4)
        .add(mode, 4)
        .add(switch, 4)
        .add(vtl, 4)
        .add(fading, 4)
        .add(dim_raw, 8)
        .add(0, 2)
        .add(color_temp, 14)
        .bytes()
    )


def encode_trigger(unit: int, state: int) -> bytes:
    return (
        BitWriter()
        .add(unit, 4)
        .add(TRIGGER_UNIT_OPERATING_STATE, 4)
        .add(0, 4)
        .add(state, 4)
        .bytes()
    )


# Engineering unit per decoded field, applied when formatting for display.
# Decoders return plain numbers (or None for "not supported") so the values are
# directly usable by machine consumers such as the MQTT bridge.
FIELD_UNITS = {
    "noise_db_a": "dB(A)",
    "voc_ppb": "ppb",
    "illumination_lx": "lx",
    "temperature_c": "C",
    "humidity_pct": "%",
    "operating_hours": "h",
    "operating_hours_active": "h",
    "power_w": "W",
    "energy_kwh": "kWh",
    "dim_percent": "%",
    "color_temp_k": "K",
}


def _scaled(raw: int, not_supported: int, scale: float) -> float | None:
    """Decoded value, or None when the device reports the field unsupported."""
    if raw == not_supported:
        return None
    value = raw * scale
    return round(value, 1) if isinstance(scale, float) and scale != 1 else value


def format_field(key: str, value: object) -> str:
    if value is None:
        return "not supported"
    if isinstance(value, (int, float)) and key in FIELD_UNITS:
        return f"{value:g} {FIELD_UNITS[key]}"
    return str(value)


def decode_d2_41_00(payload: bytes) -> tuple[int, int, dict[str, object]]:
    """Return (unit index, command id, decoded fields).

    The first byte packs Unit Index into bits 0-3 and Command ID into bits 4-7
    (EEP table, offset 0/size 4 and offset 4/size 4).  We verify that reading
    against the documented payload length and fall back to the swapped nibbles
    if - and only if - that is the reading which fits, so a wrong assumption
    shows up as a warning rather than as plausible nonsense.
    """
    if not payload:
        raise ValueError("empty D2 payload")
    unit = get_bits(payload, 0, 4)
    command = get_bits(payload, 4, 4)
    expected = INBOUND_LENGTHS.get(command)
    if expected != len(payload):
        swapped_command, swapped_unit = unit, command
        if INBOUND_LENGTHS.get(swapped_command) == len(payload):
            LOG.warning(
                "telegram %s fits command 0x%X only with swapped nibbles "
                "(unit=%d) - byte order differs from the EEP table",
                payload.hex().upper(),
                swapped_command,
                swapped_unit,
            )
            unit, command = swapped_unit, swapped_command
        elif expected is not None:
            LOG.warning(
                "command %s (0x%X) should carry %d payload bytes, got %d: %s",
                COMMAND_NAMES.get(command, "?"),
                command,
                expected,
                len(payload),
                payload.hex().upper(),
            )

    fields: dict[str, object] = {}
    if command == PRODUCT_STATUS and len(payload) >= 3:
        # Unit Activity Status 14 at bit 9 ... Unit Activity Status 0 at bit 23.
        fields["active_units"] = [i for i in range(15) if get_bits(payload, 23 - i, 1)]
    elif command == UNIT_OPERATING_STATE and len(payload) >= 2:
        state = get_bits(payload, 12, 4)
        fields["illumination_mode"] = ILLUMINATION_MODES.get(
            state, "not supported" if state == 15 else f"reserved({state})"
        )
    elif command == UNIT_STATUS and len(payload) >= 6:
        mode = get_bits(payload, 8, 4)
        dim = get_bits(payload, 24, 8)
        color_temp = get_bits(payload, 34, 14)
        fields["illumination_mode"] = ILLUMINATION_MODES.get(
            mode, "not supported" if mode == 15 else f"reserved({mode})"
        )
        fields["switch_control"] = SWITCH_CONTROL.get(get_bits(payload, 12, 4), "?")
        vtl = get_bits(payload, 16, 4)
        fields["vtl"] = VTL_MODES.get(vtl, "not supported" if vtl == 15 else f"reserved({vtl})")
        fading = get_bits(payload, 20, 4)
        # 15 means "not used" in a command but "not supported" in a status
        fields["fading"] = FADING_MODES.get(
            fading, "not supported" if fading == 15 else f"reserved({fading})"
        )
        fields["dim_percent"] = None if dim == 255 else round(dim / 2, 1)
        fields["color_temp_k"] = None if color_temp == 16383 else color_temp
    elif command == PRESENCE_DATA and len(payload) >= 2:
        presence = get_bits(payload, 14, 2)
        fields["presence"] = {0: "no presence", 1: "presence", 3: "not supported"}.get(
            presence, f"reserved({presence})"
        )
        for label, offset in (("occupancy_member_1", 11), ("occupancy_member_2", 8)):
            value = get_bits(payload, offset, 3)
            fields[label] = {0: "not occupied", 1: "occupied", 7: "not supported"}.get(
                value, f"reserved({value})"
            )
    elif command == ENVIRONMENTAL_DATA and len(payload) >= 8:
        fields["noise_db_a"] = _scaled(get_bits(payload, 8, 8), 255, 1)
        fields["voc_ppb"] = _scaled(get_bits(payload, 16, 16), 65535, 1)
        fields["illumination_lx"] = _scaled(get_bits(payload, 33, 15), 32767, 1)
        fields["temperature_c"] = _scaled(get_bits(payload, 49, 7), 127, 0.5)
        fields["humidity_pct"] = _scaled(get_bits(payload, 57, 7), 127, 1)
    elif command == MAINTENANCE_DATA and len(payload) >= 9:
        fields["operating_hours"] = _scaled(get_bits(payload, 8, 18), 262143, 1)
        fields["operating_hours_active"] = _scaled(get_bits(payload, 26, 18), 262143, 1)
        fields["power_w"] = _scaled(get_bits(payload, 44, 12), 4095, 0.1)
        fields["energy_kwh"] = _scaled(get_bits(payload, 56, 16), 65535, 1)
    else:
        fields["raw"] = payload.hex().upper()
    return unit, command, fields


# ---------------------------------------------------------------------------
# RPS F6-02-01 - a virtual rocker switch
#
# Besides D2-41-00 the module accepts RPS F6-02-01 and F6-03-01 from a taught
# transmitter (manual ch. 5.5 / 6.1-6.3), so the stick can also act as a plain
# wall switch.
#
# DB0 = R1<<5 | EB<<4 | R2<<1 | SA, and the ERP1 status byte carries the T21/NU
# bits that mark a rocker action: 0x30 while pressed, 0x20 on release.  Getting
# that status byte wrong makes the receiver ignore the telegram entirely.
# ---------------------------------------------------------------------------

# R1 is a 3-bit field, so all four rockers of F6-03-01 are reachable.  F6-02-01
# only uses A and B; the luminaire's profile list names rocker C explicitly
# ("RPS F6-03-01 [C]"), so C and D are worth trying when A does nothing useful.
RPS_BUTTONS = {"AI": 0, "A0": 1, "BI": 2, "B0": 3, "CI": 4, "C0": 5, "DI": 6, "D0": 7}
RPS_STATUS_PRESSED = 0x30
RPS_STATUS_RELEASED = 0x20

# action -> (button, how long to hold it)
SWITCH_ACTIONS = {
    "on": ("AI", 0.15),
    "off": ("A0", 0.15),
    "brighter": ("AI", 2.0),
    "darker": ("A0", 2.0),
    "service": ("BI", 0.15),
}


def rps_press(button: str) -> bytes:
    return bytes([(RPS_BUTTONS[button] << 5) | 0x10])


def decode_rps(payload: bytes, status: int) -> str:
    """Decode F6-02-01.  Status bit 4 (NU) says whether a rocker is identified."""
    if not payload:
        return "(empty)"
    db0 = payload[0]
    names = {value: name for name, value in RPS_BUTTONS.items()}
    if not status & 0x10:  # U-message: no button info, i.e. a release
        return f"release / no button (0x{db0:02X})"
    first = names.get((db0 >> 5) & 0x07, str((db0 >> 5) & 0x07))
    text = f"{first} {'pressed' if db0 & 0x10 else 'released'}"
    if db0 & 0x01:  # second action valid
        text += f" + {names.get((db0 >> 1) & 0x07, (db0 >> 1) & 0x07)}"
    return text


def rps_tap(
    dongle: Dongle,
    sender: bytes,
    destination: bytes,
    button: str,
    hold: float,
    pulse: float = 0.0,
) -> None:
    """Press a button, hold it for `hold` seconds, release it.

    A real rocker sends one telegram on press and one on release, which is what
    pulse=0 does.  Some receivers instead treat the button as released when no
    telegram has arrived for a while, so a long press has to be kept alive by
    retransmitting it: pulse>0 resends the press telegram every `pulse` seconds
    for the duration of the hold.
    """
    payload = rps_press(button)
    dongle.send_radio(RORG_RPS, payload, sender, destination, status=RPS_STATUS_PRESSED)
    if pulse > 0:
        elapsed = 0.0
        while elapsed < hold:
            step = min(pulse, hold - elapsed)
            time.sleep(step)
            elapsed += step
            if elapsed < hold:
                dongle.send_radio(
                    RORG_RPS, payload, sender, destination, status=RPS_STATUS_PRESSED
                )
    else:
        time.sleep(hold)
    dongle.send_radio(RORG_RPS, b"\x00", sender, destination, status=RPS_STATUS_RELEASED)


# ---------------------------------------------------------------------------
# UTE teach-in (RORG 0xD4)
# ---------------------------------------------------------------------------


@dataclass
class UteRequest:
    bidirectional: bool
    response_expected: bool
    request_type: int  # 0=teach-in, 1=delete, 2=either
    command: int  # 0=query, 1=response
    channels: int
    manufacturer: int
    eep: tuple[int, int, int]  # rorg, func, type
    raw: bytes

    @property
    def eep_text(self) -> str:
        return "%02X-%02X-%02X" % self.eep

    @property
    def request_text(self) -> str:
        return {0: "teach-in", 1: "teach-out", 2: "teach-in or teach-out"}.get(
            self.request_type, f"reserved({self.request_type})"
        )


def parse_ute(payload: bytes) -> UteRequest | None:
    if len(payload) < 7:
        return None
    db6, db5, db4, db3, db2, db1, db0 = payload[:7]
    return UteRequest(
        bidirectional=bool(db6 & 0x80),
        response_expected=not (db6 & 0x40),
        request_type=(db6 >> 4) & 0x03,
        command=db6 & 0x0F,
        channels=db5,
        manufacturer=((db3 & 0x07) << 8) | db4,
        eep=(db0, db1, db2),
        raw=bytes(payload[:7]),
    )


UTE_RESPONSE_CODES = {
    0: "request not accepted",
    1: "teach-in successful",
    2: "teach-out successful",
    3: "EEP not supported",
}


def build_ute_query(
    manufacturer: int,
    channels: int = 0xFF,
    eep: tuple[int, int, int] = (0xD2, 0x41, 0x00),
) -> bytes:
    """A UTE teach-in query we send, to get ourselves into the lamp's list.

    DB6 = 0x80: bidirectional, response expected, teach-in request, query.
    """
    rorg, func, type_ = eep
    return bytes(
        [0x80, channels, manufacturer & 0xFF, (manufacturer >> 8) & 0x07, type_, func, rorg]
    )


def build_ute_response(request: UteRequest, response_code: int) -> bytes:
    """Mirror the request back with command id 1 and a response code.

    response_code: 0 = not accepted, 1 = teach-in successful,
                   2 = teach-out successful, 3 = EEP not supported.
    """
    db6 = (0x80 if request.bidirectional else 0x00) | ((response_code & 0x03) << 4) | 0x01
    return bytes([db6]) + request.raw[1:7]


# ---------------------------------------------------------------------------
# Device store
# ---------------------------------------------------------------------------


@dataclass
class Device:
    device_id: str
    eep: str = "D2-41-00"
    manufacturer: int | None = None
    paired_at: str | None = None
    # Base ID + this is the address the luminaire was paired from, and so the
    # only one it takes commands from.  It belongs to the pairing, not to the
    # program sending, so the CLI and the bridge both read it from here.
    sender_offset: int = 0
    notes: dict[str, object] = field(default_factory=dict)

    @property
    def address(self) -> bytes:
        return bytes.fromhex(self.device_id)


# Anything in the store file that is not the device list itself, preserved
# across rewrites.  Holds "switch_offset", the address the virtual rocker
# switch was taught with, which is a separate identity from any pairing.
_STORE_EXTRA: dict[str, object] = {}


def load_devices(path: Path) -> dict[str, Device]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("cannot read %s: %s", path, exc)
        return {}
    _STORE_EXTRA.clear()
    _STORE_EXTRA.update(
        {k: v for k, v in raw.items() if k not in ("devices", "updated_at")}
    )
    devices: dict[str, Device] = {}
    for device_id, body in (raw.get("devices") or {}).items():
        if not isinstance(body, dict):
            continue
        devices[device_id.upper()] = Device(
            device_id=device_id.upper(),
            eep=str(body.get("eep", "D2-41-00")),
            manufacturer=body.get("manufacturer") if isinstance(body.get("manufacturer"), int) else None,
            paired_at=body.get("paired_at") if isinstance(body.get("paired_at"), str) else None,
            sender_offset=body.get("sender_offset")
            if isinstance(body.get("sender_offset"), int) else 0,
            notes=body.get("notes") if isinstance(body.get("notes"), dict) else {},
        )
    return devices


def save_devices(path: Path, devices: dict[str, Device]) -> None:
    payload = {
        **_STORE_EXTRA,
        "devices": {
            device_id: {
                "eep": device.eep,
                "manufacturer": device.manufacturer,
                "paired_at": device.paired_at,
                "sender_offset": device.sender_offset,
                "notes": device.notes,
            }
            for device_id, device in sorted(devices.items())
        },
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def stored_switch_offset() -> int | None:
    """The offset the virtual rocker switch was taught with, if any."""
    value = _STORE_EXTRA.get("switch_offset")
    return value if isinstance(value, int) else None


def remember_switch_offset(path: Path, offset: int, devices: dict[str, Device]) -> None:
    _STORE_EXTRA["switch_offset"] = int(offset)
    save_devices(path, devices)


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def describe_radio(radio: Radio) -> str:
    stamp = time.strftime("%H:%M:%S")
    where = f"{radio.sender_id}"
    if radio.destination and radio.destination != BROADCAST:
        where += f" -> {radio.destination.hex().upper()}"
    signal = f" {radio.dbm} dBm" if radio.dbm is not None else ""
    head = f"[{stamp}] {where}{signal}"

    if radio.rorg == RORG_VLD:
        try:
            unit, command, fields = decode_d2_41_00(radio.payload)
        except ValueError as exc:
            return f"{head}  D2 undecodable ({exc}): {radio.payload.hex(' ').upper()}"
        name = COMMAND_NAMES.get(command, f"unknown cmd {command}")
        body = "  ".join(f"{key}={format_field(key, value)}" for key, value in fields.items())
        return f"{head}  D2-41-00 unit {unit} {name}: {body}"

    if radio.rorg == RORG_UTE:
        request = parse_ute(radio.payload)
        if request is None:
            return f"{head}  UTE (malformed): {radio.payload.hex(' ').upper()}"
        kind = "response" if request.command == 1 else "query"
        return (
            f"{head}  UTE {request.request_text} {kind} EEP={request.eep_text} "
            f"manufacturer=0x{request.manufacturer:03X} channels=0x{request.channels:02X}"
        )

    if radio.rorg == RORG_RPS:
        return f"{head}  RPS {decode_rps(radio.payload, radio.status)}"

    if radio.rorg == RORG_SIGNAL and radio.payload:
        return f"{head}  Signal 0x{radio.payload[0]:02X}: {radio.payload.hex(' ').upper()}"

    return f"{head}  RORG 0x{radio.rorg:02X}: {radio.payload.hex(' ').upper()}"


# ---------------------------------------------------------------------------
# Serial ports
# ---------------------------------------------------------------------------


def candidate_ports() -> list[str]:
    """Serial ports that plausibly carry an EnOcean stick."""
    candidates: list[str] = []
    for pattern in (
        "/dev/serial/by-id/*EnOcean*",
        "/dev/cu.usbserial-*",
        "/dev/ttyUSB*",
    ):
        candidates.extend(sorted(glob.glob(pattern)))
    return list(dict.fromkeys(candidates))


def autodetect_port() -> str:
    unique = candidate_ports()
    if not unique:
        sys.exit("No EnOcean USB stick found - pass --port explicitly.")
    if len(unique) > 1:
        LOG.warning("several serial ports found (%s), using %s", ", ".join(unique), unique[0])
    return unique[0]
