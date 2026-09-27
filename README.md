# Waldmann EnOcean bridge

Control Waldmann luminaires that have a TALK MODUL EnOcean wireless module from
Home Assistant, using the bidirectional EnOcean profile EEP D2-41-00 (Status
Data, Sensor Data, Maintenance Data, Light Control).

`waldmann-bridge` runs as a service with an EnOcean USB stick and connects to
Home Assistant over MQTT. It publishes MQTT Discovery configs, so after you pair
a luminaire it shows up in Home Assistant automatically, with a light for each
head plus its sensors. A small web UI takes care of pairing, configuration and
debugging.

There's also a `waldmann` command line tool built on the same code, see
[docs/cli.md](https://github.com/petercv/waldmann-enocean/blob/main/docs/cli.md).

The EnOcean part talks ESP3 to the USB stick through pyserial and implements
D2-41-00 and the UTE teach-in directly from the EEP tables. (No dependencies on
`python-enocean`, `EEP.xml` and/or BeautifulSoup.)

![EnOcean tab of the web UI](https://raw.githubusercontent.com/petercv/waldmann-enocean/main/docs/images/enocean.png)

> This is an independent project. It is not affiliated with, endorsed by or
> supported by Waldmann GmbH & Co. KG. "Waldmann" and "TALK MODUL" are
> trademarks of their owner.

## Hardware

| | |
|---|---|
| USB stick | EnOcean USB300/USB500 or similar, ESP3 at 57600 baud, 868.3 MHz in the EU |
| Luminaire | Waldmann YARA family (or KIRK ceiling sensor) with TALK MODUL EnOcean |
| Manual | Waldmann 405488810 "TALK MODUL EnOcean", chapter 7 |
| Profile | [D2-41-00](https://www.enocean-alliance.org/wp-content/uploads/2022/10/D2-41-00.pdf) |

The serial port is detected automatically (`/dev/cu.usbserial-*`,
`/dev/ttyUSB*`, `/dev/serial/by-id/*EnOcean*`). You can also set it under
**Settings** or with `--port`. If the bridge can't find or open the stick, the
Status tab says why, and the bridge keeps trying until it's there. Unplugging
the stick while it's running works the same way.

## Install

Run it as a service on a Raspberry Pi or any other Linux machine that's always
on:

```sh
sudo python3 -m venv /opt/waldmann-enocean
sudo /opt/waldmann-enocean/bin/pip install "waldmann-enocean[bridge]"
sudo /opt/waldmann-enocean/bin/waldmann-bridge --install-service
```

The last command installs a systemd service and starts it. The service runs as
its own unprivileged user, and keeps its config and pairings in
`/var/lib/waldmann-enocean`. If `python3 -m venv` fails, run
`sudo apt install python3-venv` first.

A password for the web UI is generated on the first run and written to the
log:

```sh
sudo journalctl -u waldmann-enocean | grep -A3 "password generated"
```

Open `http://<host>:8099/`, log in and enter your MQTT broker under
**Settings**. No command-line options are needed. The broker defaults to
`127.0.0.1:1883` and everything else can be set in the UI. Changes are applied
right away.

The web UI uses plain HTTP. That's fine on your own network, but put a reverse
proxy with TLS in front of it if you want to reach it from outside.

To update to a new version:

```sh
sudo /opt/waldmann-enocean/bin/pip install -U "waldmann-enocean[bridge]"
sudo systemctl restart waldmann-enocean
```

To remove the service (your config and pairings are kept):

```sh
sudo systemctl disable --now waldmann-enocean
sudo rm /etc/systemd/system/waldmann-enocean.service
```

To try it on a laptop first:

```sh
pipx install "waldmann-enocean[bridge]"
waldmann-bridge --mqtt-host 192.168.1.10
```

Requires Python 3.10 or newer. The CLI only needs `pyserial`, the `[bridge]`
extra adds `paho-mqtt`.

## Pairing

The luminaire sends a UTE teach-in telegram and the bridge answers it, as
described in chapter 7.2 of the TALK MODUL manual. (Chapter 6.1 is the teach-in
for RPS switches, which doesn't work for VLD.)

1. Click **Start pairing** on the Status tab. You can also use the *Pair new
   luminaire* button in Home Assistant, or publish to `<base>/bridge/pair/set`.

2. Open the service flap on the column (a paper clip works) and briefly press
   **key C** on the wireless module:
   * press once to send the teach-in of profile 1
   * press twice to send the teach-in of profile 2

   Use the profile that has the VLD telegram enabled in the LIGHT ADMIN app. If
   only profile 1 is set to VLD, a single short press is enough. A profile set
   to RPS sends a switch teach-in instead, which doesn't give you two-way
   control. The teach-in key on a luminaire head works the same way.

3. The bridge answers the teach-in, saves the luminaire and publishes its
   discovery configs. The device then appears in Home Assistant.

Nothing happening, or does the luminaire send data but ignore your commands?
See [troubleshooting](https://github.com/petercv/waldmann-enocean/blob/main/docs/protocol.md#troubleshooting).

## What you get in Home Assistant

Each luminaire becomes a device, with for every head:

* a **light** with on/off, brightness, color temperature and the VTL
  chronotypes as effects
* a **select** for the VTL chronotype (off, normal, owl, lark)
* a **binary sensor** for presence
* **sensors** for illuminance, temperature, humidity and the maintenance
  counters (only the ones your luminaire actually reports)
* a **Refresh** button to poll the luminaire

There's also a bridge device with a **Pair new luminaire** button.

When you set a color temperature, the bridge also turns VTL off, otherwise the
luminaire overrides it. To turn VTL back on, pick *VTL normal*, *VTL owl* or
*VTL lark* from the light's effect menu, or use the VTL select. Brightness
changes keep the head's current mode and VTL setting. See [Caveats](#caveats)
for why.

## Web UI

The web UI runs on port 8099 and needs a login. You can change the port under
**Settings** (port 80 works too), and the password there or with
`--web-password`. If you've forgotten it, set a new
one for the service like this:

```sh
sudo /opt/waldmann-enocean/bin/waldmann-bridge \
    --config /var/lib/waldmann-enocean/config.json --web-password NEW-PASSWORD
sudo systemctl restart waldmann-enocean
```

**Status** shows the USB stick, Base ID, MQTT connection and an activity log,
and has the **Start pairing** button.

![Status tab](https://raw.githubusercontent.com/petercv/waldmann-enocean/main/docs/images/status.png)

**EnOcean** (the screenshot at the top) shows the live state of every head.
Pick a luminaire and head to switch it on or off, send a request, or change
mode, brightness, color temperature or VTL one at a time. You can also send raw
D2 payloads, and the telegram monitor shows everything that's sent and
received, optionally filtered to the selected head.

**MQTT** shows the broker connection, base topic and discovery prefix, all the
topics the bridge uses, and a button to republish discovery.

![MQTT tab](https://raw.githubusercontent.com/petercv/waldmann-enocean/main/docs/images/mqtt.png)

**Settings** covers the USB stick, MQTT broker and TLS, topics, timing, the web
server and the sign-in.

![Settings tab](https://raw.githubusercontent.com/petercv/waldmann-enocean/main/docs/images/settings.png)

## Caveats

A few things on the luminaire side can make it look like a command didn't
work:

* **Color temperature only sticks with VTL off.** With VTL on, the chronotype
  sets the color temperature itself. The bridge turns VTL off when you set a
  color temperature from Home Assistant.
* **Brightness needs an illumination mode in the same telegram**, otherwise the
  luminaire ignores it. The bridge and the CLI send the head's current mode
  along, so a head in reduced light stays in reduced light and a head that's
  off turns on in working light.
* **Brightness also needs daylight control turned off** in the Waldmann User
  app. With daylight control on, the luminaire keeps regulating to its own
  light level.

More details in [docs/protocol.md](https://github.com/petercv/waldmann-enocean/blob/main/docs/protocol.md).

## Topics

```
waldmann-enocean/bridge/status          online | offline (also the LWT)
waldmann-enocean/bridge/pair/set        any payload starts pairing
waldmann-enocean/<id>/unit<N>/state     JSON state, retained
waldmann-enocean/<id>/unit<N>/set       Home Assistant JSON light command
waldmann-enocean/<id>/unit<N>/vtl/set   off | normal | owl | lark
waldmann-enocean/<id>/refresh/set       poll this luminaire now
```

You can change the base topic in the settings, and the MQTT tab shows the
actual topics for your luminaires. Home Assistant keeps the same entities when
you do, because their ids don't include the base topic.

## Where things are stored

The config and the pairings are kept together in one directory:

```
~/.waldmann-enocean/config.json     broker, topics, web login
~/.waldmann-enocean/devices.json    paired luminaires
```

Make a backup of `devices.json`. Without it you lose your pairings, and the
devices disappear from Home Assistant.

The directory is picked in this order:

1. `--config` / `--store`
2. `$WALDMANN_ENOCEAN_HOME`
3. `$STATE_DIRECTORY` (set by systemd, so the service uses
   `/var/lib/waldmann-enocean`)
4. `~/.waldmann-enocean`

The luminaire remembers the pairing by the Base ID of the USB stick. If you move
the stick to another machine, copy `devices.json` along and it keeps working, or
just pair again.

## Documentation

| | |
|---|---|
| [docs/cli.md](https://github.com/petercv/waldmann-enocean/blob/main/docs/cli.md) | the `waldmann` command line and RPS switch emulation |
| [docs/protocol.md](https://github.com/petercv/waldmann-enocean/blob/main/docs/protocol.md) | how D2-41-00 behaves on a TALK MODUL, and troubleshooting |
| [docs/internals.md](https://github.com/petercv/waldmann-enocean/blob/main/docs/internals.md) | notes on how the bridge works |

## License

MIT, see [LICENSE](https://github.com/petercv/waldmann-enocean/blob/main/LICENSE).
