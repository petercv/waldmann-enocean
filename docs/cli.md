# The `waldmann` command line

The bridge is what most people want, see the [README](../README.md). The
`waldmann` command uses the same protocol code and is handy for pairing,
quick tests, reading sensors and using the USB stick as a virtual rocker
switch.

Only one program can use the serial port at a time, so stop the bridge first:

```sh
sudo systemctl stop waldmann-enocean
```

## Usage

```sh
waldmann info                  # stick version, Base ID, paired devices
waldmann listen                # decode every telegram on the air
waldmann pair                  # answer the luminaire's teach-in
waldmann units                 # which heads the luminaire has

waldmann on                    # working light
waldmann on --level 60 --ct 4000
waldmann off
waldmann dim 25
waldmann ct 5000
waldmann mode reduced          # off | reduced | working | service

waldmann status                # mode, dimming level, color temperature
waldmann presence              # presence and occupancy
waldmann env                   # noise, VOC, light level, temperature, humidity
waldmann maintenance           # operating hours, power, energy
waldmann sensors               # all of the above, add --json for JSON output
```

A luminaire with more than one head has a unit index per head. Pick one with
`--unit N`, and use `units` to see which ones exist. If you've paired more than
one luminaire, select one with `--id 01A2B3C4`.

Commands are applied right away. With `--switch store` the dimming and color
settings are stored for the given mode without switching to it.

## Verifying

`listen` shows everything on the air. The luminaire broadcasts Unit Status,
Presence Data and Environmental Data by itself (every 300 s by default, you can
change that in LIGHT ADMIN), so you'll see data even before pairing.

To check that the luminaire actually accepts your commands, request
maintenance data:

```sh
waldmann maintenance --wait 15
```

The luminaire never sends maintenance data on its own (manual chapter 5.8), so
if you get a reply, it's an answer to your request. If `listen` shows
broadcasts but this gets no reply, the pairing is missing or was removed. Pair
again, and see [troubleshooting](protocol.md#troubleshooting) if that doesn't
help.

## Multiple heads

Pairing is done per wireless module, not per head. All heads share the same
EnOcean id, and each telegram carries the unit index of the head it's for, so
one teach-in covers all of them. Which key you press decides what gets paired
(manual chapter 6.1):

* **key C on the wireless module** pairs the whole luminaire
* the teach-in key on a **luminaire head** pairs only that head

Use key C unless you only want to pair one head. `pair` prints the channel
field of the teach-in, which should be `0xFF` (all channels).

The address that gets paired is the Base ID of the USB stick, plus
`--sender-offset` if you give one. It's saved with the luminaire, so later
commands use the right address without you having to pass it again.

## Virtual rocker switch (RPS)

Besides D2-41-00, the wireless module also accepts RPS telegrams (`F6-02-01` /
`F6-03-01`) from a paired switch, see manual chapters 5.5 and 6.1 to 6.3. With
`switch` the USB stick acts as such a switch. It uses Base ID + 1 so it doesn't
get mixed up with the D2-41-00 address, or the offset you taught it with if you
passed `--sender-offset` to `switch teach`.

1. Hold key C for 5 seconds (LED B flashes green), then run:

   ```sh
   waldmann switch teach
   ```

   It waits for you to press Return, and then sends the three button presses
   within two seconds that chapter 6.1 asks for.

   The stick won't show up under **LIGHT ADMIN > Switches > list of switches**.
   That list is for Bluetooth easy-fit switches (6-byte MACs), not EnOcean
   transmitters (4-byte ids). EnOcean transmitters are stored in the module's
   own device list, which the app doesn't show. The only way to tell it worked
   is that the luminaire reacts.

2. Control it:

   ```sh
   waldmann switch on
   waldmann switch off
   waldmann switch brighter --hold 2
   waldmann switch darker --hold 2
   waldmann switch service
   ```

According to chapter 6.2 the bottom half of the rocker switches on and dims up,
and the top half switches off and dims down. If on and off are the wrong way
around, use the raw button names instead (`switch A0`, `switch AI`,
`switch BI`, `switch B0`).

Running `switch teach` again for a switch that's already paired removes it
again, because the procedure in chapter 6.1 is a toggle.

Dimming works by holding a button. A real rocker sends one telegram when you
press it and one when you let go, which is what `--hold` does. If the luminaire
treats the button as released as soon as telegrams stop coming in, use
`--pulse 0.3` to keep repeating the press. If you hold it too long, the level
goes all the way down and the luminaire switches off.

### One head at a time

Where you pair the switch decides what it controls (chapter 6.1): paired with
key C it controls the whole luminaire, paired with a head's teach-in key only
that head. The stick can act as several switches by using a different
`--sender-offset` for each, so you can pair one per head:

```sh
waldmann --sender-offset 2 switch teach   # hold head 1's teach-in key for 5 s
waldmann --sender-offset 3 switch teach   # hold head 2's teach-in key for 5 s
waldmann --sender-offset 2 switch on      # head 1 only
```

### Brightness other than full

A plain rocker "on" switches to full brightness. You can dim afterwards by
holding a button (`switch darker --hold 2`, which is time based and so not very
precise), or set **Switches mode** to `Advanced` in LIGHT ADMIN, configure
light scenes there and send them to the luminaire. Each half of the rocker then
triggers its own scene. `Office System` mode does the same with Waldmann's
Well-Being, Creativity and Focus scenes.

You can send eight buttons: `AI A0 BI B0 CI C0 DI D0`. `F6-02-01` only defines
rockers A and B, but the rocker field is 3 bits, so the `F6-03-01` rockers C
and D can be sent too. The luminaire's own profile list even mentions rocker C
(`RPS F6-03-01 [C]`).

In practice, with a generic switch paired, `AI` switches on at full brightness
and `A0` switches off. Holding `A0` also just switches off, whatever the hold
time or `--pulse`, so hold-to-dim doesn't work. Rockers C and D do nothing.
That leaves the four `F6-02-01` buttons, which is also what LIGHT ADMIN's
Advanced mode offers (it calls them `A0 A1 B0 B1`, with the I contact written
as 1).

### Seeing what a button press did

Since only one program can use the serial port, `--watch N` sends the press and
then listens for the luminaire's telegrams in the same run:

```sh
waldmann switch AI --watch 25
```

The luminaire sends a `Unit Status` whenever the illumination mode changes, so
you get the new mode, dimming level and color temperature for each head. That
makes it easy to tell the heads apart and to check what a scene actually does.
If the press didn't change anything, nothing comes back.

Advanced mode scenes use the same terms as the EEP: Baselight is reduced light,
Worklight is working light, Servicelight is service light, and the color choice
is either a color temperature or the VTL chronotype.

The ERP1 status byte matters here. A rocker telegram needs `0x30` while the
button is pressed and `0x20` when it's released, otherwise the luminaire
ignores it.
