# Features

A tour of everything MeshHome does. The [README](../README.md#what-it-does) has the summary.

- [Messaging](#messaging)
- [Contacts](#contacts)
- [Repeater and room server administration](#repeater-and-room-server-administration)
- [Your radio's settings](#your-radios-settings)
- [Bot](#bot)
- [Map](#map)
- [Settings and administration](#settings-and-administration)
- [Reliability](#reliability)
- [API](#api)

## Messaging

- **Channels and direct messages** with unread counts, favorites, search, paginated history, a date
  badge per day, a "new messages" divider, per-conversation drafts, and a UTF-8 byte budget in the
  composer. The composer has a built-in emoji picker that draws emoji with the system emoji font.
- **Honest delivery states:** *Queued → Sending → Sent by radio / Delivered / Not confirmed delivered
  / Outcome uncertain / Failed / Expired*. Sends are idempotent, and an interrupted send is never
  re-sent automatically.
- **Add channel** (the **+** next to the search box) offers the same choices as the MeshCore app:
  create a private channel, join a private channel, join Public, join a hashtag channel, or scan a QR
  code. Hashtag and new private channels come with their key, a QR code, and a
  `meshcore://channel/add` link to share. A channel can have a **region scope**: messages you send on
  it only flood through repeaters serving that region. Scanning with the live camera needs HTTPS;
  over plain HTTP you can scan a photo instead.
- **Message menu** (right-click or long-press a message):
  - **Message details:** when it was sent and received, hops, path hash size, SNR and RSSI.
  - **Sender info:** the sender's contact. For channels this is matched by name, which isn't verified.
  - **View message paths:** the route of every copy your radio heard, with the repeaters named from
    your contacts. Paths are recorded from the radio's packet log while MeshHome is connected.
    Select a repeater for its details (see [Node details](#node-details)).
  - **Copy text**, **Block sender**, and **Delete** (removes the message from the archive only).
- **Conversation menu** (right-click or long-press a conversation): info, mark as read, favorite, mute
  or unmute, and delete or clear history.
- **One star per person:** starring a direct-message conversation and favouriting the contact are the
  same thing (the radio's favourite flag), so the sidebar, Contacts and the bot always agree. Channels
  have their own star.
- **Notification sounds:** a chime for new incoming messages while the app is open in a browser tab:
  *All messages*, *Direct messages only*, or *Off*, with per-conversation overrides.
- **Read state** is shared across browsers and only moves forward.

### Signed messages

Anyone with a channel's key can post under any name. A **signed** message carries a signature after
the text, made by the sender's radio with its own key, so you can tell it really came from that key
and wasn't changed. MeshHome uses the same format as MeshOS, so each can check the other's
signatures; other MeshCore apps and radios show the text as usual.

- **Ticks beside the time:** green, signed by one of your radio's contacts; grey, the signature
  checks out but the sender isn't a contact; a question mark, signed by a key your radio hasn't
  heard yet (checked as soon as it is); red, it doesn't match (altered, or not really from that
  sender). No tick: not signed. Tap a message to see who signed it.
- **Signed messages…** in a channel (the shield in its header, or its menu):
  - **Sign my messages:** the radio signs what you send there (its private key never leaves it).
    Signatures take 75 bytes, so messages are shorter.
  - **Only messages from my contacts:** hides messages that aren't signed by one of your radio's
    contacts. They aren't deleted: they come back if you add the sender or turn this off.
- **Trust is a contact.** A signature proves only that the holder of a key sent the message. Trust
  comes from adding that key as a contact after checking it with the person:
  - **Share my contact** (in the same dialog) posts your contact card in the channel, as MeshCore's
    standard contact link. In MeshHome and MeshOS it shows as a card with an **Add** button.
  - **Add** shows the start of their key (such as `B3F8 CE5B 40D1 A9E2`) to compare with them in
    person or on a call, then adds them to your radio's contacts.
  - **Contacts → My contact** (also the QR icon in **Account**, from your name at the bottom left)
    shows your QR code, link and key.
  - **Contacts → +** adds someone by scanning their contact QR code (the camera on HTTPS pages; a
    photo of the code works everywhere) or pasting their `meshcore://contact/add` link, after
    showing the start of their key to check.

The signature covers the channel, the time and the text, so it can't be moved to another channel
or reused. Contact cards are too long to carry a signature.

## Contacts

A compact, searchable list (name, device type, last heard) with type and status filters, sorting in
either direction, favorites first, and pagination. The list updates by itself as the radio hears
adverts. Click a person to open your conversation with them, or a repeater or room server to manage
it. Right-click or long-press a contact for:

- **Details**.
- **Share:** copy a `meshcore://` contact link, or re-broadcast the advert to nearby nodes.
- **Set path / Reset path:** choose the repeater route, or go back to flooding. Pick repeaters from
  a list (most recently heard first, with when each was last heard), type their hashes, or **pick
  them on the map and test it**: tap the repeaters in order (a line follows the route from your
  node), then **Test route** sends a trace along it and back, and shows the signal (SNR) each
  repeater heard and how long the round trip took. The map shows repeaters heard within the last 7
  days; **Heard within** shows more or fewer, and repeaters not heard for over a week are dimmed.
  **Undo** and **Clear** start over; **Use this route** sets it. A trace tests the repeaters, not
  the contact itself.
- **Favorite:** sets the radio's favourite flag, so the radio won't overwrite the contact when its
  contact list is full.
- **Block:** an app-side block. MeshCore radios can't block traffic, so the blocked contact's DMs, and
  channel messages under their name, are archived but hidden, never unread, and silent. Unblocking
  restores them.
- **Remove contact:** removes it from the radio. The archive keeps the conversation.
- **Remote manage** (repeaters and room servers), below.

**Adding a contact** before the radio hears their advert: **+** on the Contacts page scans their
contact QR code (camera on HTTPS; a photo of the code works everywhere) or takes their
`meshcore://contact/add` link, shows their name, type and key to check, and adds them to the radio.
Scripts can do the same with `POST /api/contacts/import` (see [API.md](API.md)).

**Blocked** and **Removed from radio** are different: *Blocked* is MeshHome's own setting (the
contact stays on the radio), while *Removed from radio* means the contact is no longer in the radio's
contact list (removed by you, by another app, or dropped by the radio when its list was full).

## Repeater and room server administration

**Remote manage** logs in to a repeater or room server with its admin or guest password, then offers:

- **Status:** battery (percentage and voltage), uptime, signal, noise floor, packet and airtime
  counters.
- **Command line:** any MeshCore CLI command, with quick commands and history.
- **Settings:** name, radio settings (with the region presets), transmit power, owner info, adverts and
  advert intervals, position (with a map picker), clock, access list, admin and guest passwords,
  regions, routing, repeat limits, telemetry, neighbours (select one for its details), reboot,
  and version. **Change identity key**
  gives the node a new key with a public-key prefix you choose.

To save airtime, nothing is fetched until you tap a refresh icon; the page shows what was fetched last
and when. Passwords and keys are never stored or logged.

## Your radio's settings

**Settings → Configure node settings** changes the connected radio's own configuration:

- **Identity:** name, location (with "use this device's location" and a map picker), and whether
  adverts share the location.
- **LoRa radio:** MeshCore's region presets (kept up to date from MeshCore) or custom frequency,
  bandwidth, spreading factor and coding rate; TX power; client repeat.
- **Channels:** add, rename, remove, or set a region scope. Keys can be hashtag-derived, the Public
  key, a generated random key (shown once), or entered by hand. Private keys are never displayed
  afterwards.
- **Contacts and routing:** auto-add contacts, extra ACKs, path hash size, and default flood scope.
- **Other:** telemetry permissions, firmware variables, advanced timing, adverts, clock sync, and
  reboot.

Values are checked against the firmware's own limits before anything is sent. Factory reset and
private-key export/import are deliberately left out; use official MeshCore tools for those.

## Bot

**Settings → Bot** (off by default) lets you query your home node from another radio by direct
message:

- `/info`: the node's name, MeshHome version, time online, radio settings and contact count.
- `/ping`: how your message arrived (SNR, RSSI, hops or direct route).
- `/help`: the list of commands, including those added by apps.

**Apps** add more commands. Choose **Browse apps** in Settings → Bot to open the
[app catalog](https://github.com/roach0816/MeshHome-apps), and filter it by category; the categories
come from the apps in the catalog. It currently has one app:

- **Ecowitt weather station** (`/weather`): outdoor temperature, humidity, wind, 24-hour rain and air
  quality from an Ecowitt gateway or Wi-Fi console (e.g. a WS90 through a GW1100/GW2000/GW3000) on
  your network. It reads the gateway's local live data, without the Ecowitt cloud.

An app is a recipe, not code. It lists the settings it needs, where it reads data from, and how to
word the reply. Before installing, you see its commands and what it will contact, and you can try it
with your settings. Apps are published in the catalog without a new MeshHome release. When one is
updated, MeshHome shows what changes (new commands, settings or hosts), and the new version only runs
after you choose **Update**. Recipes can't run code, contact hosts they don't declare, or put your
settings into a reply. Apps can also take readings that a device on your network *sends* to
MeshHome, for devices with an upload setting but no API to read from: see
[Bot apps](bot-apps.md#apps-a-device-sends-data-to-push-sources). Writing an app: see the catalog's
[FORMAT.md](https://github.com/roach0816/MeshHome-apps/blob/main/FORMAT.md).

It answers favourite contacts only, unless you allow every contact. Channels are never answered, and
blocked contacts are ignored. Replies are rate-limited to save airtime (quick follow-up commands are
answered in turn), and commands older than 15 minutes are ignored.

## Map

Shows the companions, repeaters, room servers and sensors heard by the gateway at the positions they
advertise (sharing a position is optional in MeshCore), filterable by type and by when each node was
last heard. Built with [Leaflet](https://leafletjs.com) and [OpenStreetMap](https://www.openstreetmap.org)
tiles by default; see [Configuration](configuration.md#map-tiles-and-privacy) for privacy and
self-hosted tiles.

### Node details

Open from a hop in a message's paths, or from a repeater's neighbour. They show the matching
contacts (a short key prefix can match more than one), each with its type, public key, when it was
last heard, its position and the town there, and its distance from your radio, plus **Remote
manage** or **Send message**. The town is looked up from OpenStreetMap Nominatim; turn that off in
**Settings → Map → Place names**.

## Settings and administration

- **Radio connection:** MeshCore TCP, the Raspberry Pi radio HAT (native installs), Simulated (sample
  traffic, no hardware), or None. Switch at any time.
- **Account** (click your name, bottom left): change your username and password, sign out, and
  see **signed-in devices** (browsers), with sign-out for any of them or all at once.
- **API keys:** see [API](#api).
- **Backup & restore:** see [Backup and restore](backup-restore.md).
- **Network & HTTPS** (native installs): see [HTTPS](install-native.md#https).
- **Software updates:** release notes and one-click updates on native installs, and the radio
  firmware check.
- **Maintenance pause/resume**, device information, collection gaps, JSON export, and deleting
  simulated data.
- Light and dark themes. Layouts for phone, tablet, and desktop. The browser tab reads
  "MeshHome - <hostname>", so tabs for different servers are easy to tell apart.
- **Install as an app** (needs HTTPS): see below.

### Install as an app

With HTTPS on, the owner can turn on **Settings → Install as an app**. Phones and computers can then
install MeshHome from the browser: on an iPhone or iPad, Safari's **Share → Add to Home Screen**; on
Android, Chrome's **⋮ → Install app**; on a computer, the install icon in Chrome's or Edge's address
bar. The installed app opens full-screen with its own icon and app-switcher entry, and long-pressing
the icon offers **Contacts** and **Map**.

It works through a web app manifest and a small service worker. The service worker keeps only
MeshHome's own files, so the app opens quickly, and shows a "Can't reach your MeshHome server" page
instead of the browser's error when the server is offline. Messages, contacts and settings are never
stored on the device: everything under `/api` always comes from the server. Turning the setting off
removes the service worker and its files from each browser the next time MeshHome opens there.

The switch stays unavailable until MeshHome is open over HTTPS, because browsers only install apps
and run service workers on secure pages. Each address is a separate app, so install it from the
address you use day to day.

**Notifications on this device** (Settings, once installing is on): new messages arrive as
notifications on that phone or computer even when MeshHome is closed, for direct messages, channels
or both. A conversation's own sound setting (on or off) overrides the choice. Tap a notification to
open the conversation. They're sent by your MeshHome server itself, as standard web push, through
the browser's push service (Google's for Chrome and Android, Apple's for Safari, Mozilla's for
Firefox). Each notification is encrypted for that browser, so the push service sees only the time
and size. No account, app store or relay is involved. On iPhone and iPad they work in the
home-screen app only (iOS 16.4 or later). **Send a test notification** checks the whole path.

The installed app also shows the **unread count on its icon** (where the system supports app
badges), **reconnects at once** when you return to it, and on Android appears in the **share
sheet**: share text or a link to MeshHome, choose a conversation, and the text is waiting in its
message box.

## Reliability

- **Durable receive pipeline:** the server fetches one message from the radio, commits it to the
  database, and only then fetches the next. Collection gaps (outages, pauses, restarts) are recorded
  and shown in Settings.
- **One radio owner:** a PostgreSQL advisory lock makes sure exactly one process owns the radio.
- **Realtime:** an authenticated WebSocket pushes changes; the app falls back to REST resyncs if that
  connection drops.

## API

**Settings → API keys** lets other services and scripts read messages, send them, and follow events
live, using `Authorization: Bearer <key>`. A key can be *read only* or *read & write* and can expire.
Keys are shown once and stored only as a fingerprint. Account, network, update, backup and radio
settings stay limited to the web interface. See the [API guide](API.md); the running app also serves
an interactive reference at `/api/docs`.
