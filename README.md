# Rain Bird

# Rain Bird for eisy / IoX

Local-network control of Rain Bird sprinkler controllers through the Rain Bird WiFi module (**LNK**). Built on the open-source [`pyrainbird`](https://github.com/allenporter/pyrainbird) library (MIT). It talks directly to the controller's IP address - no Rain Bird cloud account is involved. Developed for an **ESP-Me**; other controllers that use the same protocol (ESP-TM2, ESP-ME3, ...) may work too, but only an ESP-TM2 is listed as fully verified by the library authors.

> **LNK2 modules:** the released `pyrainbird` 6.0.2 only speaks plain HTTP (LNK). LNK2 modules need HTTPS, which only newer, unreleased library code supports. When a newer release is published the plugin will use it automatically (`create_controller`).

## Settings

Only two: **host** (the WiFi module's IP address) and **password**. There is no user name or email.

## What you get

**Rain Bird Plugin** (controller node): connection status and a *Discover* command.

**Rain Bird <model>** (one node):
| Property | Meaning |
|---|---|
| Watering | Idle / Watering (any zone running) |
| Rain Delay | days remaining |
| Rain Sensor | Clear / Rain Detected |
| Water Budget | seasonal adjust, % (read-only) |

Commands: *Stop All Watering*, *Set Rain Delay* (0-14 days), *Run Program* (A-D).

**One node per zone**, named with the names you enter in the configuration:
| Property | Meaning |
|---|---|
| Zone State | Off / Running |
| Run Time | minutes used when the zone is turned on (1-240) |

Commands: *On / Run Zone* (optional minutes, defaults to the node's Run Time), *Off / Stop Watering*, *Set Run Time*.

## Setup

1. Install the plugin and open its configuration page.
2. Enter **host** (reserve the address in your router) and **password** (the one from the Rain Bird app).
3. Optionally add rows under **Zone names**.
4. Restart the plugin if nodes do not appear; then press *Discover*.

## Good to know

* The controller handles **one client at a time**. Opening the Rain Bird app while the plugin is polling can cause a brief "busy" message; the plugin retries up to 3 times.
* Zone status polls every 30 s; rain delay, rain sensor and water budget every 5 min.
* The Rain Bird protocol can only *stop all* watering, so a zone's *Off* command stops watering only if that zone is currently running.
* **Safety pauses** (shown as a notice on the plugin page): a rejected password pauses polling until you save the configuration; 3 undecodable replies in a row (usually a wrong password) pause for 16 minutes; 3 network failures in a row pause for 2 minutes.
* On every start the plugin removes any banner it did not create itself and names its controller node "Rain Bird Plugin", so leftovers from another plugin that previously used the same slot disappear.
* Water budget is read from program A. Changing it is not supported by the released library - use the Rain Bird app.
* Program letters A-D are sent as program numbers 0-3 (not verified against hardware - check on first use).
* Schedules/programs themselves are not edited by this plugin.

## Requirements

`pyrainbird>=6.0.2` (installed automatically).

## License Notice

This plugin's development referenced the following source(s), whose license is not on the standard permissive allowlist. Review their license terms before relying on or redistributing this plugin:

- [rainbird-iq4-cli/docs/IQ4-API.md at main · nickustinov/rainbird-iq4-cli · GitHub](https://github.com/nickustinov/rainbird-iq4-cli/blob/main/docs/IQ4-API.md) -- license: unknown
- [GitHub - bbreukelen/node-rainbird: Rainbird controller in Node.js · GitHub](https://github.com/bbreukelen/node-rainbird) -- license: GPL-3.0

See LICENSE.md for full third-party attribution.
