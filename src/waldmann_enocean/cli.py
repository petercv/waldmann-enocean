"""The `waldmann` command line: pairing, control and diagnostics.

Everything here is a thin layer over :mod:`waldmann_enocean.protocol` - the
same code the MQTT bridge runs on, so what works here works there.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import serial

from .paths import default_store
from .protocol import (
    BROADCAST,
    COMMAND_NAMES,
    Device,
    Dongle,
    ENVIRONMENTAL_DATA,
    GET_ENVIRONMENTAL_DATA,
    GET_MAINTENANCE_DATA,
    GET_PRESENCE_DATA,
    GET_PRODUCT_STATUS,
    GET_UNIT_STATUS,
    ILLUMINATION_MODES,
    LOG,
    MAINTENANCE_DATA,
    MODE_BY_NAME,
    MODE_VALUES,
    NO_CHANGE_4,
    NO_CHANGE_CT,
    NO_CHANGE_DIM,
    PRESENCE_DATA,
    PRODUCT_STATUS,
    RORG_RPS,
    RORG_UTE,
    RORG_VLD,
    RPS_BUTTONS,
    SWITCH_ACTIONS,
    TRIGGER_STATES,
    UNIT_OPERATING_STATE,
    UNIT_STATUS,
    UTE_RESPONSE_CODES,
    VTL_BY_NAME,
    WALDMANN_MANUFACTURER,
    autodetect_port,
    build_ute_query,
    build_ute_response,
    decode_d2_41_00,
    describe_radio,
    encode_get,
    encode_set_unit_data,
    encode_trigger,
    format_field,
    load_devices,
    parse_ute,
    remember_switch_offset,
    rps_tap,
    save_devices,
    stored_switch_offset,
)
def parse_device_id(text: str) -> str:
    cleaned = text.replace(":", "").replace("-", "").strip().upper()
    if len(cleaned) != 8 or any(char not in "0123456789ABCDEF" for char in cleaned):
        raise argparse.ArgumentTypeError(f"expected 8 hex digits, got {text!r}")
    return cleaned


def resolve_target(args: argparse.Namespace, devices: dict[str, Device]) -> bytes:
    """The luminaire to talk to, and (unless --sender-offset was given) the
    address to talk from: the one it was paired with, since it ignores any
    other."""
    if args.id:
        device = devices.get(args.id)
        target = bytes.fromhex(args.id)
    else:
        paired = [d for d in devices.values() if d.eep.upper() == "D2-41-00"]
        if not paired:
            sys.exit("No paired luminaire known.  Run 'waldmann pair' first, or pass --id.")
        if len(paired) > 1:
            sys.exit(
                "Several luminaires are paired ("
                + ", ".join(sorted(d.device_id for d in paired))
                + ") - pick one with --id."
            )
        device = paired[0]
        target = device.address
    if args.sender_offset is None and device is not None:
        args.sender_offset = device.sender_offset
    return target


def sender_address(dongle: Dongle, offset: int | None) -> bytes:
    base = dongle.base_id()
    if not offset:
        return base
    if not 0 <= offset <= 127:
        sys.exit("--sender-offset must be between 0 and 127")
    return base[:3] + bytes([(base[3] + offset) & 0xFF])


def cmd_info(args: argparse.Namespace, dongle: Dongle, devices: dict[str, Device]) -> int:
    version = dongle.version()
    base = dongle.base_id()
    print(f"port          : {dongle.port}")
    print(f"module        : {version['description']}")
    print(f"app version   : {version['app_version']}   API {version['api_version']}")
    print(f"chip id       : {version['chip_id']}")
    print(f"base id       : {base.hex().upper()}  (sender address for our telegrams)")
    if devices:
        print("paired        :")
        for device in sorted(devices.values(), key=lambda d: d.device_id):
            manufacturer = (
                f"0x{device.manufacturer:03X}" if device.manufacturer is not None else "?"
            )
            print(f"  {device.device_id}  EEP {device.eep}  manufacturer {manufacturer}  paired {device.paired_at or '?'}")
    else:
        print(f"paired        : nothing yet ({default_store()})")
    return 0


def cmd_listen(args: argparse.Namespace, dongle: Dongle, devices: dict[str, Device]) -> int:
    deadline = time.monotonic() + args.seconds if args.seconds else None
    print(f"Listening on {dongle.port}.  Ctrl-C to stop.")
    try:
        while deadline is None or time.monotonic() < deadline:
            radio = dongle.read_radio(0.5)
            if radio is not None:
                print(describe_radio(radio))
    except KeyboardInterrupt:
        print()
    return 0


def cmd_pair(args: argparse.Namespace, dongle: Dongle, devices: dict[str, Device]) -> int:
    sender = sender_address(dongle, args.sender_offset)
    print(f"Our address (USB stick Base ID): {sender.hex().upper()}")
    print()
    print("Now trigger the teach-in on the luminaire (TALK MODUL manual, ch. 7.2):")
    print("  * briefly press key C on the wireless module ONCE  -> profile 1")
    print("  * briefly press key C on the wireless module TWICE -> profile 2")
    print("  Press for whichever profile holds the VLD telegram in LIGHT ADMIN;")
    print("  with only profile 1 set to VLD, one short press is all you need.")
    print("  (The teach-in key on the luminaire head works the same way.)")
    print(f"Waiting {args.seconds}s for a UTE teach-in telegram...")
    print()

    deadline = time.monotonic() + args.seconds
    rps_seen = False
    paired_any = False
    try:
        while time.monotonic() < deadline:
            radio = dongle.read_radio(0.5)
            if radio is None:
                continue
            received_at = time.monotonic()
            print(describe_radio(radio))

            if radio.rorg == RORG_RPS and not rps_seen:
                rps_seen = True
                print(
                    "  -> that is the RPS switch profile, not the bidirectional VLD one.\n"
                    "     Press key C the other number of times to send the D2-41-00 teach-in."
                )
                continue

            if radio.rorg != RORG_UTE:
                continue
            request = parse_ute(radio.payload)
            if request is None or request.command != 0:
                continue

            device_id = radio.sender_id
            if request.eep != (0xD2, 0x41, 0x00) and not args.accept_any:
                print(
                    f"  -> ignoring EEP {request.eep_text} (only D2-41-00 is handled; "
                    "use --accept-any to override)"
                )
                continue

            # The response has to be on the air within 500 ms, so send it before
            # touching the device store.
            teach_out = request.request_type == 1
            code, verb = (2, "teach-out") if teach_out else (1, "teach-in")
            if request.response_expected:
                dongle.send_radio(
                    RORG_UTE,
                    build_ute_response(request, code),
                    sender=sender,
                    destination=radio.sender,
                    status=args.status_byte,
                )
                print(
                    f"  -> {verb} accepted, response sent to {device_id} "
                    f"({(time.monotonic() - received_at) * 1000:.0f} ms)"
                )
            else:
                print(f"  -> {verb} recorded ({device_id} asked for no response)")

            if teach_out:
                devices.pop(device_id, None)
            else:
                devices[device_id] = Device(
                    device_id=device_id,
                    eep=request.eep_text,
                    manufacturer=request.manufacturer,
                    paired_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    sender_offset=args.sender_offset or 0,
                    notes={"channels": request.channels},
                )
            save_devices(args.store, devices)

            if request.manufacturer != WALDMANN_MANUFACTURER:
                print(
                    f"  note: manufacturer is 0x{request.manufacturer:03X}, "
                    f"Waldmann is 0x{WALDMANN_MANUFACTURER:03X}"
                )
            print(f"  stored in {args.store}")
            paired_any = True
            if not args.keep_going:
                # Stay a moment longer: the module often follows up with a
                # Signal telegram carrying its product id.
                grace = time.monotonic() + 3
                while time.monotonic() < grace:
                    extra = dongle.read_radio(0.5)
                    if extra is not None:
                        print(describe_radio(extra))
                break
    except KeyboardInterrupt:
        print()

    if not paired_any:
        print()
        print("No D2-41-00 teach-in received.  Things to check:")
        print("  * is the luminaire powered and the wireless module seated?")
        print("  * try the other profile (one vs. two short presses of key C)")
        print("  * the VLD telegram has to be enabled in the LIGHT ADMIN app")
        print("  * the luminaire holds at most 10 transmitters - clear the list by")
        print("    holding key C for 10 s if it is full (LED flashes 10 times)")
        return 1
    print()
    print("Paired.  Check it with:  waldmann status")
    return 0


def cmd_teach(args: argparse.Namespace, dongle: Dongle, devices: dict[str, Device]) -> int:
    """Teach *us* into the luminaire's device list (the opposite of `pair`).

    `pair` handles ch. 7.2, where the module teaches itself into us so we can
    receive its data.  This is the other direction: with the luminaire put into
    teach-in mode by hand, we send it a D2-41-00 UTE teach-in query so that our
    address lands in its device list and it accepts commands from us.
    """
    sender = sender_address(dongle, args.sender_offset)
    destination = BROADCAST if args.broadcast else resolve_target(args, devices)
    payload = build_ute_query(args.manufacturer, args.channels)

    print(f"Our address: {sender.hex().upper()}   target: {destination.hex().upper()}")
    print(f"UTE teach-in query: {payload.hex(' ').upper()}  (EEP D2-41-00, "
          f"manufacturer 0x{args.manufacturer:03X}, channels 0x{args.channels:02X})")
    print()
    print("Put the luminaire into teach-in mode (manual ch. 6.1):")
    print("  * press and HOLD key C on the wireless module for 5 seconds")
    print("    -> status LED B flashes green, teach-in mode lasts 60 s")
    print("  (holding for 10 s instead wipes the whole device list - don't.)")
    print(f"Sending a teach-in query every {args.interval:g}s for {args.seconds:g}s...")
    print()

    deadline = time.monotonic() + args.seconds
    next_send = 0.0
    try:
        while time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                dongle.send_radio(
                    RORG_UTE,
                    payload,
                    sender=sender,
                    destination=destination,
                    status=args.status_byte,
                )
                print(f"[{time.strftime('%H:%M:%S')}] teach-in query sent")
                next_send = time.monotonic() + args.interval
            radio = dongle.read_radio(0.5)
            if radio is None:
                continue
            if radio.rorg != RORG_UTE:
                LOG.debug("while teaching: %s", describe_radio(radio))
                continue
            response = parse_ute(radio.payload)
            if response is None or response.command != 1:
                continue
            code = response.request_type  # in a response these bits are the result
            print()
            print(f"UTE response from {radio.sender_id}: "
                  f"{UTE_RESPONSE_CODES.get(code, f'unknown({code})')} "
                  f"(EEP {response.eep_text})")
            if code == 1:
                device = devices.setdefault(
                    radio.sender_id,
                    Device(device_id=radio.sender_id, eep=response.eep_text),
                )
                device.sender_offset = args.sender_offset or 0
                device.notes["taught_us_at"] = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
                save_devices(args.store, devices)
                print("Now try:  waldmann maintenance --wait 10")
                return 0
            return 1
    except KeyboardInterrupt:
        print()

    print()
    print("No UTE response.  The luminaire either was not in teach-in mode, or it")
    print("does not accept being taught this way.  Try --broadcast, or a different")
    print("--manufacturer (0x02E is Waldmann's own id).")
    return 1


def cmd_switch(args: argparse.Namespace, dongle: Dongle, devices: dict[str, Device]) -> int:
    """Act as an EnOcean rocker switch (RPS F6-02-01).

    A real PTM switch broadcasts, so this broadcasts by default; pass --id to
    address one luminaire.  The switch identity defaults to Base ID + 1 so it
    stays distinct from the address the D2-41-00 teach-in used.
    """
    # Its own identity, never a pairing's: the one it was taught with, or
    # Base ID + 1 so it stays distinct from a D2-41-00 pairing at + 0.
    offset = args.sender_offset
    if offset is None:
        offset = stored_switch_offset()
    if offset is None:
        offset = 1
    sender = sender_address(dongle, offset)
    destination = bytes.fromhex(args.id) if args.id else BROADCAST
    print(f"Virtual switch address: {sender.hex().upper()} -> {destination.hex().upper()}")

    if args.action == "teach":
        print()
        print("Put the luminaire into teach-in mode (manual ch. 6.1):")
        print("  * press and HOLD key C on the wireless module for 5 seconds")
        print("    -> status LED B flashes green")
        print("  (or hold the teach-in key on one luminaire head for 5 s to teach")
        print("   that head only.  Holding key C for 10 s wipes the device list.)")
        print()
        input("Press Return once the LED is flashing green... ")
        print("Sending 3 button presses within two seconds (manual ch. 6.1)...")
        for index in range(3):
            rps_tap(dongle, sender, destination, args.button, 0.1)
            print(f"  press {index + 1}/3 ({args.button})")
            time.sleep(0.45)
        print()
        print("The LED should have gone out - the switch is taught.")
        print("It will NOT appear in LIGHT ADMIN's list of switches: that list is")
        print("for Bluetooth easy-fit switches, not EnOcean transmitters.  The")
        print("confirmation is that the luminaire reacts:  waldmann switch on")
        print("Running 'switch teach' again with this address unteaches it.")
        remember_switch_offset(args.store, offset, devices)
        return 0

    button, default_hold = SWITCH_ACTIONS.get(args.action, (args.action, 0.15))
    hold = default_hold if args.hold is None else args.hold
    for index in range(args.repeat):
        rps_tap(dongle, sender, destination, button, hold, args.pulse)
        pulsing = f", resent every {args.pulse:g}s" if args.pulse else ""
        print(
            f"{args.action}: button {button} held {hold:g}s{pulsing} "
            f"({index + 1}/{args.repeat})"
        )
        time.sleep(0.25)

    if args.watch:
        # Unit Status is sent on a change of illumination mode, so pressing a
        # button and then listening shows what the luminaire actually did -
        # per unit, which is how you tell the two heads apart.
        print(f"\nWatching the luminaire's telegrams for {args.watch:g}s:")
        deadline = time.monotonic() + args.watch
        seen = False
        while time.monotonic() < deadline:
            radio = dongle.read_radio(deadline - time.monotonic())
            if radio is None:
                break
            if radio.rorg in (RORG_VLD, RORG_RPS) and radio.sender != sender:
                print(" ", describe_radio(radio))
                seen = True
        if not seen:
            print("  (nothing - the luminaire reports on its own schedule, try longer)")
    return 0


def _request(
    dongle: Dongle,
    args: argparse.Namespace,
    target: bytes,
    payload: bytes,
    expect: int | None,
    wait: float,
) -> list[tuple[int, int, dict[str, object]]]:
    """Send one telegram and collect the replies to *it*.

    The luminaire also broadcasts Unit Status, Presence and Environmental data
    on its own schedule.  Those are not answers to anything we sent, so they
    must not be reported as such: a real response is addressed to us, whereas
    cyclic data goes out as a broadcast.  Anything unsolicited is logged as
    such and kept out of the result.
    """
    sender = sender_address(dongle, args.sender_offset)
    LOG.debug("TX D2-41-00 %s -> %s", payload.hex().upper(), target.hex().upper())
    dongle.send_radio(
        RORG_VLD, payload, sender=sender, destination=target, status=args.status_byte
    )

    results: list[tuple[int, int, dict[str, object]]] = []
    unsolicited = 0
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        radio = dongle.read_radio(deadline - time.monotonic())
        if radio is None:
            break
        if radio.rorg != RORG_VLD or radio.sender != target:
            LOG.debug("ignoring %s", describe_radio(radio))
            continue
        try:
            unit, command, fields = decode_d2_41_00(radio.payload)
        except ValueError as exc:
            LOG.warning("undecodable telegram %s: %s", radio.payload.hex().upper(), exc)
            continue
        addressed_to_us = radio.destination == sender
        if not addressed_to_us and command != expect:
            unsolicited += 1
            LOG.debug("unsolicited broadcast: %s", describe_radio(radio))
            continue
        results.append((unit, command, fields))
        if expect is not None and command == expect:
            break
    if unsolicited:
        LOG.info(
            "(ignored %d unsolicited broadcast%s while waiting)",
            unsolicited,
            "" if unsolicited == 1 else "s",
        )
    return results


def _print_results(
    title: str, results: list[tuple[int, int, dict[str, object]]], as_json: bool
) -> int:
    if as_json:
        print(
            json.dumps(
                [
                    {"unit": unit, "command": COMMAND_NAMES.get(command, command), **fields}
                    for unit, command, fields in results
                ],
                indent=2,
            )
        )
    else:
        if not results:
            print(f"{title}: no reply")
        for unit, command, fields in results:
            print(f"{COMMAND_NAMES.get(command, command)} (unit {unit})")
            for key, value in fields.items():
                print(f"  {key:24}{format_field(key, value)}")
    return 0 if results else 1


def cmd_read(args: argparse.Namespace, dongle: Dongle, devices: dict[str, Device]) -> int:
    target = resolve_target(args, devices)
    reads = {
        "units": (GET_PRODUCT_STATUS, PRODUCT_STATUS),
        "status": (GET_UNIT_STATUS, UNIT_STATUS),
        "presence": (GET_PRESENCE_DATA, PRESENCE_DATA),
        "env": (GET_ENVIRONMENTAL_DATA, ENVIRONMENTAL_DATA),
        "maintenance": (GET_MAINTENANCE_DATA, MAINTENANCE_DATA),
    }
    wanted = list(reads) if args.command == "sensors" else [args.command]
    results: list[tuple[int, int, dict[str, object]]] = []
    for name in wanted:
        get_id, expect = reads[name]
        unit = 0 if get_id == GET_PRODUCT_STATUS else args.unit
        results.extend(_request(dongle, args, target, encode_get(unit, get_id), expect, args.wait))
    return _print_results(args.command, results, args.json)


def read_illumination_mode(
    args: argparse.Namespace, dongle: Dongle, target: bytes
) -> int | None:
    """The head's current illumination mode, or None if it does not answer."""
    replies = _request(
        dongle, args, target, encode_get(args.unit, GET_UNIT_STATUS), UNIT_STATUS, 3.0
    )
    for _, _, fields in replies:
        name = fields.get("illumination_mode")
        if isinstance(name, str):
            return MODE_VALUES.get(name)
    return None


