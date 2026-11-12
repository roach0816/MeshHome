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
- **Automatic retries, like the MeshCore app:** a direct message that gets no delivery ACK is sent
  again, up to 3 attempts in all, each with the same timestamp, so the recipient can tell it's a
  repeat. With a stored route, the first two go along it; then the route is reset and the last
  attempt floods the mesh (a delivered flood teaches the radio a new route). A contact with no
  stored route gets two flood attempts. An ACK for any attempt counts as delivered. The message
  shows each step ("Sending attempt 2 of 3…", "Delivered on attempt 3 by flood"), and **Retry**
  afterwards starts a fresh round. Bot replies are sent once, to save airtime.
- **Preferred repeater** (Settings → Radio): pick the repeater you rely on, such as one on your roof,
  and direct traffic goes through it whenever that works. The radio on its own keeps whichever route
  answered a flood first, which can skip your repeater. MeshHome instead remembers a route through
  it for each contact and puts it on the radio before every direct send: messages, bot replies,
  room server posts, and remote management. Routes are learned from the radio's own route, from
  direct messages and adverts that arrive through the repeater (so repeaters, room servers and
  sensors get one too), and from a path you set that starts at it. If two attempts on it go
  unanswered, the next attempt floods around it and that contact skips it for 30 minutes. When
  that happens to two contacts in a row, the repeater counts as down, and sends go around it,
  until your radio hears it relay something. Delivered messages say so ("Delivered via Roof").
  A contact's details show its remembered route, with a switch to turn it off for that contact.
  Setting a path by hand that doesn't start at the repeater also turns it off for that contact.
  Channel messages always flood through every repeater, so this doesn't apply to them.
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

A reply too long for one direct message (150 bytes) goes out as up to three, marked `(1/2)`, `(2/2)`
and so on, in order. The bot only answers direct messages, which are never signed, so signing a
channel doesn't shorten its replies.
- `/env`: the readings of the environment sensor on this Pi ([Sensors on the Pi](#sensors-on-the-pi)),
  such as `🌡️ 21.4°C · 💧 48% RH · ⏲️ 1013.2 hPa · 🍃 air 120 kΩ`. **Answer /env**, beside it in the
  command list, turns it off; when it's on it follows **Who can use it** like the other commands.
  Raspberry Pi / Debian installs only.

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
self-hosted tiles, and [Offline map](#offline-map) to save an area for use without internet.

### Node details

Open from a hop in a message's paths, or from a repeater's neighbour. They show the matching
contacts (a short key prefix can match more than one), each with its type, public key, when it was
last heard, its position and the town there, and its distance from your radio, plus **Remote
manage** or **Send message**. The town is looked up from OpenStreetMap Nominatim; turn that off in
**Settings → Map → Place names**.

## Tracking

Follow GPS trackers, such as a tracker carried by whoever is running at a relay race, on a map of
their own. Off until it's turned on in **Settings → Tracking**; then **Tracking** appears in the
navigation.

- **How positions arrive:** MeshCore companions don't send their position on a timer (an advert only
  goes out when someone asks for one), so MeshHome asks. On each tracker's interval (1, 2, 5, 10 or
  15 minutes) it sends a telemetry request, and the tracker answers with its latest GPS fix,
  altitude and battery voltage. Positions in its adverts are recorded too. Each request is two short
  packets, repeated by each repeater on the way.
- **Moving between repeaters:** when a request on the stored route goes unanswered, MeshHome forgets
  that route and asks again by flood, and the reply teaches the radio the new route. So a runner who
  moves from your base's coverage into a repeater's costs at most one missed position.
- **Out of range:** with *Ask less often when a tracker doesn't answer* on (the default), a tracker
  that misses 3 requests in a row is asked less often, up to every 5 minutes, until it answers again.
  Turn it off at an event on your own mesh to keep asking at the tracker's interval.
- **The map:** each shown tracker's position with how long ago it was taken, its recent trail
  (Settings → Tracking → *Trail on the map*), your base, and the courses. A card per tracker along
  the bottom shows its name, how fresh its position is and its battery; tap it to centre the map.
  From the list: show or hide each tracker, ask for a position now, give it a label (who's carrying
  it, such as "Leg 7 – Sam"), change its interval or colour, pause it, export its track as GPX, or
  clear its history. Positions are kept for 30 days.
- **Courses:** upload GPX files (tracks, routes and waypoints) to draw them. A colour in the name
  ("Green loop") colours the line. The simulated radio's tracker runs the first visible course, so
  you can rehearse at home.
- **Progress, pace and ETA:** each position is placed on the tracker's course ("Red Loop · 3.2 of
  6.5 mi · 49%"). A tracker's course can be chosen in its **Edit** dialog (do it at each handoff,
  along with the label); *Automatic* uses the visible course its position is on, which can be wrong
  where loops share trail. On a loop's shared start and finish, and on out-and-back sections, the
  match that follows on from the previous position wins. More than about 120 m (400 ft) from the
  line counts as off course. Pace is the distance covered along the course over the last ten
  minutes (with no course, the straight-line distance between fixes, which reads slower on twisty
  trail), and the ETA assumes that pace for the rest of the course.
