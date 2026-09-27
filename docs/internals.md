# How the bridge works

Some notes for working on the code.

## Modules

| module | |
|---|---|
| `protocol.py` | the EnOcean protocol, shared by the bridge and the CLI |
| `cli.py` | the `waldmann` command line |
| `paths.py` | where the config and pairings are stored |
| `bridge/config.py` | settings file and web login |
| `bridge/discovery.py` | Home Assistant MQTT Discovery payloads |
| `bridge/core.py` | the bridge itself: serial loop, MQTT and state |
| `bridge/web.py` | the web UI and its JSON API |
| `bridge/index.html` | the page |

The bridge and the CLI both use `protocol.py`, so anything that works in the
CLI works in the bridge. Don't build telegrams in the bridge itself.

## Serial port access

**Only the main loop touches the serial port.** MQTT callbacks and web requests
put work on a queue that the main loop picks up, so there's only ever one
writer. Anything that needs the radio from another thread has to go through
that queue, even something small like reading the Base ID.

## Home Assistant identifiers

Home Assistant tracks entities by their discovery node name and `unique_id`
(`waldmann_enocean_<id>_u<N>`). If you change `NODE_PREFIX` in `discovery.py`,
the existing entities are orphaned and you get duplicates next to them. Treat
that as a breaking change. The base topic isn't part of either, so changing it
only moves the state and command topics, and Home Assistant updates the
existing entities.

## Re-reading state after a command

The luminaire sends a Unit Status as soon as a change starts, so the values in
it are from halfway through the fade, and the final values are never sent.
Without a re-read, Home Assistant would show those in-between values until the
next poll, which can be minutes later. That's why `schedule_followup` reads the
state again 5 and 15 seconds after every command.

## Web UI

### Saving settings

The Settings tab is split into cards, but that's only for layout. Switching to
a TLS broker for example touches two cards, so there's one **Save** for the
whole form in a bar at the bottom. The bar is always visible and the buttons
are disabled until you change something, so it's clear how saving works. Only
changed fields are sent (`POST /api/config` merges whatever it gets), so
changes across several cards end up as one write and one MQTT reconnect.

The login fields are part of the same form and the same Save, but they're
sent to `POST /api/password` instead. That endpoint checks the current
password first and then signs out every session. An empty new password means
"keep the current one", so you can change only the username. `web_username`
is in `Config.PROTECTED_FIELDS`: you can read it, but `/api/config` won't
write it, otherwise the username could be changed without the password check.
If the current password is missing, nothing is saved at all, so you don't end
up with the settings saved and the login change silently dropped.

### Login

The UI has its own login form instead of the browser's Basic auth dialog,
because several browsers won't show that dialog on plain HTTP. The session is
a bearer token in `localStorage`. Basic auth still works for curl and scripts.

The login form is a normal `method="post"` form with `username` and `password`
fields, and it's removed from the page after you log in. That's what browsers
look for before offering to save the password. Chrome and Edge also get the
credential through `navigator.credentials.store()`. Safari and Firefox don't
support that, so they only go by the form.

### Other details

The theme selector's *Auto* setting follows the browser's
`prefers-color-scheme`, so the same page can be light in one browser and dark
in another. *Light* and *Dark* override that and are remembered per browser in
`localStorage`.

The serial port field is empty when the port is detected automatically. The
form shows which port was found and lists the ports it can see, so you can
pick one.

For TLS, a broker with a publicly trusted certificate needs nothing extra. For
a self-signed certificate, set **CA certificate file**. **Skip certificate
verification** is only meant for testing.

### Screenshots

The screenshots in `docs/images` are made with `tools/screenshots.py`, which
renders the page in headless Chrome with made-up data. Run it again after
changing the UI:

```sh
python3 tools/screenshots.py
```