def cmd_set(args: argparse.Namespace, dongle: Dongle, devices: dict[str, Device]) -> int:
    target = resolve_target(args, devices)
    mode = NO_CHANGE_4
    dim_raw = NO_CHANGE_DIM
    color_temp = NO_CHANGE_CT

    if args.command == "off":
        mode = MODE_BY_NAME["off"]
    elif args.command == "on":
        mode = MODE_BY_NAME[args.mode]
    elif args.command == "mode":
        mode = MODE_BY_NAME[args.value]
    elif args.command == "dim":
        percent = max(0.0, min(100.0, float(args.value)))
        dim_raw = round(percent * 2)
        if args.mode is not None:
            mode = MODE_BY_NAME[args.mode]
    elif args.command == "ct":
        color_temp = max(0, min(16000, int(args.value)))

    if args.command in {"on", "off", "mode", "ct"} and args.level is not None:
        dim_raw = round(max(0.0, min(100.0, args.level)) * 2)
    if args.command in {"on", "mode", "dim"} and args.ct is not None:
        color_temp = max(0, min(16000, args.ct))

    # A dimming level only takes effect when the same telegram also names an
    # illumination mode.  With mode = "no change" the luminaire acknowledges the
    # telegram and then keeps its old level - measured on a TALK MODUL, both
    # with the Waldmann app running and closed.  Color temperature has no such
    # requirement, it applies on its own.  So carry the head's current mode
    # over, and light a dark head rather than silently doing nothing.
    if dim_raw != NO_CHANGE_DIM and mode == NO_CHANGE_4:
        current = read_illumination_mode(args, dongle, target)
        mode = MODE_BY_NAME["working"] if current in (None, 0) else current

    payload = encode_set_unit_data(
        unit=args.unit,
        mode=mode,
        # VTL drives color temperature on its own; it has to be off before an
        # explicit --ct sticks.
        vtl=NO_CHANGE_4 if args.vtl is None else VTL_BY_NAME[args.vtl],
        switch={"switch": 1, "store": 0}[args.switch],
        fading={"direct": 0, "runtime": 1, "default": NO_CHANGE_4}[args.fade],
        dim_raw=dim_raw,
        color_temp=color_temp,
    )
    results = _request(dongle, args, target, payload, UNIT_STATUS, args.wait)
    print(
        f"Set Unit Data -> {target.hex().upper()} unit {args.unit}: "
        f"mode={ILLUMINATION_MODES.get(mode, 'no change')} "
        f"dim={'no change' if dim_raw == NO_CHANGE_DIM else f'{dim_raw / 2:g}%'} "
        f"ct={'no change' if color_temp == NO_CHANGE_CT else f'{color_temp} K'}"
    )
    return _print_results(args.command, results, args.json)