- **Units:** metric (km, m, °C, hPa) or imperial (mi, ft, °F, inHg), for course distances, pace (per
  km or mile), altitude, temperature and pressure. One setting for all of MeshHome: change it in
  **Settings → Tracking** or with the radio's sensors; the `/env` bot reply uses it too.
- **Battery:** shown as an approximate percentage worked out from the voltage, in red at 20 % or
  less. A tracker that has ever reported over 4.25 V is taken to have a high-voltage cell (full at
  about 4.35 V, like the SenseCAP MeshTracker X1's), which reads lower for the same voltage.
- **How its answer arrived:** from the radio's log of the packets it hears: the repeaters the
  answer passed ("via Car Repeater"), named after contacts whose key starts the same, or "heard
  directly", with the signal (SNR, RSSI) of the last hop. A flood answer lists the repeaters it
  passed; one on the stored route doesn't, so the stored route is shown. Useful for finding where
  coverage drops along the course.
- **Sensors:** when the tracker shares its environment readings (MeshCore app → **Telemetry →
  Environment**), its temperature, barometric pressure and the altitude worked out from it are shown
  too. A tracker's barometer temperature is mostly the heat of whoever carries it. Trackers whose
  firmware doesn't report a barometer simply don't show these.
- **Setting up a tracker:** in the MeshCore app on the tracker, turn GPS on and set **Telemetry →
  Location** to allow this node (flag it in the tracker's contacts) or everyone. The tracker also
  needs your node in its contacts, to read the request. The tracker's status says when it answered
  without a position.

### Meshtastic trackers

Trackers running Meshtastic, such as a SenseCAP MeshTracker X1 (which ships with it), can be
followed too, alongside MeshCore trackers on the same map.

- **How positions arrive:** Meshtastic trackers broadcast their position on their own, on a timer or
  when they've moved (smart position), and their battery and sensor readings on other timers. So
  nothing is polled: MeshHome records every position it hears from a tracked node, with its battery
  as the tracker reports it, its temperature and pressure, and how the packet arrived (hop count,
  SNR and RSSI, and the last relay, named from the node list, such as "via Car Router (2 hops)").
  Positions from while MeshHome wasn't connected come from the node's own list when it reconnects.
  **Ask now** sends a position request, which a tracker answers at most every 3 minutes.
- **Where they're heard** (Settings → Tracking → *Meshtastic trackers*): the **radio HAT on this
  Pi** running Meshtastic (see below), **a Meshtastic node on the network** (its API on TCP port
  4403, e.g. an ESP32 node with Wi-Fi), or a **simulated** node with two trackers running the first
  visible course, to rehearse at home.
- **The team channel:** trackers must share a channel with that node. On Meshtastic's default
  public channel, positions are sent blurred to about 1.5 km and anyone nearby can follow them, so
  MeshHome sets up a **private team channel**: a random key, positions at full precision, and your
  region and modem preset. Its **QR code** (the Meshtastic share link) is scanned into each tracker
  and relay with the Meshtastic app, choosing **Replace**, which also sets their radio to match. On
  an iPhone, scan it with the Camera app and tap the meshtastic.org link; on Android, use the scan
  button in the app's Settings → Channels. On the phone showing MeshHome, **Open in Meshtastic**
  does the same without scanning. The code holds the channel's key.
- **Adding one:** in Tracking, **Add a tracker** → *Meshtastic node*, from the nodes the radio has
  heard (trackers first). Course, label and colour work as for MeshCore trackers; there's no
  interval to set, as the tracker decides how often it broadcasts (set it on the tracker, for
  example every 60–120 s with smart position on).
- **The radio HAT with Meshtastic:** in **Settings → Radio connection → Radio HAT on this Pi**,
  *Switch to Meshtastic* (or `sudo meshhome radio-hat setup --firmware meshtastic`) installs
  meshtasticd, Meshtastic for Linux, from Meshtastic's apt repository and runs it on the HAT instead
  of ZephCore. The HAT then isn't MeshHome's MeshCore radio: MeshHome sets the radio mode to None
  (the time isn't counted as a collection gap) and hears Meshtastic trackers through the HAT,
  under Settings → Tracking. *Switch to MeshCore* brings back ZephCore with its
  identity, contacts and channels, and MeshHome's radio with it. Choose the RAK module (RAK13300 or
  RAK13302) and its IO slot.
- **Not tested on real Meshtastic hardware yet.** It was tested against meshtasticd 2.7.26 (two
  simulated nodes passing positions between them), and the installer in a Debian 13 container.

### Offline map

For a venue without internet: your phone can still reach MeshHome over its Wi-Fi, but not a tile
server. In **Settings → Map → Offline map**, download the area beforehand (around your courses or
your base, or by coordinates), and the maps use the saved tiles. USGS topographic maps and imagery
(public domain, United States only) are offered, or another tile server whose terms allow saving
tiles; OpenStreetMap's own servers don't allow bulk downloads and are refused. Up to 20,000 tiles;
zoom 16 shows trails, and the maps zoom in further by enlarging the saved tiles. The tiles are
stored in the database and aren't included in backups.

## Sensors on the Pi

On a Raspberry Pi install, MeshHome reads sensors attached to the Pi itself (**Settings → Sensors**),
such as the WisBlock modules on a RAK6421 HAT. The radio software on the HAT doesn't read them, so
MeshHome does. Only the Raspberry Pi / Debian install reads sensors: in Docker or Kubernetes there's
no Sensors page and no `/env`.

- **Environment sensor:** a BME680 (e.g. a RAK1906 in sensor slot B, C or D) on I²C bus 1, found
  automatically at address 0x76 or 0x77. Temperature, humidity, pressure and air quality (gas
  resistance) live, and charts over 24 hours, 7 days or 30 days (a reading is stored every 5
  minutes and kept for 30 days).
- **GPS:** a receiver that sends NMEA at 9600 baud on a serial port (e.g. a RAK12501 in slot A, on
  `/dev/serial0` on a Pi 4, `/dev/ttyAMA0` on a Pi 5, chosen automatically): fix, satellites, position and altitude. **Keep my node's location from the GPS**
  sets the radio's location when you've moved further than the distance you choose (at most every
  10 minutes, to spare the radio's flash). Whether adverts share the location is still set in the
  radio's settings.
- **`/env`**: the bot answers with the latest readings, such as `🌡️ 21.4°C · 💧 48% RH · ⏲️ 1013.2
  hPa · 🍃 air 120 kΩ`, to the contacts allowed under Settings → Bot (where **Answer /env** turns it
  off).
- **Units:** metric (°C, hPa, metres) or imperial (°F, inHg, feet), for the page and `/env`.

To use the GPS on a Raspberry Pi, the serial port must be on and its login console off
(`sudo raspi-config` → Interface Options → Serial Port: login shell **No**, hardware **Yes**, then
reboot). I²C must be on too (Interface Options → I2C). The page says what's missing when a sensor
isn't found or isn't sending. Other nodes' telemetry requests are still answered by the radio, so
they don't include these readings.

## Settings and administration

- **Radio connection:** MeshCore TCP, the Raspberry Pi radio HAT (native installs; it can run
  Meshtastic instead, for Meshtastic trackers), Simulated (sample
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
  and shown in Settings. With the radio mode set to None, nothing is collected by choice, so no gap
  stays open.
- **One radio owner:** a PostgreSQL advisory lock makes sure exactly one process owns the radio.
- **Realtime:** an authenticated WebSocket pushes changes; the app falls back to REST resyncs if that
  connection drops.

## API

**Settings → API keys** lets other services and scripts read messages, send them, and follow events
live, using `Authorization: Bearer <key>`. A key can be *read only* or *read & write* and can expire.
Keys are shown once and stored only as a fingerprint. Account, network, update, backup and radio
settings stay limited to the web interface. See the [API guide](API.md); the running app also serves
an interactive reference at `/api/docs`.
