# MeshHome API

MeshHome archives the messages of a MeshCore companion radio and lets you send messages
through it. This document describes the HTTP and WebSocket API that other services and scripts
can use with an **API key**. It is meant to be complete enough to build a client from without
further context.

- **Base URL:** wherever the owner's MeshHome is served, for example
  `https://<YOUR_HOST>` or `http://<PI_ADDRESS>:8080`. All paths below start with `/api`.
- **Format:** JSON request and response bodies (`Content-Type: application/json`).
- **Live reference:** the server also publishes an OpenAPI 3 description at `/api/openapi.json`
  and an interactive explorer at `/api/docs` (use its **Authorize** button with your key).
- **Version:** this document matches MeshHome **v1.0.12**. `GET /api/status` reports the
  running version in `app.version`.

## Contents

1. [Authentication](#1-authentication)
2. [Permissions](#2-permissions)
3. [Conventions and errors](#3-conventions-and-errors)
4. [Key concepts](#4-key-concepts)
5. [Endpoints](#5-endpoints)
6. [Realtime events (WebSocket)](#6-realtime-events-websocket)
7. [Recipes](#7-recipes)
8. [Limits and good behaviour](#8-limits-and-good-behaviour)
9. [Server information and signed-in browsers](#9-server-information-and-signed-in-browsers)

---

## 1. Authentication

The owner creates a key in the web interface under **Settings → API keys**, chooses a permission
(read only, or read & write) and, optionally, an expiry. The key looks like this:

```
mh_Q2xU8...  (the prefix "mh_" followed by 43 random characters)
```

Send it on every request in the `Authorization` header:

```http
GET /api/conversations HTTP/1.1
Host: <YOUR_HOST>
Authorization: Bearer mh_Q2xU8...
```

```bash
curl -H "Authorization: Bearer $MESHHOME_API_KEY" https://<YOUR_HOST>/api/status
```

- Keep the key secret (environment variable or secret store). It is shown to the owner only once;
  the server stores only a SHA-256 fingerprint.
- Keys created before the project was renamed to MeshHome start with `mch_` and keep working.
- Requests with an API key need **no** cookies, CSRF token or `X-Requested-With` header. Those are
  only for the browser interface.
- Use HTTPS whenever the server offers it; over plain HTTP the key crosses the network in the clear.
- The API does not send CORS headers, so it is meant for servers, scripts and native apps, not
  for JavaScript running on another website.

Authentication failures:

| Status | `detail` | Meaning |
| --- | --- | --- |
| 401 | `Invalid or expired API key` | Unknown, revoked or expired key. Do not retry with the same key. |
| 401 | `Not signed in` | No `Authorization` header was sent. |
| 403 | `This API key is read-only` | A read-only key tried to change something. |
| 403 | `API keys cannot use this endpoint; use the web interface` | Owner-only endpoint (see below). |
| 429 | `Too many invalid API keys; wait a minute` | More than 20 failed key checks from your address in a minute. |

## 2. Permissions

| Key permission | Allowed |
| --- | --- |
| **Read only** (`read`) | Every `GET` endpoint in this document. |
| **Read & write** (`write`) | Everything `read` allows, plus the `POST`/`PUT`/`PATCH`/`DELETE` endpoints marked **write** below: sending and retrying messages, marking conversations read, favouriting/muting/clearing conversations, and contact actions. |

Some endpoints are **owner-only** and refuse every API key with `403`, whatever its permission:
account and password changes, API key management, software updates, network and HTTPS settings,
the radio connection settings (mode, address, pause/resume), and node configuration changes
(radio parameters, channels, reboot). Their `GET` counterparts that are listed here can still be
read with a key.

## 3. Conventions and errors

- **IDs** are UUIDs (strings). **Times** are ISO 8601 in UTC, e.g. `2026-10-03T21:28:04.123456Z`,
  except a few radio fields documented as Unix epoch seconds.
- **Positions:** every message has an integer `position` that increases across the whole archive
  and is never reused. Use it for paging and for "what is new since…" (see
  [Recipes](#7-recipes)).
- **Errors** use FastAPI's shape. Most are `{"detail": "Human-readable reason"}`. Validation
  errors (`422`) list the problems:

  ```json
  {"detail": [{"loc": ["body", "body"], "msg": "String should have at least 1 character", "type": "string_too_short"}]}
  ```

| Status | Typical cause |
| --- | --- |
| 400 / 422 | The request is malformed or a value is out of range (the message says which). |
| 401 / 403 | See [Authentication](#1-authentication). |
| 404 | The conversation, message or contact does not exist. |
| 409 | The request conflicts with the current state: the radio is offline, a `client_message_id` was reused for a different message, the contact is no longer on the radio, and similar. Read `detail`. |
| 429 | Rate limited; wait and retry. |
| 502 | The radio rejected or did not answer a radio operation. |

## 4. Key concepts

### Conversations

A conversation is either a **channel** (`kind: "channel"`, a group chat on one of the radio's
channel slots) or a **direct message** thread with one contact (`kind: "dm"`).

- Channel messages carry the sender's self-chosen name in `sender_label`. Names are **not**
  verified identities.
- A DM conversation with `contact_id: null` is from a sender the radio could not match to a single
  known contact. You can read it, but replies are refused (`409`).
- `archived: true` means the channel has been removed from (or replaced on) the radio. Its
  history is kept, but you cannot send to it.
- `blocked: true` means the owner blocked this contact. Their new messages are hidden.
- `is_simulated: true` marks data created by the built-in simulated radio, which is used before
  real hardware is connected. Do not treat it as real traffic.
- `max_bytes` is the largest message body you may send in that conversation, measured in **UTF-8
  bytes** (not characters). DMs allow 150 bytes. Channels allow 150 bytes minus the radio's
  name plus 2, because the radio prefixes channel messages with `"<radio name>: "`. Emoji use
  4 bytes each.

### Messages and delivery states

`direction` is `"in"` (received) or `"out"` (sent from MeshHome). Outgoing messages move
through these `state` values:

| `state` | Meaning |
| --- | --- |
| `queued` | Accepted by MeshHome and waiting for the radio. |
| `sending` | Being handed to the radio now. |
| `accepted` | The radio transmitted it. **Final for channel messages**: channels have no delivery receipts. For a DM this means "sent, waiting for acknowledgement". |
| `acknowledged` | DM only: the recipient's radio returned an acknowledgement. This is a delivery receipt, not a read receipt. The web interface shows it as **Delivered**, like the MeshCore apps. |
| `no_ack` | DM only: no acknowledgement arrived in time. It may or may not have been received. Shown as *Not confirmed delivered*. |
| `uncertain` | Sending was interrupted (e.g. a restart). It may or may not have been transmitted. |
| `failed` | The radio refused it; `error` says why. |
| `expired` | It could not be sent within 60 seconds (e.g. the radio was busy or offline). |

Incoming messages have `state: "received"`. `failed`, `uncertain`, `no_ack` and `expired`
messages can be retried. MeshHome never resends anything automatically.

### Idempotent sending

Every send carries a `client_message_id` that **you** generate: 8–64 characters from
`A–Z a–z 0–9 _ -`. A random UUID without dashes works well. If a send times out or your process
restarts, repeat the request with the **same** id and body. You get the original message back
instead of a duplicate transmission. Reusing an id for a different body or conversation returns
`409`.

### Read state

The archive tracks how far the owner has read in each conversation (`read_position`), shared with
the web interface. `unread` counts incoming messages after it. Moving it forward with the API
marks messages as read for the owner as well; only do that if your service is acting for them.

## 5. Endpoints

Legend: **read** = any key, **write** = read & write key.

### Status and device

#### `GET /api/status` — read

Overall health: app version, database, radio connection, recent collection gaps.

```json
{
  "app": {"version": "0.7.1", "radio_enabled_env": true},
  "database": {"ok": true, "messages": 1234},
  "radio": {
    "state": "connected",
    "detail": "",
    "mode": "tcp",
    "is_simulated": false,
    "radio_id": "6a1e…",
    "radio_name": "Home",
    "connected_since": 1790000000.5,
    "last_interaction_at": 1790000123.0,
    "last_error": null,
    "next_retry_at": null,
    "reconnects": 0,
    "received": 52,
    "sent": 7,
    "storage_warning": null,
    "extra": {}
  },
  "radio_config": {"mode": "tcp", "host": "<RADIO_ADDRESS>", "port": 5000, "paused": false},
  "realtime_clients": 1,
  "gaps": [{"started_at": "…", "ended_at": "…", "reason": "…", "open": false}],
  "server_time": 1790000200.1
}
```

`radio.state` is one of `connected`, `connecting`, `backoff` (waiting to reconnect),
`paused`, `disabled`, `not_configured`, `lock_unavailable` (another instance owns the radio) or
`starting`. You can only send while it is `connected`. `gaps` lists periods when messages could
not be collected (radio offline, paused, restarts).

#### `GET /api/device` — read

The radio's identity and channels (never channel keys).

```json
{
  "radio": {
    "id": "6a1e…", "name": "Home", "public_key": "a1b2…(64 hex)", "is_simulated": false,
    "device_info": {"model": "…", "fw ver": "…"}, "rf": {"freq": 910.525, "bw": 62.5, "sf": 7, "cr": 5},
    "last_connected_at": "…", "live": true
  },
  "channels": [{"slot": 0, "name": "Public", "generation": 1, "active": true}],
  "contacts": 42
}
```

`radio` is `null` when no radio has ever connected.

### Conversations

#### `GET /api/conversations` — read

All conversations, most recent activity first.

```json
[
  {
    "id": "0b7c…",
    "kind": "channel",
    "title": "Public",
    "favorite": false,
    "muted": false,
    "sound": null,
    "blocked": false,
    "last_message_at": "2026-10-03T21:10:00Z",
    "last_position": 1234,
    "read_position": 1230,
    "unread": 4,
    "is_simulated": false,
    "archived": false,
    "channel_slot": 0,
    "contact_id": null,
    "contact_public_key": null,
    "peer_prefix": null,
    "max_bytes": 144,
    "preview": {"body": "Anyone copy?", "direction": "in", "sender_label": "Hilltop", "state": "received", "created_at": "…"},
    "sign_messages": false,
    "contacts_only": false
  }
]
```

For DMs, `contact_id` and `contact_public_key` identify the contact and `channel_slot` is `null`.
`peer_prefix` is the sender's public-key prefix when a DM sender could not be matched to a contact.

#### `GET /api/conversations/{conversation_id}` — read

One conversation, same shape as an item above. `404` if unknown.

#### `GET /api/conversations/{conversation_id}/info` — read

Details and statistics:

```json
{
  "conversation": { "...": "same shape as above" },
  "created_at": "…",
  "radio_name": "Home",
  "contact": {"id": "…", "name": "Alice", "alias": null, "public_key": "…", "kind": 1, "last_advert_at": "…", "on_radio": true},
  "channel": {"slot": 0, "name": "Public", "generation": 1, "active": true, "flood_scope": null},
  "stats": {"total": 120, "incoming": 100, "outgoing": 20, "first_message_at": "…", "last_message_at": "…"},
  "delete_action": "clear"
}
```

`contact` is set for DMs, `channel` for channels. `flood_scope` is the channel's region scope
(without `#`), or `null` for the radio default. `delete_action` says what `DELETE` would do: `clear`
for an active channel, `delete` otherwise.

#### `PATCH /api/conversations/{conversation_id}` — write

Change the owner's preferences for a conversation. Send only the fields to change.

```json
{"favorite": true, "muted": false, "sound": "default"}
```

`sound` is `"on"`, `"off"` or `"default"` (follow the global notification setting). Returns `204`.

Channels also take the [signed message](features.md#signed-messages) settings:

- `sign_messages`: sign what this radio sends in the channel. Signatures take 75 bytes, so the
  conversation's `max_bytes` drops by 75 while it's on.
- `contacts_only`: show only messages signed by one of the radio's contacts. The rest are hidden
  (as blocked senders' are: not returned, never unread, no notifications) and come back when the
  sender becomes a contact or the setting is turned off.

Either on a DM is `422`.

For a DM with a known contact, `favorite` is the **contact's** favourite flag on the radio (the
same as `POST /api/contacts/{id}/favorite`), so changing it needs the radio connected (`409` if
not, `502` if the radio refuses). Channels and unmatched DM senders keep an app-only favourite.

#### `PUT /api/conversations/{conversation_id}/read-position` — write

Mark messages as read up to and including `position`. It only ever moves forward; a smaller value
is ignored. Values past the end are clamped.

```json
{"position": 1234}
```

Returns `204`.

#### `DELETE /api/conversations/{conversation_id}` — write

Deletes history **from this archive only**. Nothing is transmitted and the radio is unchanged. An
active channel keeps its (now empty) conversation; anything else is removed. A DM reappears if
the contact writes again.

```json
{"action": "cleared", "messages_removed": 120}
```

### Messages

#### `GET /api/conversations/{conversation_id}/messages` — read

A page of messages, **oldest first** within the page.

| Query | Default | Meaning |
| --- | --- | --- |
| `limit` | 50 | 1–200 messages. |
| `before` | — | Only messages with `position < before` (page backwards through history). |
| `after` | — | Only messages with `position > after`, oldest first (fetch what is new). |

Without `before`/`after` you get the newest `limit` messages.

```json
{
  "messages": [
    {
      "id": "5f0e…",
      "position": 1234,
      "conversation_id": "0b7c…",
      "direction": "in",
      "sender_label": "Hilltop",
      "sender_key_prefix": null,
      "body": "Anyone copy?",
      "sender_timestamp": 1790000000,
      "created_at": "2026-10-03T21:10:00Z",
      "state": "received",
      "error": null,
      "duplicate_count": 0,
      "is_simulated": false,
      "client_message_id": null,
      "meta": {"SNR": 9.5, "RSSI": -71, "path_len": 2}
    }
  ],
  "has_more": true
}
```

- `sender_timestamp` is the sender's clock in Unix seconds and can be wrong; `created_at` is when
  MeshHome stored the message.
- `sender_key_prefix` is the sender's public-key prefix for DMs; `null` for channels.
- `duplicate_count` counts extra copies of the same message heard via other paths.
- `meta` holds radio details when available (`SNR`, `RSSI`, `path_len` hops). Treat every field
  as optional.
- Messages from blocked contacts are not returned, nor, in a channel set to `contacts_only`,
  messages not signed by a contact.
- `signature` is `null` unless the message is a signed channel message:

  ```json
  {"state": "contact", "key_id": "b3f8ce5b40d1a9e2", "signer_name": "Alice", "signer_key": "b3f8ce5b…"}
  ```

  `state` is `self` (sent by this radio), `contact` (signed by one of the radio's contacts),
  `known` (the signature checks out, but the signer isn't a contact), `unknown` (signed by a key
  the radio hasn't heard yet, so it can't be checked; it is checked once the key is known), or
  `invalid` (it doesn't match: the text was changed, or it isn't from that key). `key_id` is the
  first 8 bytes of the signer's public key; `signer_key` is the whole key once known.

#### `POST /api/conversations/{conversation_id}/messages` — write

Send a message.

```json
{"client_message_id": "3f9c2a7be1d04c0f9e1b2a3c4d5e6f70", "body": "On my way 👍"}
```

- `body`: 1 or more characters, at most the conversation's `max_bytes` in UTF-8 bytes. Newlines
  are allowed.
- Returns the stored message (`state: "queued"`). Delivery continues in the background; follow it
  with the WebSocket or by polling the message list.
- In a channel with `sign_messages` on, the radio signs the message as it's sent. `"sign": false`
  sends this one unsigned, with the unsigned limit (`max_bytes` + 75): for a contact link, which
  is too long to carry a signature.

Errors to handle:

| Status | When |
| --- | --- |
| 409 | The radio is offline, the conversation's sender is not a known contact, or `client_message_id` was used for a different message. |
| 422 | Empty body, or too many bytes (`detail` gives the size and the limit). |
| 429 | More than 20 sends per minute (shared with the owner's own sending). |

#### `GET /api/messages/{message_id}/info` — read

One message with what is known about its delivery and sender:

```json
{
  "message": { "...": "same shape as above" },
  "conversation_kind": "channel",
  "sender": {"label": "Alice", "key_prefix": null, "contact": {"id": "…", "name": "Alice", "public_key": "…", "kind": 1, "...": "…"}, "match": "name"},
  "received": {"snr": 6.5, "rssi": -88, "route": "flood", "hops": 2, "path_hash_size": 1},
  "paths": [
    {"hops": [{"hash": "eb", "names": ["Hilltop"]}, {"hash": "92", "names": ["Roof"]}], "hash_size": 1, "route": "flood", "snr": 8.75, "rssi": -102}
  ]
}
```

- `sender.contact` is the DM's contact. For a channel message it is the one contact whose
  advertised name equals the label (`match: "name"`; names are not verified). `null` when there
  is none.
- `paths` lists each copy of the message the radio heard, with its route, first repeater first.
  `names` are contacts whose key starts with the hop's hash; there may be none or several. Paths
  are only recorded while MeshHome is connected.
- `received.route` is `"direct"` when the message followed a known route; `hops` is then `null`.

#### `DELETE /api/messages/{message_id}` — write

Removes one message from this archive. Nothing is transmitted and the radio is unchanged. `409`
while an outgoing message is still `queued` or `sending`. Returns `204`.

#### `POST /api/messages/{message_id}/retry` — write

Send a `failed`, `uncertain`, `no_ack` or `expired` outgoing message again.

```json
{"confirm_possible_duplicate": false}
```

For `uncertain` and `no_ack` the first copy may already have arrived, so you must pass
`"confirm_possible_duplicate": true` (otherwise `409`). Returns the message, now `queued`.

### Search and export

#### `GET /api/search?q=…` — read

Case-insensitive substring search over message bodies, newest first.

| Query | Default | Meaning |
| --- | --- | --- |
| `q` | required | 1–200 characters. |
| `conversation_id` | — | Limit to one conversation. |
| `before` | — | Only `position < before` (page through results). |
| `limit` | 50 | 1–200. |

Returns a list of messages (same shape as above) each with an extra `conversation_title`.

#### `GET /api/export` — read

The whole archive as one JSON document
(`{"exported_at", "format": "meshhome-export/1", "conversations": [ {...conversation, "messages": [...]} ]}`).
This can be large; prefer the paged endpoints for regular syncing.

### Contacts

Contacts are the nodes the radio knows about. `kind` is `1` companion (a person), `2` repeater,
`3` room server, `4` sensor.

#### `GET /api/contacts/me` — read

This radio's own contact, for sharing: `{"name": "…", "public_key": "<64 hex>", "kind": 1, "uri":
"meshcore://contact/add?name=…&public_key=…&type=1"}`. The `uri` is MeshCore's standard contact
link (the QR code in the MeshCore app); posting it in a channel shows as a contact card with an
**Add** button in MeshHome and MeshOS. `409` if no radio has been connected yet.

#### `GET /api/contacts` — read

| Query | Default | Meaning |
| --- | --- | --- |
| `q` | — | Search name, alias or public key (up to 100 characters). |
| `kind` | — | Only this node type. |
| `show` | `all` | `all`, `favorites`, `blocked`, or `removed` (no longer on the radio). |
| `sort` | `last_heard` | `last_heard`, `name` or `kind`. |
| `order` | per column | `asc` or `desc`. Defaults to newest first for `last_heard` and A→Z otherwise. Contacts never heard stay last. |
| `favorites_first` | `false` | `true` lists favourites before everything else, each group in the chosen order. |
| `page` | 1 | Page number, starting at 1. |
| `page_size` | 25 | `10`, `25` or `50`. |

```json
{
  "items": [
    {
      "id": "9d2a…", "public_key": "a1b2…(64 hex)", "name": "Alice", "alias": null, "kind": 1,
      "last_advert_at": "…", "on_radio": true, "favorite": false, "blocked": false,
      "is_simulated": false, "conversation_id": "…or null"
    }
  ],
  "total": 42, "page": 1, "page_size": 25
}
```

`alias` is the owner's local nickname; display `alias ?? name`.

#### `GET /api/contacts/{contact_id}` — read

One contact with extra detail: `lat`, `lon` (if advertised), `path_len` (`-1` flood/unknown,
`0` direct, `n` hops), `path_hops` (hex hash per hop), `path_hash_size`, `messages_received`,
`messages_sent`.

#### `GET /api/contacts/lookup?prefix=a1b2` — read

Who a public-key prefix is, for example a hop in a message's path or a repeater's neighbour. A
short prefix can match several contacts; a node whose advert your radio hasn't heard matches none.
`prefix` is 2 to 64 hex characters.

```json
{"prefix": "a1b2", "items": [{"id": "…", "name": "Hilltop", "kind": 2, "lat": 39.78, "lon": -89.65,
  "distance_km": 12.4, "…": "the other contact fields"}]}
```

At most 10, repeaters and room servers first. `distance_km` is from your radio's advertised
position, when both positions are known.

#### `POST /api/contacts/{contact_id}/conversation` — write

Get (or create) the DM conversation with a contact. Use this before sending someone their first
message.

```json
{"conversation_id": "0b7c…"}
```

#### `PATCH /api/contacts/{contact_id}` — write

```json
{"alias": "Alice (neighbour)", "blocked": false}
```

`alias: null` or `""` clears the nickname. Blocking hides the contact's existing and future
messages from the archive views (reversible). Returns the contact.

#### `POST /api/contacts/{contact_id}/favorite` — write

`{"favorite": true}`. Stored on the radio, so the radio must be connected. Returns the contact.

#### Radio contact actions — write

These act on the radio itself; it must be connected and the contact must still be on it
(`on_radio: true`), otherwise `409`.

| Request | Effect |
| --- | --- |
| `POST /api/contacts/refresh` | Re-read the radio's contact list. Returns `{"count": n}`. |
| `POST /api/contacts/{id}/share` | Re-broadcast the contact's advert to nearby nodes. Returns `{"ok": true}`. |
| `GET /api/contacts/{id}/export` | A `meshcore://` contact card: `{"uri": "meshcore://…"}` (read). |
| `PUT /api/contacts/{id}/path` | Set a fixed route: `{"hops": ["a1", "b2"]}`; `[]` means direct. `204`. |
| `POST /api/contacts/{id}/reset-path` | Forget the route (back to flood). `204`. |
| `POST /api/radio/trace` | Trace a route of repeaters: `{"hops": ["<key or prefix>", …]}` (1–8, in order from this radio). The probe goes out along them and back the same way. Returns `{"hops": [{"hash": "…", "snr": 6.25, "names": ["Hilltop"]}, …], "final_snr": 9.5, "elapsed_ms": 980}`, each repeater out and back with the SNR it heard; `504` if it didn't come back. |
| `DELETE /api/contacts/{id}` | Remove the contact from the radio. The archive keeps it and its messages. `204`. |
| `POST /api/contacts/import` | Add a contact the radio hasn't heard an advert from: `{"uri": "meshcore://contact/add?name=…&public_key=…&type=1"}` (a [MeshCore contact QR code](https://docs.meshcore.io/qr_codes/)), or `{"public_key": "<64 hex>", "name": "…", "kind": 1}`. Returns `{"contact": {...}, "added": true}`; `added: false` if it was already a contact (left unchanged). Names are at most 31 bytes. |

### Map

#### `GET /api/map` — read

Nodes with an advertised position, and the gateway itself, for drawing a map.

#### `GET /api/places?lat=39.78&lon=-89.65` — read

The place at a position, such as a node's advertised one: `{"name": "Springfield, Illinois, US",
"enabled": true, "source": "OpenStreetMap Nominatim"}`. The server looks it up and caches it.
`name` is `null` when there is no named place there or the lookup failed. With place names turned
off (`place_names: false` in the map settings), nothing is looked up and `enabled` is `false`.

### Settings and radio information (read)

| Request | Returns |
| --- | --- |
| `GET /api/sensors` | Sensors attached to the Pi: `{"available": true, "environment": {"status": "ok", "address": 118, "reading": {"temperature_c": 21.4, "humidity": 48.0, "pressure_hpa": 1013.2, "gas_ohms": 120000, "at": 1792980817}}, "gnss": {"status": "fix", "device": "/dev/serial0", "reading": {"fix": true, "lat": …, "lon": …, "alt_m": …, "satellites": 9, "at": …}, "location_update": null}, "config": {...}}`. Status: `off`, `not_found`, `ok`/`fix`, `no_fix`, `silent` (GPS not sending) or `error`. `available` is false in a container (Docker, Kubernetes): only native installs read sensors (feature `sensors` in `GET /api/meta`), so everything stays `off` and `PUT /api/settings/sensors` returns 409. |
| `GET /api/sensors/history?hours=24` | Stored readings (every 5 minutes, kept 30 days), averaged into at most 300 points: `{"hours": 24, "points": [{"at": "…", "temperature_c": …, "humidity": …, "pressure_hpa": …, "gas_ohms": …}]}`. `hours` is 1–720. |
| `GET /api/settings/notifications` | `{"sound": "off" \| "all" \| "dms"}` (browser notification sound). |
| `GET /api/settings/map` | Map tile URL, attribution, maximum zoom, and `place_names` (look up place names for node positions). |
| `GET /api/settings/app-install` | "Install as an app": `{"enabled": false, "https": true}`. `https` says whether this request reached the server over HTTPS (directly or through a proxy that sends `X-Forwarded-Proto: https`). While `enabled`, `/manifest.webmanifest` serves the web app manifest; otherwise it answers `404`. |
| `GET /api/settings/bot` | The command bot: `{"enabled": false, "allow": "favorites" \| "everyone", "env": true}`. When enabled, the node answers DMs starting with `/`: `/info`, `/ping`, `/help`, `/env` (when `env` is true and an environment sensor is working on a native install; see `GET /api/sensors`), and the commands of installed bot apps. Its replies are ordinary outgoing messages with `meta.bot` set to the command. |
| `GET /api/settings/radio` | Radio connection settings: `mode` (`tcp`, `simulated` or `none`), `host`, `port`, `paused`, `sim_interval_seconds`. |
| `GET /api/radio/config` | The connected node's configuration: identity, LoRa parameters, channels (names and key *types*, never keys), behaviour and telemetry. `409` if the radio is offline. |
| `GET /api/radio/channels/hashtag-key?name=%23hikers` | The key of a public `#hashtag` channel, derived from its name: `{"name", "hex", "base64"}`. |
| `GET /api/system/update` | Installed version and whether an update is available. |
| `GET /api/radio/firmware` | The radio's MeshCore firmware compared with MeshCore's latest companion release: `{"available", "model", "current_version", "latest": {"version", "published_at", "notes_url"}, "up_to_date"}`. `up_to_date` is `null` when unknown; `available: false` (with a `reason`) for the simulated radio or the radio HAT. |
| `GET /api/system/radio-hat` | Raspberry Pi installs: the optional radio HAT (RAK6421 run by ZephCore): `phase` (`absent`, `installing`, `needs_reboot`, `ready`, `stopped`, …), `available`, Pi `model`, `installed_version`, and the service state. |

`PUT /api/settings/notifications` and `PUT /api/settings/map` are **write** and take the same
shape they return.

### Not available to API keys

`/api/auth/*` (except `GET /api/auth/me`, which returns `{"username", "home_name"}`),
`/api/api-keys`, `/api/setup`, `POST`/`PUT` under `/api/system/*`, `PUT /api/settings/radio`,
`PUT /api/settings/bot`, `/api/bot/apps` (bot apps: `GET` the installed apps, the catalog and its `categories` (each app has a `category`), `POST /refresh` to read the catalog again, `POST /{id}` to install with `{"settings": {…}}`, `PATCH /{id}` for `settings` and `enabled`, `POST /{id}/update`, `DELETE /{id}`, `POST /{id}/test` to try a command without sending anything, and `POST /{id}/push-token` for a new upload address; an app with a push source has `push`: its upload `path`, `help`, `max_age` and `received_at`), `/api/web-push` (notifications on the calling browser: `GET` status and this server's VAPID public key, `PUT /api/web-push/subscription` with the browser's `PushSubscription` plus `dms` and `channels`, `DELETE /api/web-push/subscription`, `POST /api/web-push/test`), `PUT /api/settings/app-install` (`{"enabled": true}`; `409` unless the request came over HTTPS; turning it off always works), `/api/radio/pause`, `/api/radio/resume`, `/api/radio/test-connection`,
`/api/radio/simulate-incoming`, `DELETE /api/simulated-data`, `POST /api/system/radio-hat`, and every `PUT`/`POST`/`DELETE`
under `/api/radio/config`, `/api/radio/channels`, `/api/radio/custom-vars` and `/api/radio/actions`,
everything under `/api/remote` (remote administration of repeaters and room servers), and
`/api/backups` and `/api/restore` (encrypted backup and restore).

## 6. Realtime events (WebSocket)

Connect to `/ws` (`wss://` when the server uses HTTPS) with the same `Authorization: Bearer`
header. Any key permission works. Browsers cannot set this header, so this is for server-side
clients.

```python
import asyncio, json, os, websockets  # pip install websockets

async def listen():
    headers = {"Authorization": f"Bearer {os.environ['MESHHOME_API_KEY']}"}
    async with websockets.connect("wss://<YOUR_HOST>/ws", additional_headers=headers) as ws:
        async for raw in ws:
            print(json.loads(raw))

asyncio.run(listen())
```

Every frame is a JSON object with `"v": 1` and a `type`. Events are **hints**: they tell you what
changed so you can fetch it over HTTP. They do not carry message text.

| `type` | Extra fields | Fetch next |
| --- | --- | --- |
| `hello` | `radio_state` | Sent once after connecting. |
| `ping` | — | Heartbeat when idle (every 25 s). Ignore it. |
| `message-created` | `conversation_id`, `message_id`; for received messages also `direction: "in"`, `kind` (`"dm"`/`"channel"`), `suppressed` (from a blocked contact) | `GET …/messages?after=<last position you have>` |
| `delivery-updated` | `message_id` and/or `conversation_id` (either may be missing) | Re-read the affected messages to see the new `state`. |
| `conversations-updated` | — | `GET /api/conversations` |
| `contacts-updated` | — | `GET /api/contacts`. Sent after the radio hears an advert or learns a path (at most every 30 s), and after contact changes. |
| `read-position-updated` | `conversation_id` | That conversation's `read_position` / `unread`. |
| `radio-status-changed` | `state` | `GET /api/status` |
| `settings-updated` | — | The settings you care about. |
| `remote-updated` | `public_key` | Used by the web app's remote administration page; API keys cannot read it. |

A missing, invalid or expired key is refused during the handshake (HTTP `403`). The server
closes an open socket with code `4401` when its key is revoked, and with code `4408` if your
client reads too slowly. Events are not replayed: after any reconnect, re-sync over HTTP from the
last `position` you stored. Reconnect with a backoff (e.g. 1 s doubling to 30 s).

## 7. Recipes

### Read everything new since last time

Store the highest `position` you have processed. Then, for each conversation whose
`last_position` is greater:

```
GET /api/conversations/{id}/messages?after={stored_position}&limit=200
```

Repeat while `has_more` is `true`, then store the new highest `position`. Positions are global, so
one stored number works across all conversations. Without the WebSocket, polling
`GET /api/conversations` every 15–60 seconds is enough. The radio itself delivers a handful
of messages a minute at most.

### Send a DM to a contact by name

```bash
KEY="Authorization: Bearer $MESHHOME_API_KEY"
BASE=https://<YOUR_HOST>

# 1. Find the contact
CONTACT=$(curl -s -H "$KEY" "$BASE/api/contacts?q=Alice&page_size=10" | jq -r '.items[0].id')
# 2. Get its DM conversation
CONV=$(curl -s -X POST -H "$KEY" "$BASE/api/contacts/$CONTACT/conversation" | jq -r .conversation_id)
# 3. Send (the same id makes a retry safe)
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" \
  -d "{\"client_message_id\": \"$(uuidgen | tr -d -)\", \"body\": \"Hello from my script\"}" \
  "$BASE/api/conversations/$CONV/messages"
```

### Post to a channel and wait for the outcome (Python)

```python
import os, time, uuid, requests

BASE = "https://<YOUR_HOST>"
S = requests.Session()
S.headers["Authorization"] = f"Bearer {os.environ['MESHHOME_API_KEY']}"

convs = S.get(f"{BASE}/api/conversations", timeout=10).json()
public = next(c for c in convs if c["kind"] == "channel" and c["title"] == "Public" and not c["archived"])

body = "Weather station: 18°C, wind 12 km/h"
if len(body.encode()) > public["max_bytes"]:
    raise ValueError("too long for this channel")

msg = S.post(
    f"{BASE}/api/conversations/{public['id']}/messages",
    json={"client_message_id": uuid.uuid4().hex, "body": body},
    timeout=10,
)
msg.raise_for_status()
msg = msg.json()

final = {"accepted", "acknowledged", "no_ack", "uncertain", "failed", "expired"}
while msg["state"] not in final:
    time.sleep(2)
    page = S.get(f"{BASE}/api/conversations/{public['id']}/messages", params={"after": msg["position"] - 1, "limit": 1}, timeout=10).json()
    msg = page["messages"][0]
print(msg["state"], msg["error"])
```

For a DM, `accepted` is not final: keep waiting for `acknowledged` or `no_ack` (up to about a
minute).

### Check that the radio is up before sending

`GET /api/status` → `radio.state == "connected"`. If not, sends return `409`. Queue the
message on your side and try again later rather than retrying in a tight loop.

## 8. Limits and good behaviour

- **Sending:** 20 messages per minute in total (your service and the owner share this). LoRa
  airtime is scarce, so send only what people need.
- **Message size:** see `max_bytes`; count UTF-8 bytes.
- **Paging:** at most 200 items per request.
- **Polling:** prefer the WebSocket. If you poll, every 15 seconds or slower is plenty.
- **Retries:** reuse the same `client_message_id`. Never retry `uncertain`/`no_ack` sends
  automatically; a person should decide whether a possible duplicate is acceptable.
- **Single radio:** everything goes through one radio connection. Radio operations (contact
  actions, `GET /api/radio/config`) can take several seconds; use timeouts of at least 30 seconds
  for them.
- **Data is personal:** messages and contact positions belong to people on the mesh. Store only
  what your service needs.

## 9. Server information and signed-in browsers

### Discover the server

`GET /api/meta` needs no sign-in. Call it to check that an address is a MeshHome server and what
it supports:

```json
{
  "product": "meshhome",
  "version": "1.0.12",
  "api_version": 1,
  "install_kind": "native",
  "needs_setup": false,
  "features": ["signed_in_devices", "backup", "remote_admin", "bot", "weather", "bot_apps", "firmware_check",
               "api_keys", "contact_import", "software_updates", "network_settings", "radio_hat",
               "system_backup", "sensors"]
}
```

- `api_version` only goes up for a change that existing clients can't handle. Changes are
  otherwise additive: new endpoints, new optional fields, new `features` names. Ignore fields and
  features you don't know.
- `features` tells you what this server has. The last five exist only on native (Debian /
  Raspberry Pi) installs.
- `needs_setup: true` means the first-run wizard hasn't been completed.

### Signed-in browsers

The owner signs in with a username and password in a browser, which gets session cookies. These
endpoints are [not available to API keys](#not-available-to-api-keys); they're listed for
completeness.

| Method and path | Purpose |
| --- | --- |
| `GET /api/auth/sessions` | Signed-in browsers: `id`, `user_agent`, `created_at`, `last_seen_at`, `expires_at`, `current` |
| `DELETE /api/auth/sessions/{id}` | Sign out one browser; its WebSocket closes at once |
| `DELETE /api/auth/sessions` | Sign out every browser except the caller |

### The API contract

The repository keeps the whole API description in `backend/openapi.json`, which you can generate
a client from. CI fails a change that would break clients built against the last release, unless
the break is deliberate and listed for that release.
