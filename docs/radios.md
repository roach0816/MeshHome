# Radios

MeshHome talks to one **MeshCore companion radio**, the same kind of radio the MeshCore phone
and desktop apps use, over the companion protocol carried on a TCP socket (port `5000` by default).

- [Which radios work](#which-radios-work)
- [Connecting a TCP gateway](#connecting-a-tcp-gateway)
- [Radio HAT on a Raspberry Pi](#radio-hat-on-a-raspberry-pi)
- [Troubleshooting](#troubleshooting)
- [Firmware](#firmware)

## Which radios work

| Works | Does not work |
| --- | --- |
| MeshCore **companion** firmware with a TCP/Ethernet build, e.g. an Elecrow ThinkNode M7 over Ethernet, or a RAK4631 + RAK13800 Ethernet (+ RAK19018 PoE) with the `RAK_4631_companion_radio_ethernet` target (MeshCore v1.17.0 or later) | MeshCore **repeater** or **room server** firmware (manage those *through* your radio with Remote manage instead) |
| Any other board whose MeshCore companion build exposes the companion protocol over TCP | Meshtastic firmware, or MQTT-only gateways |
| A RAK6421 radio HAT on a Raspberry Pi 4/5, driven by ZephCore (native installs) | Companion radios reachable only by Bluetooth or USB serial (not supported yet) |

**The gateway has its own identity.** It has its own MeshCore keys, contacts, and channels. Direct
messages reach this inbox only if they are addressed to the gateway's identity. Channel messages
appear if the gateway has the same channel configured (same name and key) and can hear the traffic.

## Connecting a TCP gateway

### 1. Prepare the gateway

- Flash MeshCore **companion** firmware with TCP/Ethernet support for your board.
- Attach the antenna before transmitting.
- Choose your region's radio preset, a name and channels. You can do all of this from MeshHome
  once it's connected (**Settings → Configure node settings**), or with a MeshCore client first.

### 2. Give it a stable address and make it reachable

- Create a **DHCP reservation** (or static IP) for the gateway, and note its IP or hostname.
- MeshHome connects **out** to the gateway over plain unicast TCP. In Kubernetes this works with
  normal pod networking: no `hostNetwork`, multicast, or mDNS. Use an IP address, or a DNS name the
  server can resolve. `.local` names will not resolve inside pods.
- **Firewall:** the gateway's TCP port gives full control of the radio and has no password. Allow it
  only from the MeshHome server (in Kubernetes, pod traffic usually leaves through the node's IP)
  and from any maintenance machine. Never expose it to the internet or to guest networks.

### 3. Connect it in the app

1. Go to **Settings → Radio connection** and choose **MeshCore TCP**.
2. Enter the gateway's IP or hostname and port, then click **Test reachability**. This only checks
   that the TCP port is open from the server.
3. Click **Save and reconnect**. The status card should move through *Connecting* to *Radio
   connected*. **Settings → Device** then shows the gateway's name, public key, firmware, radio
   settings, and channels.
4. Send a test message from another MeshCore device to one of the gateway's channels, or as a DM to
   the gateway. Check that it appears, then reply from the web app.

If you used the simulated radio first, use **Settings → Data → Delete simulated data** to remove the
sample conversations. This option is available once the mode is no longer *Simulated*.

## Radio HAT on a Raspberry Pi

On a native install, a Raspberry Pi 4 or 5 can carry the radio itself (RAK6421 HAT with a RAK13300
module), and MeshHome installs and runs the radio software for you. See
[Radio HAT on the Pi](install-native.md#radio-hat-on-the-pi-rak6421).

## Troubleshooting

On a Raspberry Pi or Debian install, start with `sudo meshhome status`
([details](install-native.md#diagnostics-meshhome-status)). It checks the radio connection and
the rest of the installation, and suggests what to do next.

| Symptom | Likely cause |
| --- | --- |
| "Not reachable" / timed out | Wrong IP or port, a firewall blocks the server, or the gateway is offline |
| Reachable, but status shows "companion did not answer the handshake" | The device on that port is not running MeshCore companion firmware, or another client is holding the connection |
| Connected, but no channel messages | The channel name or key differs from the rest of the mesh, or the gateway can't hear the traffic (check placement and antenna) |
| DMs don't arrive | The sender is messaging a different node; DMs must be addressed to the gateway's own identity |
| "Read-only (another instance owns radio)" | A second copy of the app is running against the same database; only one may own the radio |

The server retries with exponential backoff (up to 30 s) and never needs a restart for an ordinary
radio outage. When you need a desktop or CLI client to talk to the gateway directly, use **Pause for
maintenance**, which releases the TCP connection. Resume when you're done.

**Message size:** the composer enforces conservative UTF-8 byte limits (`DM_MAX_BYTES` and
`CHANNEL_MAX_BYTES` in `backend/app/radio/base.py`).

## Firmware

**Settings → Software updates → Radio firmware** compares the radio's MeshCore firmware version with
MeshCore's latest companion release, and **Settings → Device** shows *Up to date* or *… available*
next to the firmware version.

MeshHome does not install radio firmware. Companion firmware has no network update path (the
Wi-Fi update mode exists only in repeater and room-server firmware), so update the radio over USB
from a computer, for example with MeshCore's web flasher. nRF52 boards can also update over Bluetooth.
The radio HAT's ZephCore is the exception: it updates together with MeshHome.

Reflashing can erase the radio's identity, settings, channels and contacts. Before you flash, make a
backup in **Settings → Backup & restore** while the radio is connected: it saves all of these. After
flashing, reconnect the radio and use **Restore a radio from a backup…** to put them back (see
[Backup and restore](backup-restore.md#radios)).
