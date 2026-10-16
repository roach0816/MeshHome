# Configuration

Everything about *your* installation (owner account, radio, map, HTTPS hostname, and so on) is set in
the first-run wizard or in Settings, and stored in the database. Nothing installation-specific lives in
the repository or the image. Environment variables only cover deployment plumbing.

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:5432/db` (keep in a secret) | required |
| `RADIO_ENABLED` | `false` keeps the radio closed regardless of saved settings | `true` |
| `SETUP_TOKEN` | Fixed first-run token instead of a random one | random |
| `COOKIE_SECURE` | `auto` / `true` / `false` | `auto` |
| `SESSION_DAYS` | Sign-in lifetime. App sign-ins renew while in use, so this is how long an unused app stays signed in. | `30` |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` | `INFO` |
| `SEND_EXPIRY_SECONDS` | Queued sends older than this are not transmitted | `60` |
| `MESHHOME_INSTALL_KIND` | `native` enables in-place upgrades and system settings from the UI (set by the installer). The older name `MESHCORE_INSTALL_KIND` also works. | `container` |
| `MESHHOME_STATE_DIR` | Writable state directory for a native install. The older name `MESHCORE_STATE_DIR` also works. | — |
| `UPDATE_REPO` | GitHub `owner/repo` checked for new releases; empty disables update checks | this repo |
| `RELEASE_NOTES_URL` | Where the version number links (`{version}` is substituted); empty for no link | this repo's GitHub releases |
| `RADIO_PRESETS_URL` | Where MeshCore's region presets are fetched; empty uses the built-in list only | MeshCore's server |
| `FIRMWARE_REPO` | GitHub `owner/repo` of MeshCore companion firmware releases, for the firmware check | `meshcore-dev/MeshCore` |

## Running one instance

Run the app as a **single process** (`--workers 1`, one replica, `strategy: Recreate` in Kubernetes),
because exactly one process may own the radio. A PostgreSQL advisory lock enforces this: a second
instance can serve history but will not connect to the radio.

## Map tiles and privacy

The browser loads map tiles directly from the configured tile server. That server sees your IP
address, which map areas you view, and the app's origin (the tile requests send the origin as
`Referer`, as OpenStreetMap's tile policy expects). Nothing else is sent: no message content, node
names, or keys. OpenStreetMap's public tiles suit light personal use. To avoid any third-party
requests, point **Settings → Map** at a self-hosted XYZ tile server.

## Inbound uploads

Bot apps with a push source receive readings at `/push/<code>` on MeshHome's plain-HTTP address.
That is the only route that needs no sign-in. It only accepts uploads from the local network, and
the code is a 32-character random value per app (Settings → Bot → the app → New address). See
[Bot apps](bot-apps.md#plain-http) for HTTPS and Kubernetes installs.

## Outbound connections

Besides the radio, MeshHome connects out to:

- GitHub, every few hours, to check for MeshHome releases and MeshCore firmware releases
  (`UPDATE_REPO`, `FIRMWARE_REPO`; empty values turn the checks off);
- MeshCore's server, once a day, for the region presets (`RADIO_PRESETS_URL`);
- the push service of each browser that turned on **Notifications on this device** (Google's,
  Apple's, Mozilla's or Microsoft's), with each notification encrypted for that browser;
- OpenStreetMap Nominatim, when you open a node's details, to name the town at its advertised
  position. Only those coordinates are sent, each position is looked up once and cached, and
  **Settings → Map → Place names** turns it off;
- the bot app catalog on GitHub (`BOT_APPS_CATALOG_URL`; empty uses only the apps included with
  MeshHome), every few hours or when you refresh it;
- what installed bot apps read when the bot answers their commands: a device on your local network
  (such as an Ecowitt gateway) or the internet hosts the app lists;
- on native installs with HTTPS, Let's Encrypt and your DNS provider, to obtain and renew the
  certificate.
