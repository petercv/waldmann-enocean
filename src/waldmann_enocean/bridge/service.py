"""Install the bridge as a systemd service: ``sudo waldmann-bridge --install-service``.

pip only installs into its own environment, so the unit file ships with the
package and this writes it to /etc/systemd/system, pointing ExecStart at the
installation it was run from.  Running it again after an upgrade is harmless.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ..paths import write_atomic

NAME = "waldmann-enocean"
UNIT_PATH = Path("/etc/systemd/system") / f"{NAME}.service"

UNIT = """\
[Unit]
Description=Waldmann EnOcean to MQTT bridge
Documentation=https://github.com/petercv/waldmann-enocean
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=always
RestartSec=5

# systemd creates an unprivileged user for the service, so there is no account
# to set up.  The serial device is owned by group dialout on Debian and
# Raspberry Pi OS.
DynamicUser=yes
SupplementaryGroups=dialout
AmbientCapabilities=CAP_NET_BIND_SERVICE

# Creates /var/lib/{name} and passes it as $STATE_DIRECTORY, which is where
# the bridge keeps config.json and devices.json.
StateDirectory={name}

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryDenyWriteExecute=true

[Install]
WantedBy=multi-user.target
"""


def exec_start() -> str:
    """The command that starts this installation of the bridge."""
    # sys.executable is the environment's own python (a venv's bin/python is
    # deliberately not resolved), and the entry point script sits next to it.
    script = Path(sys.executable).parent / "waldmann-bridge"
    argv = [str(script)] if script.exists() else [
        sys.executable, "-m", "waldmann_enocean.bridge.main"]
    return " ".join(f'"{arg}"' if " " in arg else arg for arg in argv)


def unit_text() -> str:
    return UNIT.format(exec_start=exec_start(), name=NAME)


def install() -> int:
    if not Path("/run/systemd/system").is_dir():
        print("This system doesn't run systemd, so there is no service to "
              "install. Start waldmann-bridge some other way.", file=sys.stderr)
        return 1
    if os.geteuid() != 0:
        print(f"Installing the service needs root:\n\n"
              f"    sudo {exec_start()} --install-service", file=sys.stderr)
        return 1
    if Path(sys.executable).is_relative_to("/home") or Path(sys.executable).is_relative_to("/root"):
        print("The service can't see files in home directories (ProtectHome), "
              "so install the package outside them, for example in "
              f"/opt/{NAME}. See the README.", file=sys.stderr)
        return 1

    write_atomic(UNIT_PATH, unit_text())
    print(f"Wrote {UNIT_PATH}")
    for command in (["systemctl", "daemon-reload"],
                    ["systemctl", "enable", f"{NAME}.service"],
                    ["systemctl", "restart", f"{NAME}.service"]):
        print("$", " ".join(command))
        subprocess.run(command, check=True)

    print(f"\nThe bridge is running. On its first run it writes a password for "
          f"the web UI to the log:\n\n"
          f"    sudo journalctl -u {NAME} | grep -A3 'password generated'\n\n"
          f"Then open http://<this host>:8099/ and enter your MQTT broker under "
          f"Settings.")
    return 0