def cmd_trigger(args: argparse.Namespace, dongle: Dongle, devices: dict[str, Device]) -> int:
    target = resolve_target(args, devices)
    payload = encode_trigger(args.unit, TRIGGER_STATES[args.value])
    results = _request(dongle, args, target, payload, UNIT_OPERATING_STATE, args.wait)
    return _print_results(args.command, results, args.json)


def cmd_raw(args: argparse.Namespace, dongle: Dongle, devices: dict[str, Device]) -> int:
    target = resolve_target(args, devices)
    payload = bytes.fromhex(args.value.replace(" ", ""))
    results = _request(dongle, args, target, payload, None, args.wait)
    return _print_results(args.command, results, args.json)


def cmd_devices(args: argparse.Namespace, dongle: Dongle, devices: dict[str, Device]) -> int:
    if not devices:
        print(f"No devices in {args.store}")
        return 1
    for device in sorted(devices.values(), key=lambda d: d.device_id):
        print(f"{device.device_id}  {device.eep}  paired {device.paired_at or '?'}")
    return 0


def cmd_forget(args: argparse.Namespace, dongle: Dongle, devices: dict[str, Device]) -> int:
    if devices.pop(args.value.upper(), None) is None:
        print(f"{args.value.upper()} was not stored")
        return 1
    save_devices(args.store, devices)
    print(f"Forgot {args.value.upper()}")
    return 0


