# D2-41-00 on a TALK MODUL

Notes on how the D2-41-00 profile behaves on the TALK MODUL EnOcean, including
a few things you won't find in the EEP spec or the Waldmann manual.

## Pairing and the sender address

The UTE teach-in from chapter 7.2 (`waldmann pair`, or **Start pairing** in the
web UI) is all you need. After that the luminaire answers every command, and
sends its replies to the address you paired from.

If the luminaire keeps broadcasting data but ignores your commands, the problem
is most likely the sender address, not the profile, the firmware or the
hardware. The module keeps some state per sender address that you can't read
out, and once an address is in a bad state it stays that way:

* Pairing the same address again doesn't fix it, even though the module
  replies "teach-in successful" (UTE response code 1) every time.
* Pairing from a different address (for example `--sender-offset 8`) works
  straight away.
* Wiping the device list (hold key C for 10 seconds) fixes the original
  address. After that a single `pair` is enough again.

This happened with Base ID + 0, which had been used by an older project on the
same luminaire. So a successful teach-in response doesn't tell you much. The
real test is whether a command gets answered, see
[Verifying](cli.md#verifying).

It's a good idea to use a separate `--sender-offset` for each purpose instead
of the plain Base ID. Offset 0 isn't special to the luminaire (it only sees a
4-byte id), but the Base ID block has 128 addresses for exactly this, and it
means you have a clean address to fall back on if one goes bad.

The offset a luminaire was paired from is saved with it in `devices.json`, and
both the CLI and the bridge use it automatically when they talk to that
luminaire. `--sender-offset` (or the setting in the web UI) only picks the
address for new pairings. The virtual rocker switch keeps its own offset, see
[cli.md](cli.md#virtual-rocker-switch-rps).

The status LED on the module responds when you release the button, not while
you hold it. Hold for 5 seconds and let go for the green teach-in flash, or
hold for 10 to 15 seconds and let go for the ten flashes that confirm a wipe.

## What you can control

`Set Unit Data` handles the illumination mode, dimming level and color
temperature:

```sh
waldmann --unit 1 on --vtl off --level 60 --ct 4000
```

There are a few catches.

**Color temperature needs VTL off.** With the biodynamic chronotype (VTL) on,
the luminaire picks its own color temperature. A 3500 K request with
`--vtl off` stays at 3500 K, but a 3000 K request with `--vtl normal` ends up
around 4300 K.

**Dimming needs an illumination mode in the same telegram.** If the mode field
is left at 15 ("no change"), the luminaire replies with a Unit Status but keeps
its old level. With an actual mode in the telegram the level is applied. So
`mode working --level 30` works, but a plain `dim 30` sent as "no change" does
nothing. Color temperature doesn't have this problem, `ct 3000` works fine with
the mode left at "no change".

Because of this, `waldmann dim` first reads the head's current mode and sends
it along, so a head in reduced light stays in reduced light. If the head is
off, it switches to working light. Use `dim 40 --mode off` if you only want to
store a level without turning the light on. The bridge does the same for Home
Assistant and the web UI.

**Dimming also needs daylight control turned off** in the Waldmann User app.
With daylight control on, the luminaire regulates to its own light level and
ignores the level you send. D2-41-00 has no field for daylight control, so it
can only be changed in the app.

Right after you push a configuration from the app, the luminaire can take a
little while to settle. During that time commands may be applied and then
undone about 20 seconds later, with VTL going back to `normal`. It stops once
the luminaire has settled.

The fading mode field (`--fade direct|runtime`) doesn't seem to do anything on
this module. It reports the field as "not supported", and the reported level
jumps straight to the new value whichever mode you pick.

## Telegram layout

The first byte of every D2-41-00 payload holds the unit index in the high
nibble and the command id in the low nibble. The decoder checks every received
telegram against the payload length the EEP defines for that command, and logs
a warning if they don't match.

A luminaire with two heads sends telegrams for unit 0 and unit 1 with their own
values. `Get Product Status` returns the list of active units, `[0, 1]` for two
heads. If it doesn't answer, check the sender address as described above.

## Troubleshooting

### The luminaire sends data but ignores commands

`pair` should be enough. If commands are still ignored, try pairing from a
different `--sender-offset`. If that works, the original address is in a bad
state and needs a device-list wipe, see
[Pairing and the sender address](#pairing-and-the-sender-address).

There's also a `teach` command that sends a UTE teach-in query the other way
around, with the luminaire in teach-in mode (hold key C for 5 seconds). You
shouldn't need it. Chapter 6.1 of the manual warns that teaching an
already-paired transmitter again removes it, so stick with `pair` unless you're
experimenting.

### No teach-in arrives

* The VLD telegram has to be enabled in the LIGHT ADMIN app (Bluetooth).
* The luminaire remembers up to 10 transmitters. Clear the list by holding
  key C for 10 seconds (the LED flashes 10 times) and pair again.
* Check that the antenna wire is connected to the module (the grey wire, or
  the black one without a marking if there are two).
