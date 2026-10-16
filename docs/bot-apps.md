# Bot apps

Bot apps add commands to the bot (**Settings → Bot → Apps**). They're published in the
[app catalog](https://github.com/roach0816/MeshHome-apps), which MeshHome reads directly, so apps
are added and updated without a MeshHome release. The catalog's
[FORMAT.md](https://github.com/roach0816/MeshHome-apps/blob/main/FORMAT.md) is the reference for
writing one; this page covers what MeshHome does with them, and setting up apps that a device sends
data to.

## What an app can do

An app is a JSON *recipe*, never code. MeshHome runs every recipe with the same small engine
([`bot_recipes.py`](../backend/app/services/bot_recipes.py)), which only allows these:

- **Settings** the owner fills in, such as a device's address.
- **Sources**, where data comes from:
  - `local`: an HTTP GET to a device on your network. Its address comes from a setting and must be
    private.
  - `internet`: an HTTP GET to one of the internet hosts the recipe lists.
  - `push` (recipe format 2): MeshHome requests nothing. A device on your network sends its readings
    to the app's upload address, and commands use the latest reading.
- **A login** (recipe format 3), for devices that only accept a session: a JSON POST to the same
  local device, whose session token the sources then send. It's the only request that isn't a GET.
- **Commands**: pick values out of the data and word a reply that fits one direct message. Replies
  can only use values from the data, never your settings. The one exception is a command that reads
  a group (below): when it's sent without a group, or with an unknown one, it lists your group
  names, which you chose as labels for use over the air.

Requests never follow redirects. Whatever a mesh user types after a command only picks one of your
groups by name; it never goes into an address, a header or a path.

Before installing, you see an app's commands and everything it contacts or receives data from.
When a new version is published, MeshHome shows what changes, and it only runs after you choose
**Update**.

## Recipe format 3

MeshHome understands recipe formats 1 to 3 (`SCHEMA` in `bot_recipes.py`). A recipe that uses
anything below must declare `"schema": 3`; formats 1 and 2 behave exactly as before. The catalog's
FORMAT.md has full examples.

### Settings

- **`"type": "groups"`**: named groups of IDs, one per line: `garage: cover.garage_left, cover.garage_right`.
  Names are 1–20 letters or digits (stored lowercased) and must be unique. At most 10 groups of 6
  IDs; an ID is 1–100 characters from `A–Z a–z 0–9 _ . : -`. Up to 4000 characters in all. Only a
  command's `group` reads it; a source can't use it as a placeholder.
- **`"type": "select"`**: a dropdown. Either `options` (a list of `{ "value", "label" }`, up to 60)
  or `depends_on` (an earlier select with `options`) plus `options_by` (one list per parent value,
  up to 30 parents). An app has at most 1000 options. A value matches
  `^[A-Za-z0-9][A-Za-z0-9._/-]{0,59}$` and goes into URLs as written (e.g. `football/nfl`), since
  the recipe fixed every possible value. When a parent changes, the dependent select clears.
- **`item`** is reserved as a setting key.
- Settings can't contain control characters or line breaks (except groups), because header values
  are now used as written.

### Sources

- **Headers** take setting values as written, not URL-encoded (format 1 and 2 recipes still
  encode them). A value with a control character is refused when saved and again when sent.
- **`"auth": { "basic": { "username": "{user}", "password": "{password}" } }`** sends HTTP Basic
  credentials. A source can't have both `auth` and an `Authorization` header.
- **`"verify_tls": false`** (local sources only) accepts the device's self-signed certificate. The
  install sheet says so, and an update that turns it on lists it.
- **`"max_kb"`**: the largest response read, 512 by default, up to 2048. A source with
  `"cache_seconds": 0` isn't kept in the cache.
- **`{item}`**: in a source read by group commands (and only those), each ID of the chosen group,
  URL-encoded.
- **`"login": "<name>"`** and **`{login.token}`**: see Logins.

### Logins

```json
"logins": {
  "pihole": {
    "url": "http://{host}/api/auth",
    "body": { "password": "{password}" },
    "token_from": "session.sid",
    "ttl_from": "session.validity",
    "ttl_max": 1800
  }
}
```

- At most one login. It's a POST with a JSON body: a flat object of templates over settings.
- Only local sources can use it, and its URL must use the **same host setting** as each of them.
  It gets the same local-address check, no redirects, and a 64 KB response limit. `timeout` and
  `verify_tls` work as for sources.
- The token is read with the path language at `token_from`. It's reused until
  `min(ttl_from, ttl_max) − 60` seconds have passed (per device and credentials). When a source
  answers 401 or 403, MeshHome signs in again once and retries once.
- `{login.token}` is only valid in the URL and headers of a source that names the login. The token
  is never logged and never reaches a reply.
- The install sheet shows "signs in to the device with your password". An update that adds or
  changes a login lists it.

### Commands

- **`"sources": ["team", "schedule"]`** (up to 3) instead of `"source"`: all are read together. A
  value names its source with `"source"` (default: the first). A source that fails only leaves its
  values out; the reply is `unavailable` only if all fail.
- **`"group": "<groups setting>"`**: the command is sent as `/ha garage`, and `/help` lists it as
  `/ha <group>`. It reads its one `source` once per ID in the group (together, each cached
  separately), words `reply.each` per response in group order, and joins them with `reply.join`,
  dropping from the end to fit. IDs that fail or can't fill `each` are left out: if every request
  fails the reply is `unavailable`, and if none fills `each` it's `empty`. A group command uses
  `each` instead of `parts`.
  - `reply.no_group` (default `Groups: {groups}`) answers a command sent without a group;
    `reply.unknown_group` (default `No group {arg}. Groups: {groups}`) answers an unknown one. These
    two are the only templates that may use `{groups}` (your group names) and `{arg}` (the word
    sent, at most 20 characters).
  - One command is still one reply for the bot's rate limits.

### Values

- **`"format": "count"`**: how many items the list at the path has. If the path's last step is a
  filter, how many items match. A missing list gives no value; an empty one gives 0.
- **`"format": "duration"`**: seconds as `45s`, `1m23s`, `1h23m`, `2d7h`.
- **`"map": { "on": "open", "off": "closed" }`** (text values): shows a value differently, matched
  ignoring case. Mapping to `""` leaves the value out.

### Paths

In addition to `key`, `key[n]` and `key[field=value]` (first match, ignoring case):

- `[-1]`: the last item (negative indexes count from the end).
- Dotted filter fields: `events[competitions.0.status.type.completed=true]`. Each segment is a key
  or a list index; `true`, `false` and numbers compare as lowercase text. Dots inside `[ ]` don't
  split the path.
- `[last:field=value]`: the last match instead of the first.
- `[has:field]`: the first item whose field exists and isn't null or empty; `[last:has:field]` the
  last.

## Apps a device sends data to (push sources)

Many devices have no local API to read from, but can *upload* readings to a server you choose:
weather consoles with a "Customized server" setting, gateways with a custom upload, and anything
that can call a URL or webhook. A recipe with a push source receives those uploads.

### Setting up the device

After you install such an app, its card in Settings → Bot shows:

- **The upload address**, as a full URL and split into server, port and path, because device setup
  pages usually ask for them separately. The path is `/push/` followed by a 32-character code that
  identifies the app.
- **The app's instructions** for where the setting is on its device.
- **When the last reading arrived.** It updates every few seconds, so you can tell straight away
  whether the device is sending.
- **New address** makes a new code. The old one stops working, so enter the new path on the device.

**Try** in the app's settings answers from the latest reading, or says that nothing has arrived. A
reading counts as current for the time the recipe sets (five minutes by default). After that, or if
nothing has arrived since MeshHome started, the command answers with the app's "unavailable" text.

### What MeshHome accepts

- **Local network only.** Uploads are only accepted from private, loopback or link-local addresses.
  If a proxy reports a public address in `X-Forwarded-For`, the upload is refused.
- **Formats:** a query string on a GET, or a form-encoded POST body (`query`), or a JSON POST body
  (`json`), whichever the recipe names.
- **Careless URLs are fine.** Some devices glue their readings straight onto the path, or add a
  stray `?`, `&` or `/` after it. Everything after the 32-character code is read as readings.
- **Limits:** at most 16 KB per upload, and one stored reading per app per second. Extra uploads
  get `ok` and are dropped. Only the latest reading is kept, in memory; devices send again within a
  minute after a restart.
- **Nothing is logged.** Uploads often carry device keys (a station's MAC address, a password).
  MeshHome logs only the app, the sender's address and the outcome, and hides the upload path in
  the web server's access log.

### Plain HTTP

Devices can't use HTTPS, so they send to MeshHome's plain-HTTP address:

- **Plain HTTP installs** (the default): the address you open MeshHome on, port 8080 unless you
  changed it.
- **Raspberry Pi with HTTPS:** nginx's port 80 passes `/push/` through to MeshHome, while everything
  else on port 80 still redirects to HTTPS. This needs the HTTP→HTTPS redirect turned on (the
  default, in Settings → Network & HTTPS). Upload addresses shown over HTTPS use port 80.
- **Kubernetes:** the device must reach MeshHome on plain HTTP from your network. Ingress controllers
  usually redirect plain HTTP to HTTPS once TLS is set up, so either exempt `/push/` from the redirect
  on your ingress, or expose the Service on your LAN (for example a LoadBalancer or NodePort) and
  enter that address on the device.
