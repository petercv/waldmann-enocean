"""Where the config and the pairings live.

One directory holds both, so it can be backed up or moved in a single step:

    ~/.waldmann-enocean/config.json     broker, topics, web credentials
    ~/.waldmann-enocean/devices.json    the pairings

`devices.json` is the file that matters.  Losing it loses every pairing, and
with it the Home Assistant devices, so it is deliberately kept somewhere
durable rather than in a cache directory a cleaner is entitled to empty.

Resolution order, first match wins:

1. ``--config`` / ``--store`` on the command line
2. ``$WALDMANN_ENOCEAN_HOME``
3. ``$STATE_DIRECTORY`` - set by systemd from ``StateDirectory=``, so the
   service writes to /var/lib/waldmann-enocean and never into a home directory
4. ``~/.waldmann-enocean``
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "waldmann-enocean"


def home() -> Path:
    """The directory holding this installation's config and pairings."""
    override = os.environ.get("WALDMANN_ENOCEAN_HOME")
    if override:
        return Path(override).expanduser()
    state = os.environ.get("STATE_DIRECTORY")
    if state:
        # systemd may hand over several, colon separated; the first is ours
        return Path(state.split(":")[0])
    return Path.home() / f".{APP_NAME}"


def default_config() -> Path:
    return home() / "config.json"


def default_store() -> Path:
    return home() / "devices.json"


def write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    """Replace `path` with `text` so that it is never left half written.

    Both files are rewritten while the service runs, often on an SD card, and
    a power cut in the middle of a plain write leaves a truncated file behind.
    The temporary file gets its final permissions from the start, so the
    config (which holds the MQTT password) is never readable by others.

    An existing file keeps its owner.  Resetting the service's web password
    with sudo would otherwise leave a config.json owned by root, which the
    service can no longer read.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)  # an existing tmp file keeps its old mode otherwise
        if os.geteuid() == 0 and path.exists():
            owner = path.stat()
            os.chown(tmp, owner.st_uid, owner.st_gid)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
