# Backup and restore

**Settings → Backup & restore** makes one encrypted file that holds the whole installation, and
restores it on the same or another installation, native or container.

## What a backup contains

- The owner account and API keys (password hashes and key fingerprints only).
- All settings: radio connection, map, notifications, bot and its installed apps, and so on.
- The message archive, contacts, channels and the repeater-admin history.
- **Native installs also:** the network and HTTPS settings, the HTTPS certificate and key, the DNS
  provider credentials, and the radio HAT's identity, contacts and channels (ZephCore). These files
  are root-only, so the installer's root helper packs them; the unencrypted copy exists only until it
  is encrypted into the backup.

- **A connected TCP radio:** its identity key, name and location, radio (LoRa) and other settings,
  channels with their keys, and contacts. See [Radios](#radios).

Not included: sign-in sessions (everyone signs in again after a restore).

## Encryption

Every backup is encrypted with a passphrase you choose (at least 10 characters): AES-256-GCM with
the key derived using scrypt, sealed in authenticated chunks so a damaged, cut-off or altered file is
detected. The file can't be opened without the passphrase, and MeshHome doesn't store it. Keep
the passphrase somewhere safe: a lost passphrase means a lost backup.

## Where backups are kept

- **Native installs** keep backups in `/var/lib/meshhome/user-backups` until you delete them, with
  Download and Delete buttons in Settings. Download copies and keep them off the Pi:
  `uninstall --purge` deletes that folder.
- **Container installs** hand you the file to download straight away (the container has no persistent
  disk of its own).

## Restoring

Restore from **Settings → Backup & restore → Restore a backup…**, or on a fresh installation from the
setup wizard with **Restore a backup instead** (using the setup token). Upload the file and enter the
passphrase. MeshHome decrypts and checks the backup **before anything changes**, and lists what
will be restored, what won't be restored here and why, and anything to do afterwards. Then you
confirm.

| From → to | What happens |
| --- | --- |
| Native → native | Everything comes back. The certificate and DNS credentials are put back and the network settings applied; no new certificate is requested, so point the hostname at the new Pi if it moved. The HAT identity is restored; if no HAT is set up yet, setting it up uses it. |
| Native → container | App data only. HTTPS, the certificate, DNS credentials and the HAT identity aren't used (the cluster's Ingress handles HTTPS, and HATs need a Pi). A radio connection set to the HAT is cleared. |
| Container → native | App data only. The Pi keeps its own network and HTTPS settings. |
| From a newer version | Refused until you update this installation. Backups from older versions restore (new settings take their defaults). |

A restore replaces all data and signs everyone out; sign in with the backup's account. From
Settings, a copy of the data being replaced is saved first, encrypted with the same passphrase. On
native installs, the replaced certificate folder and HAT data are also kept as `*.before-restore`.

## Radios

A TCP radio, such as an Elecrow ThinkNode M7, keeps its identity, settings, channels and contacts
in its own memory, and reflashing its firmware can erase them. When you create a backup while it's
connected, MeshHome also saves a snapshot of it, inside the backup's encryption:

- **The identity key**: the private key that makes the radio *this* node, so its public key, and
  everyone's contacts for it, stay the same. MeshHome checks that the key matches the radio's
  public key before saving it. Some firmware builds don't allow exporting the key; the backup then
  says so, and saves everything else.
- **Settings**: name and location, radio (LoRa) settings, behaviour, telemetry, tuning, and the
  firmware's custom variables.
- **Channels**, with their keys, and **contacts**, with their saved paths and favourites.

The radio HAT isn't part of this: its identity is files on the Pi, saved with the system settings
above.

### Putting a radio back

Restoring a backup never changes a radio. To restore one, for example after reflashing it:

1. Reconnect the radio (**Settings → Radio connection**).
2. In **Settings → Backup & restore**, choose **Restore a radio from a backup…**, upload the backup
   and enter its passphrase. MeshHome shows the saved radio, and whether the connected radio has
   the same identity.
3. Choose what to restore: the identity key, settings, channels, and contacts. Channels replace the
   radio's channels; contacts are added where the radio doesn't have them.
4. MeshHome writes them, reports each part, and reconnects to read the radio again. With the
   identity restored, the radio picks up its message archive and contacts in MeshHome where they
   were.

Settings are checked like they are on the Device page, and adjusted to the radio where needed: for
example, transmit power is capped at what this radio allows.

Only restore an identity to one radio at a time: two radios with the same identity on the mesh
confuse routing. The key is restored only if the radio's firmware allows importing one.

## Database dumps

These are separate from the encrypted backups above and are meant for repairs:

- **Native:** `sudo meshhome backup` writes an unencrypted database dump to
  `/var/lib/meshhome/backups`. Updates make one automatically before upgrading.
- **Kubernetes:** the `meshhome-db-backup` CronJob runs daily at 03:17 UTC. It writes
  `meshhome-<timestamp>.dump` and a `.sha256` file to the `meshhome-backups` volume, keeps 30 days,
  and always keeps the newest three. To run one now:
  `kubectl -n meshhome create job --from=cronjob/meshhome-db-backup backup-now`.

Restoring a dump in Kubernetes (fetch the dump from the backup volume, pause the radio in Settings
first):

```bash
kubectl -n meshhome exec -i deploy/meshhome-db -- \
  pg_restore --clean --if-exists -U meshhome -d meshhome < meshhome-<timestamp>.dump
kubectl -n meshhome rollout restart deploy/meshcore
```
