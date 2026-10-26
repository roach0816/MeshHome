# Raspberry Pi and Debian install (native)

A native install runs directly on the operating system, without Docker, with its own local
PostgreSQL database. This page covers the details; the [README](../README.md#option-1-raspberry-pi-or-debian-native)
has the short version.

- [Requirements](#requirements)
- [What the installer does](#what-the-installer-does)
- [Where things live](#where-things-live)
- [The `meshhome` command](#the-meshhome-command)
- [Diagnostics: `meshhome status`](#diagnostics-meshhome-status)
- [Radio HAT on the Pi (RAK6421)](#radio-hat-on-the-pi-rak6421)
- [HTTPS](#https)
- [Updates](#updates)
- [Installs from before the rename (MeshCore Home 0.9 and earlier)](#installs-from-before-the-rename-meshcore-home-09-and-earlier)

## Requirements

- A **Raspberry Pi 4 or 5** with **Raspberry Pi OS (64-bit)**, or any 64-bit **Debian 12 (bookworm)**
  or **Debian 13 (trixie)** system. 32-bit Raspberry Pi OS is not supported.
- Internet access during installation (packages and the release download).
- For the radio HAT: Raspberry Pi OS 13 "Trixie" or Debian 13 (see [Radio HAT](#radio-hat-on-the-pi-rak6421)).

## What the installer does

```bash
curl -fsSLo install.sh https://github.com/roach0816/MeshHome/releases/latest/download/install.sh
sudo bash install.sh
```

The installer runs as a full-screen dashboard that stays in place instead of scrolling. It shows the
checklist of steps, what is happening now, and one overall progress bar. Each question appears on
the same screen with an explanation of what will change. When it finishes, a short summary stays in
your terminal; the full detail is in `/var/log/meshhome-install.log`. Upgrades
(`meshhome update`) and the uninstaller use the same screen. Small terminals (under 64×20),
pipes, and `--plain` get plain line-by-line output instead.

1. **Check this system:** OS, 64-bit CPU, systemd, memory, disk space, and internet access.
2. **Choose the version:** the latest release.
3. **System packages:** lists exactly which apt packages it will install, with versions,
   descriptions, and download size, then asks **Install these packages? [Y/n]**.
4. **Review system changes:** asks for the port (default 8080), lists every change it will make
   (system user, directories, database, services, port), then asks **Make these changes? [Y/n]**.
5. **Download and verify** the release package (SHA-256 checksum).
6. **Install the app** into its own Python environment (prebuilt packages, no compiling).
7. **Set up the database:** a local PostgreSQL database, reached as the app's OS user, so there's no
   password.
8. **Start the service** and show the address to open and the **first-run setup token**.
9. **Radio HAT (optional, Raspberry Pi 4/5):** see [Radio HAT](#radio-hat-on-the-pi-rak6421).
10. **HTTPS (optional):** see [HTTPS](#https).
11. **Automatic security updates (optional, recommended):** turns on Debian's `unattended-upgrades` so
    OS security fixes install daily. It never upgrades MeshHome itself.

Answering **n** at any prompt cancels the installation. Re-running the installer is safe: it picks up
where a failed attempt stopped, and repairs a broken install. `--yes` answers every question with its
default: a detected radio HAT is set up, HTTPS is skipped (set it up later in Settings), and
automatic security updates are turned on.

## Where things live

| Where | What |
| --- | --- |
| `/opt/meshhome/releases/<version>` | The app. `current` points at the active version; the previous one is kept for rollback |
| `/etc/meshhome/meshhome.env` | Configuration (port, database URL, network settings), readable only by root and the app |
| `/etc/meshhome/acme/` | HTTPS certificate, Let's Encrypt account and DNS provider credentials (root only) |
| `/var/lib/meshhome` | Update status, setup token, database dumps, and your backups (`user-backups/`) |
| `/var/lib/meshhome-radio` | The radio HAT's identity, contacts and channels (ZephCore) |
| `meshhome.service` | Runs as the unprivileged `meshhome` user, with the rest of the system read-only to it |

The web app never runs as root. Changes that need root (updates, network and HTTPS settings, the
radio HAT, backup and restore of system files) are written as a request file and carried out by
root-owned helper units (`meshhome-update`, `meshhome-config`), which re-check every value.

## The `meshhome` command

```text
sudo meshhome status    diagnostic report (start here when something is wrong)
sudo meshhome https     set up HTTPS (or `https --disable` to go back to plain HTTP)
sudo meshhome security-updates   turn on automatic OS security updates
sudo meshhome radio-hat set up the RAK6421 radio HAT (also: radio-hat remove | restart | logs)
meshhome logs [-f]      application log
sudo meshhome update    upgrade to the latest release (same wizard, with a database dump)
sudo meshhome backup    dump the database now (for repairs; see docs/backup-restore.md)
sudo meshhome uninstall [--purge]   remove the app (--purge also deletes the data)
sudo meshhome migrate   move an install from before the rename to the new names (see below)
```

**Uninstalling** without `--purge` removes the app, its services and the HTTPS site, but keeps the
database, settings, HTTPS certificate and DNS credentials. Reinstalling picks them all up again,
including HTTPS. If HTTPS can't be restored (for example, the certificate is gone and a new one
can't be obtained), the installer switches to plain HTTP so the app stays reachable, and
`sudo meshhome https` sets HTTPS up again. `--purge` deletes everything.

## Diagnostics: `meshhome status`

`meshhome status` checks every part of the installation and marks each line ✓ (fine),
• (information), ! (warning) or ✗ (problem), with a suggested fix for each problem:

- **App:** version, service (including crash loops), web health, and whether the app is responding.
- **Radio:** connection state and last error. If the radio is disconnected, it also tests whether the
  gateway's address and port can be reached, which tells a network problem apart from a firmware or
  handshake problem. Collection gaps are listed too.
- **Database:** PostgreSQL and the archive size.
- **Network & HTTPS:** the address, nginx, certificate expiry, and automatic renewal.
- **Updates:** the latest release, the last upgrade's result, and OS security updates.
- **System:** disk, memory, clock synchronization, temperature, and Raspberry Pi under-voltage.
- **Radio HAT** (when set up): hardware, SPI, the service, port restrictions, and recent radio errors.
- **Recent log problems:** the last 24 hours, with repeated lines grouped.

The report contains no passwords, keys, or tokens, so you can paste it when asking for help (it does
include network addresses). It exits with status 1 when it finds a problem, so scripts and monitoring
can use it too.

## Radio HAT on the Pi (RAK6421)

Instead of a separate radio on your network, a Raspberry Pi 4 or 5 can carry the radio itself: a
**RAKwireless WisMesh Pi HAT (RAK6421)** with a **RAK13300** (or RAK13302) LoRa module in **IO slot 1**
and an antenna for your region. Nothing needs flashing: the radio module has no firmware of its own.

MeshHome installs and manages the radio software for you. It uses
[ZephCore](https://github.com/liquidraver/ZephCore) (MIT licence), the MeshCore firmware ported to
Linux, which drives the radio over SPI and serves the standard MeshCore companion protocol.

- **Set it up** in **Settings → Radio connection → Radio HAT on this Pi**, or with
  `sudo meshhome radio-hat`. The installer also offers it when it runs on a Pi 4 or 5.
- **What setup does:**
  - Downloads the ZephCore build for your Pi model, pinned by the release
    (`deploy/native/zephcore.lock`) and checked against its SHA-256.
  - Turns on SPI. The Pi needs **one restart**, which you can start from the web interface.
  - Runs ZephCore as its own service (`meshhome-radio`) under an unprivileged user that may only
    use the SPI and GPIO devices.
  - Limits the companion port (5000) to the Pi itself: it has no password, so other devices on your
    network cannot connect.
- **Configure the radio** from MeshHome's **Node settings**: name, frequency, power, and channels.
  **ZephCore starts on the EU/UK frequency (869.618 MHz)**, so outside Europe set your region's
  preset before sending.
- **Updates and removal:** MeshHome upgrades bring tested ZephCore updates, and keep the previous
  version if a new one does not start. Removing the HAT keeps the radio's identity, contacts, and
  channels; `uninstall --purge` deletes them. [Backups](backup-restore.md) include them too.
- **Requirements:** a Raspberry Pi 4 or 5 running **64-bit Raspberry Pi OS 13 "Trixie"** (or
  Debian 13). ZephCore's builds need glibc 2.38, so Raspberry Pi OS 12 "Bookworm" is not supported;
  MeshHome explains this instead of offering setup. Do not install Meshtastic (`meshtasticd`): it
  would take over the radio.

The radio HAT has been tested end to end in Debian 13 containers against the real ZephCore software,
but not yet on real RAK6421 hardware. Treat the radio side as unverified until it has run on a real
HAT.

### Sensors on the HAT

The HAT's sensor slots can carry WisBlock sensors that MeshHome reads
([Sensors on the Pi](features.md#sensors-on-the-pi)): a BME680 environment sensor (RAK1906) in slot
B, C or D, and a GPS (RAK12501) in slot A, the long slot. Power the Pi off before fitting a module,
and press long modules in at both ends until they snap in.

- **I²C** must be on (`sudo raspi-config` → Interface Options → I2C). `sudo i2cdetect -y 1` should
  show the BME680 at `76` (or `77`).
- **The serial port** must be on, with its login console off: `sudo raspi-config` → Interface
  Options → Serial Port, login shell **No**, hardware **Yes**, then reboot. The GPS then sends at
  9600 baud on `/dev/serial0` (`ttyS0`) on a Pi 4, or `/dev/ttyAMA0` on a Pi 5, which MeshHome
  picks automatically.
- MeshHome's service joins the `i2c` and `dialout` groups by itself when it starts.

## HTTPS

Without HTTPS, the Pi serves **plain HTTP** on port 8080. Passwords and session cookies then cross
your network unencrypted, and features that browsers only allow on HTTPS pages are hidden: copy
buttons, "Use this device's location", live QR scanning, installing as an app, and notifications.

HTTPS is offered as the last step of the installer. You can also turn it on, change it, or turn it
off later in **Settings → Network & HTTPS**, or with `sudo meshhome https`. The Settings card
shows the address, HTTPS status, certificate expiry, and app port. **Configure…** opens a dialog for:

- the app port;
- HTTPS on/off, the hostname, the HTTPS port, and the HTTP→HTTPS redirect;
- the Let's Encrypt email, your DNS provider and its credentials, the DNS wait, and a staging option
  for testing;
- **Renew now**.

Before anything changes, the dialog lists exactly what will happen. The change is applied by the
root-only helper. If the app doesn't come back afterwards, the previous configuration, including
previously saved credentials, is restored automatically. If a certificate can't be obtained, nothing
changes, and you can retry.

HTTPS uses:

- **nginx** in front of the app on ports 80/443. Port 80 redirects to HTTPS, except uploads from
  devices to bot apps (`/push/`), which can't use HTTPS and are passed to the app. The app then
  listens on `127.0.0.1` only. nginx comes from Debian's own repositories and is listed and confirmed
  **[Y/n]** before installing.
- A trusted **Let's Encrypt** certificate validated through your **DNS provider** (DNS-01). The Pi
  doesn't need to be reachable from the internet, so it works for a private, LAN-only hostname.
- [lego](https://github.com/go-acme/lego) (MIT licence) to get and renew the certificate. The
  installer downloads the version pinned in `deploy/native/lego.lock` and checks its SHA-256. A timer
  (`meshhome-acme-renew.timer`) checks twice a day and renews about 30 days before expiry.

### Supported DNS providers

| | | | | |
| --- | --- | --- | --- | --- |
| Cloudflare | Amazon Route 53 | Google Cloud DNS | Azure DNS | DigitalOcean |
| GoDaddy | Namecheap | Porkbun | OVHcloud | Gandi |
| Hetzner DNS | Linode (Akamai) | Vultr | IONOS | Hostinger |
| Name.com | NameSilo | Dynadot | DNSimple | Netlify |
| Vercel | deSEC | Duck DNS | Infomaniak | Self-hosted (RFC 2136: BIND, PowerDNS, Knot, Technitium) |

Each provider asks only for its own credentials (an API token, or a key and secret; Google takes a
service-account JSON key). The form and installer say where to create them. **GoDaddy** only allows
API access for accounts with 10+ domains or a paid plan, and **Namecheap** needs API access turned on
and your public IP address allowlisted. Credentials are stored only in
`/etc/meshhome/acme/credentials.env`, readable by root only, and are never shown again, logged,
or given to the app.

You need:

1. A hostname in a domain at one of the providers above (e.g. `meshhome.<your-domain>`).
2. API credentials for that provider, limited to that domain's zone where the provider allows it.
3. A local DNS record (router, Pi-hole, etc.) pointing the hostname at the Pi's LAN address.

### My DNS provider isn't listed

Some registrars (Squarespace, Wix, Bluehost, Network Solutions, Hover) have no DNS API. Hand just the
validation to a free provider that has one: create a free [deSEC](https://desec.io) account with a
name such as `yourname.dedyn.io`, then at your registrar add one CNAME record,
`_acme-challenge.meshhome.<your-domain>` → `_acme-challenge.yourname.dedyn.io`. Choose **deSEC** with
its token. lego follows the CNAME, and the rest of your DNS stays where it is.

**Older installs that used certbot** with Cloudflare keep working unchanged. The next time the HTTPS
settings are saved (or `sudo meshhome https` runs), renewals move to lego, reusing the saved
Cloudflare token, and certbot's renewal for that site is turned off.

## Updates

The app checks this repository's GitHub Releases for new versions every few hours. When one is
available, the version under your name in the sidebar changes to **Update to …**, and
**Settings → Software updates** shows the release notes with an **Install** button. Installing:

1. downloads the release and verifies its checksum;
2. dumps the database;
3. installs the new version next to the current one and restarts the app (about a minute offline);
4. **rolls back automatically** if the new version doesn't start.

The web app only asks for an update by writing the requested version number to a file. The
root-owned `meshhome-update` unit double-checks it against the official releases before
installing anything. Migrations only ever add to the database, so rolling back is always safe.

The same page also compares the **radio's MeshCore firmware** with MeshCore's latest companion
release. MeshHome does not install radio firmware; companion firmware is updated over USB (see
[Radios](radios.md#firmware)).

## Installs from before the rename (MeshCore Home 0.9 and earlier)

Installs made before the rename keep working under their old names (`/opt/meshcore-home`, the
`meshcore-home` service and command, the `meshcore` user and database), and receive updates as usual.
To move one to the new names, update it to 0.10 or later and then run:

```bash
sudo meshcore-home migrate
```

It lists every change and asks before making any. Then it stops the app, dumps the database, and
renames the database, its role, the system users, the directories, the env file, the services, the
nginx site and the command. Nothing is copied. Messages, settings, sign-ins, API keys, HTTPS and the
radio HAT carry over, and the app is offline for about a minute. Each step records how to undo
itself. If any step fails, or the app doesn't come back healthy, every step is undone and the install
runs as before. The old command, `sudo meshcore-home`, keeps working afterwards.