# ---------------------------------------------------------------------------


DEFAULTS: dict[str, object] = {
    "port": None,
    "baudrate": 57600,
    "store": None,
    "id": None,
    "unit": 0,
    "sender_offset": None,  # None = not given; commands pick their own default
    "wait": 3.0,
    "status_byte": 0x00,
    "json": False,
    "verbose": False,
    "dump_esp3": False,
    # only meaningful for the switching commands
    "mode": "working",
    "level": None,
    "ct": None,
    "vtl": None,
    "switch": "switch",
    "fade": "default",
}


def build_parser() -> argparse.ArgumentParser:
    # Shared options are attached to every subparser as well, so they can be
    # given before or after the subcommand.  SUPPRESS keeps the subparser from
    # overwriting a value that was already given before the subcommand;
    # anything left unset is filled in from DEFAULTS after parsing.
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--port", help="serial port of the USB stick (default: autodetect)")
    common.add_argument("--baudrate", type=int, help="default: 57600")
    common.add_argument("--store", type=Path,
                        help=f"paired-device file (default: {default_store()})")
    common.add_argument("--id", type=parse_device_id, help="target luminaire, e.g. 01A2B3C4")
    common.add_argument("--unit", type=int, help="luminaire head / unit index (0-14, default: 0)")
    common.add_argument(
        "--sender-offset", type=int,
        help="use Base ID + offset (0-127) as our address (default: the one the "
        "luminaire was paired with)"
    )
    common.add_argument("--wait", type=float, help="seconds to wait for replies (default: 3)")
    common.add_argument(
        "--status-byte",
        type=lambda value: int(value, 0),
        help="ERP1 status byte we transmit (default 0x00; the luminaire uses 0x80)",
    )
    common.add_argument("--json", action="store_true", help="print replies as JSON")
    common.add_argument("--verbose", action="store_true")
    common.add_argument("--dump-esp3", action="store_true", help="log raw ESP3 frames")

    parser = argparse.ArgumentParser(
        description="Pair with and control Waldmann luminaires over EnOcean (EEP D2-41-00).",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return subparsers.add_parser(name, help=help_text, parents=[common])

    add("info", "show USB stick details and paired devices")

    listen = add("listen", "decode incoming telegrams")
    listen.add_argument("--seconds", type=float, default=0, help="stop after N seconds")

    pair = add("pair", "answer the luminaire's UTE teach-in")
    pair.add_argument("--seconds", type=float, default=120)
    pair.add_argument("--accept-any", action="store_true", help="accept EEPs other than D2-41-00")
    pair.add_argument("--keep-going", action="store_true", help="keep pairing more devices")

    teach = add("teach", "teach US into the luminaire, so it accepts our commands")
    teach.add_argument("--seconds", type=float, default=45)
    teach.add_argument("--interval", type=float, default=3.0, help="resend period")
    teach.add_argument("--broadcast", action="store_true", help="broadcast instead of unicast")
    teach.add_argument(
        "--manufacturer",
        type=lambda value: int(value, 0),
        default=0x7FF,
        help="manufacturer id to claim (default 0x7FF; Waldmann is 0x02E)",
    )
    teach.add_argument(
        "--channels",
        type=lambda value: int(value, 0),
        default=0xFF,
        help="channel count to request (default 0xFF = all)",
    )

    add("status", "read Unit Status (mode, dim level, color temperature)")
    add("units", "read Product Status (which luminaire heads exist)")
    add("presence", "read Presence Data")
    add("env", "read Environmental Data (noise, VOC, lux, temperature, humidity)")
    add("maintenance", "read Maintenance Data (hours, power, energy)")
    add("sensors", "read everything the profile offers")

    def add_light_options(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--level", type=float, help="dimming level in percent")
        sub.add_argument("--ct", type=int, help="color temperature in K")
        sub.add_argument(
            "--vtl",
            choices=list(VTL_BY_NAME),
            help="biodynamic chronotype; set 'off' for a fixed --ct to hold",
        )
        sub.add_argument(
            "--switch",
            choices=("switch", "store"),
            default="switch",
            help="apply the settings now (default) or only store them for the mode",
        )
        sub.add_argument(
            "--fade",
            choices=("default", "direct", "runtime"),
            default="default",
            help="fading mode",
        )

    on = add("on", "switch the luminaire on")
    on.add_argument(
        "--mode", choices=list(MODE_BY_NAME), default="working", help="illumination mode"
    )
    add_light_options(on)

    off = add("off", "switch the luminaire off")
    add_light_options(off)

    dim = add("dim", "set the dimming level in percent")
    dim.add_argument("value", type=float)
    dim.add_argument(
        "--mode",
        choices=list(MODE_BY_NAME),
        help="illumination mode to send along (default: keep the head's own, "
        "switching a dark head to working light)",
    )
    add_light_options(dim)

    color = add("ct", "set the color temperature in K")
    color.add_argument("value", type=int)
    add_light_options(color)

    mode = add("mode", "set the illumination mode")
    mode.add_argument("value", choices=list(MODE_BY_NAME))
    add_light_options(mode)

    switch = add("switch", "act as an EnOcean rocker switch (RPS F6-02-01)")
    switch.add_argument(
        "action", choices=["teach", *SWITCH_ACTIONS, *RPS_BUTTONS], metavar="action"
    )
    switch.add_argument(
        "--button", choices=list(RPS_BUTTONS), default="AI", help="button to teach with"
    )
    switch.add_argument("--hold", type=float, help="seconds to hold the button down")
    switch.add_argument(
        "--pulse",
        type=float,
        default=0.0,
        help="resend the press telegram every N seconds while held (0 = once, like a real rocker)",
    )
    # NB: do NOT use switch.set_defaults(sender_offset=...) here.  parents=[]
    # shares action objects between parsers, and set_defaults() mutates
    # action.default in place, which would change the default for every
    # subcommand.  cmd_switch picks its own default instead.
    switch.add_argument("--repeat", type=int, default=1)
    switch.add_argument(
        "--watch",
        type=float,
        default=0.0,
        help="after pressing, decode the luminaire's telegrams for N seconds",
    )
    trigger = add("trigger", "set/clear an illumination-mode trigger")
    trigger.add_argument("value", choices=list(TRIGGER_STATES))

    raw = add("raw", "send a raw D2 payload (hex)")
    raw.add_argument("value")

    add("devices", "list paired devices")

    forget = add("forget", "remove a device from the store")
    forget.add_argument("value")

    return parser


HANDLERS = {
    "info": cmd_info,
    "listen": cmd_listen,
    "pair": cmd_pair,
    "teach": cmd_teach,
    "status": cmd_read,
    "units": cmd_read,
    "presence": cmd_read,
    "env": cmd_read,
    "maintenance": cmd_read,
    "sensors": cmd_read,
    "on": cmd_set,
    "off": cmd_set,
    "dim": cmd_set,
    "ct": cmd_set,
    "mode": cmd_set,
    "switch": cmd_switch,
    "trigger": cmd_trigger,
    "raw": cmd_raw,
    "devices": cmd_devices,
    "forget": cmd_forget,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for name, default in DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if args.store is None:
        args.store = default_store()
    devices = load_devices(args.store)
    handler = HANDLERS[args.command]

    if args.command in {"devices", "forget"}:
        return handler(args, None, devices)  # type: ignore[arg-type]

    port = args.port or autodetect_port()
    try:
        dongle = Dongle(port, args.baudrate, dump=args.dump_esp3)
    except serial.SerialException as exc:
        sys.exit(f"Cannot open {port}: {exc}")
    try:
        return handler(args, dongle, devices)
    finally:
        dongle.close()


if __name__ == "__main__":  # python -m waldmann_enocean.cli
    raise SystemExit(main())
