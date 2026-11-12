"""The parts of the Meshtastic client API that tracking needs, as a small protobuf codec.

Written for MeshHome from the published field numbers of Meshtastic's messages (FromRadio,
ToRadio, MeshPacket, Position, Telemetry, User, NodeInfo, Channel, LoRa config, AdminMessage and
ChannelSet). It doesn't include or generate code from Meshtastic's GPL-licensed .proto files.
Only the fields MeshHome reads or writes are listed; others are skipped when decoding.

A schema maps a field number to (name, kind). Kinds: "uint" (varint), "int" (int32 varint,
negative values as 64-bit two's complement), "sint" (zigzag), "bool", "enum", "fixed32",
"sfixed32", "float", "bytes", "string", a nested schema (a message), and ("repeated", kind).
Decoded messages are dicts; a message's raw bytes are kept under "_raw" so a configuration can be
handed back to the device unchanged (e.g. the LoRa settings in a channel QR code).
"""

from __future__ import annotations

import base64
import struct
from typing import Any

Schema = dict[int, tuple[str, Any]]


class ProtoError(ValueError):
    pass


# ---- wire format -------------------------------------------------------------------------------


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(buf):
            raise ProtoError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ProtoError("varint too long")


def _put_varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def fields(buf: bytes) -> list[tuple[int, int, Any]]:
    """(field number, wire type, value) for each field: int for varints, bytes otherwise."""
    out: list[tuple[int, int, Any]] = []
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        num, wt = key >> 3, key & 7
        if num == 0:
            raise ProtoError("field number 0")
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 1:
            v, i = buf[i : i + 8], i + 8
        elif wt == 2:
            n, i = _varint(buf, i)
            v, i = buf[i : i + n], i + n
        elif wt == 5:
            v, i = buf[i : i + 4], i + 4
        else:
            raise ProtoError(f"unsupported wire type {wt}")
        if i > len(buf):
            raise ProtoError("truncated field")
        out.append((num, wt, v))
    return out


def _scalar(kind: str, wt: int, v: Any) -> Any:
    if kind in ("uint", "enum"):
        return v if wt == 0 else None
    if kind == "int":
        if wt != 0:
            return None
        return v - (1 << 64) if v >= 1 << 63 else v
    if kind == "sint":
        return (v >> 1) ^ -(v & 1) if wt == 0 else None
    if kind == "bool":
        return bool(v) if wt == 0 else None
    if kind == "fixed32":
        return struct.unpack("<I", v)[0] if wt == 5 else None
    if kind == "sfixed32":
        return struct.unpack("<i", v)[0] if wt == 5 else None
    if kind == "float":
        return struct.unpack("<f", v)[0] if wt == 5 else None
    if kind == "bytes":
        return bytes(v) if wt == 2 else None
    if kind == "string":
        return bytes(v).decode("utf-8", "replace") if wt == 2 else None
    raise ProtoError(f"unknown kind {kind}")


def decode(buf: bytes, schema: Schema) -> dict[str, Any]:
    msg: dict[str, Any] = {"_raw": bytes(buf)}
    for num, wt, v in fields(buf):
        spec = schema.get(num)
        if spec is None:
            continue
        name, kind = spec
        repeated = isinstance(kind, tuple) and kind[0] == "repeated"
        inner = kind[1] if repeated else kind
        if isinstance(inner, dict):
            if wt != 2:
                continue
            value: Any = decode(v, inner)
        elif repeated and wt == 2 and inner not in ("bytes", "string"):
            # Packed repeated scalars.
            value = []
            j = 0
            while j < len(v):
                if inner in ("fixed32", "sfixed32", "float"):
                    value.append(_scalar(inner, 5, v[j : j + 4]))
                    j += 4
                else:
                    n, j = _varint(v, j)
                    value.append(_scalar(inner, 0, n))
            msg.setdefault(name, []).extend(value)
            continue
        else:
            value = _scalar(inner, wt, v)
            if value is None:
                continue
        if repeated:
            msg.setdefault(name, []).append(value)
        else:
            msg[name] = value
    return msg


def encode(msg: dict[str, Any], schema: Schema) -> bytes:
    by_name = {name: (num, kind) for num, (name, kind) in schema.items()}
    out = bytearray()
    for name, value in msg.items():
        if name == "_raw" or value is None:
            continue
        if name not in by_name:
            raise ProtoError(f"no field {name!r}")
        num, kind = by_name[name]
        repeated = isinstance(kind, tuple) and kind[0] == "repeated"
        inner = kind[1] if repeated else kind
        for v in value if repeated else [value]:
            out += _encode_field(num, inner, v)
    return bytes(out)


def patch(raw: bytes, schema: Schema, changes: dict[str, Any]) -> bytes:
    """`raw` with the named fields replaced, keeping every other field as it was (also ones this
    module doesn't know), so settings can be changed without resetting the rest."""
    by_name = {name: num for num, (name, _) in schema.items()}
    nums = {by_name[k] for k in changes}
    out = bytearray()
    i = 0
    while i < len(raw):
        start = i
        key, i = _varint(raw, i)
        wt = key & 7
        if wt == 0:
            _, i = _varint(raw, i)
        elif wt == 1:
            i += 8
        elif wt == 2:
            n, i = _varint(raw, i)
            i += n
        elif wt == 5:
            i += 4
        else:
            raise ProtoError(f"unsupported wire type {wt}")
        if key >> 3 not in nums:
            out += raw[start:i]
    return bytes(out) + encode(changes, schema)


def _encode_field(num: int, kind: Any, v: Any) -> bytes:
    if isinstance(kind, dict):
        body = v if isinstance(v, bytes | bytearray) else encode(v, kind)
        return _put_varint(num << 3 | 2) + _put_varint(len(body)) + bytes(body)
    if kind in ("uint", "enum", "int"):
        return _put_varint(num << 3) + _put_varint(int(v))
    if kind == "bool":
        return _put_varint(num << 3) + _put_varint(1 if v else 0)
    if kind == "sint":
        n = int(v)
        return _put_varint(num << 3) + _put_varint((n << 1) ^ (n >> 63))
    if kind == "fixed32":
        return _put_varint(num << 3 | 5) + struct.pack("<I", int(v) & 0xFFFFFFFF)
    if kind == "sfixed32":
        return _put_varint(num << 3 | 5) + struct.pack("<i", int(v))
    if kind == "float":
        return _put_varint(num << 3 | 5) + struct.pack("<f", float(v))
    if kind in ("bytes", "string"):
        body = v.encode() if isinstance(v, str) else bytes(v)
        return _put_varint(num << 3 | 2) + _put_varint(len(body)) + body
    raise ProtoError(f"unknown kind {kind}")


# ---- messages ----------------------------------------------------------------------------------

# Data.portnum
TEXT_MESSAGE_APP = 1
POSITION_APP = 3
NODEINFO_APP = 4
ROUTING_APP = 5
ADMIN_APP = 6
TELEMETRY_APP = 67
TRACEROUTE_APP = 70

BROADCAST = 0xFFFFFFFF

POSITION: Schema = {
    1: ("latitude_i", "sfixed32"),
    2: ("longitude_i", "sfixed32"),
    3: ("altitude", "int"),
    4: ("time", "fixed32"),
    5: ("location_source", "enum"),
    7: ("timestamp", "fixed32"),
    10: ("altitude_hae", "sint"),
    12: ("HDOP", "uint"),
    14: ("gps_accuracy", "uint"),
    15: ("ground_speed", "uint"),
    16: ("ground_track", "uint"),
    19: ("sats_in_view", "uint"),
    22: ("seq_number", "uint"),
    23: ("precision_bits", "uint"),
}

USER: Schema = {
    1: ("id", "string"),
    2: ("long_name", "string"),
    3: ("short_name", "string"),
    5: ("hw_model", "enum"),
    6: ("is_licensed", "bool"),
    7: ("role", "enum"),
    8: ("public_key", "bytes"),
}

DEVICE_METRICS: Schema = {
    1: ("battery_level", "uint"),
    2: ("voltage", "float"),
    3: ("channel_utilization", "float"),
    4: ("air_util_tx", "float"),
    5: ("uptime_seconds", "uint"),
}

ENVIRONMENT_METRICS: Schema = {
    1: ("temperature", "float"),
    2: ("relative_humidity", "float"),
    3: ("barometric_pressure", "float"),
    5: ("voltage", "float"),
}

TELEMETRY: Schema = {
    1: ("time", "fixed32"),
    2: ("device_metrics", DEVICE_METRICS),
    3: ("environment_metrics", ENVIRONMENT_METRICS),
}

DATA: Schema = {
    1: ("portnum", "enum"),
    2: ("payload", "bytes"),
    3: ("want_response", "bool"),
    4: ("dest", "fixed32"),
    5: ("source", "fixed32"),
    6: ("request_id", "fixed32"),
    7: ("reply_id", "fixed32"),
    9: ("bitfield", "uint"),
}

MESH_PACKET: Schema = {
    1: ("from", "fixed32"),
    2: ("to", "fixed32"),
    3: ("channel", "uint"),
    4: ("decoded", DATA),
    5: ("encrypted", "bytes"),
    6: ("id", "fixed32"),
    7: ("rx_time", "fixed32"),
    8: ("rx_snr", "float"),
    9: ("hop_limit", "uint"),
    10: ("want_ack", "bool"),
    11: ("priority", "enum"),
    12: ("rx_rssi", "int"),
    14: ("via_mqtt", "bool"),
    15: ("hop_start", "uint"),
    16: ("public_key", "bytes"),
    17: ("pki_encrypted", "bool"),
    18: ("next_hop", "uint"),
    19: ("relay_node", "uint"),
}

MY_NODE_INFO: Schema = {
    1: ("my_node_num", "uint"),
    8: ("reboot_count", "uint"),
    11: ("min_app_version", "uint"),
    13: ("pio_env", "string"),
    15: ("nodedb_count", "uint"),
}

NODE_INFO: Schema = {
    1: ("num", "uint"),
    2: ("user", USER),
    3: ("position", POSITION),
    4: ("snr", "float"),
    5: ("last_heard", "fixed32"),
    6: ("device_metrics", DEVICE_METRICS),
    7: ("channel", "uint"),
    8: ("via_mqtt", "bool"),
    9: ("hops_away", "uint"),
    10: ("is_favorite", "bool"),
}

MODULE_SETTINGS: Schema = {1: ("position_precision", "uint"), 2: ("is_client_muted", "bool")}

CHANNEL_SETTINGS: Schema = {
    2: ("psk", "bytes"),
    3: ("name", "string"),
    4: ("id", "fixed32"),
    5: ("uplink_enabled", "bool"),
    6: ("downlink_enabled", "bool"),
    7: ("module_settings", MODULE_SETTINGS),
}

# Channel.role
DISABLED, PRIMARY, SECONDARY = 0, 1, 2

CHANNEL: Schema = {1: ("index", "int"), 2: ("settings", CHANNEL_SETTINGS), 3: ("role", "enum")}

LORA_CONFIG: Schema = {
    1: ("use_preset", "bool"),
    2: ("modem_preset", "enum"),
    3: ("bandwidth", "uint"),
    4: ("spread_factor", "uint"),
    5: ("coding_rate", "uint"),
    6: ("frequency_offset", "float"),
    7: ("region", "enum"),
    8: ("hop_limit", "uint"),
    9: ("tx_enabled", "bool"),
    10: ("tx_power", "int"),
    11: ("channel_num", "uint"),
    12: ("override_duty_cycle", "bool"),
    13: ("sx126x_rx_boosted_gain", "bool"),
    14: ("override_frequency", "float"),
}

DEVICE_CONFIG: Schema = {1: ("role", "enum")}

CONFIG: Schema = {1: ("device", DEVICE_CONFIG), 6: ("lora", LORA_CONFIG)}

DEVICE_METADATA: Schema = {
    1: ("firmware_version", "string"),
    7: ("role", "enum"),
    9: ("hw_model", "enum"),
    11: ("hasPKC", "bool"),
}

FROM_RADIO: Schema = {
    1: ("id", "uint"),
    2: ("packet", MESH_PACKET),
    3: ("my_info", MY_NODE_INFO),
    4: ("node_info", NODE_INFO),
    5: ("config", CONFIG),
    7: ("config_complete_id", "uint"),
    8: ("rebooted", "bool"),
    10: ("channel", CHANNEL),
    13: ("metadata", DEVICE_METADATA),
}

HEARTBEAT: Schema = {}

TO_RADIO: Schema = {
    1: ("packet", MESH_PACKET),
    3: ("want_config_id", "uint"),
    4: ("disconnect", "bool"),
    7: ("heartbeat", HEARTBEAT),
}

# AdminMessage.ConfigType
LORA_CONFIG_TYPE = 5
SESSIONKEY_CONFIG_TYPE = 8

ADMIN: Schema = {
    1: ("get_channel_request", "uint"),
    2: ("get_channel_response", CHANNEL),
    3: ("get_owner_request", "bool"),
    4: ("get_owner_response", USER),
    5: ("get_config_request", "enum"),
    6: ("get_config_response", CONFIG),
    32: ("set_owner", USER),
    33: ("set_channel", CHANNEL),
    34: ("set_config", CONFIG),
    64: ("begin_edit_settings", "bool"),
    65: ("commit_edit_settings", "bool"),
    101: ("session_passkey", "bytes"),
}

CHANNEL_SET: Schema = {1: ("settings", ("repeated", CHANNEL_SETTINGS)), 2: ("lora_config", LORA_CONFIG)}


# ---- names -------------------------------------------------------------------------------------

REGIONS = {
    0: "UNSET",
    1: "US",
    2: "EU_433",
    3: "EU_868",
    4: "CN",
    5: "JP",
    6: "ANZ",
    7: "KR",
    8: "TW",
    9: "RU",
    10: "IN",
    11: "NZ_865",
    12: "TH",
    13: "LORA_24",
    14: "UA_433",
    15: "UA_868",
    16: "MY_433",
    17: "MY_919",
    18: "SG_923",
    19: "PH_433",
    20: "PH_868",
    21: "PH_915",
    22: "ANZ_433",
    23: "KZ_433",
    24: "KZ_863",
    25: "NP_865",
    26: "BR_902",
}

PRESETS = {
    0: "LONG_FAST",
    1: "LONG_SLOW",
    3: "MEDIUM_SLOW",
    4: "MEDIUM_FAST",
    5: "SHORT_SLOW",
    6: "SHORT_FAST",
    7: "LONG_MODERATE",
    8: "SHORT_TURBO",
}

ROLES = {
    0: "CLIENT",
    1: "CLIENT_MUTE",
    2: "ROUTER",
    3: "ROUTER_CLIENT",
    4: "REPEATER",
    5: "TRACKER",
    6: "SENSOR",
    7: "TAK",
    8: "CLIENT_HIDDEN",
    9: "LOST_AND_FOUND",
    10: "TAK_TRACKER",
    11: "ROUTER_LATE",
    12: "CLIENT_BASE",
}
# Roles that relay for others: named first when a packet's relay byte matches several nodes.
RELAY_ROLES = {"ROUTER", "ROUTER_CLIENT", "REPEATER", "ROUTER_LATE", "CLIENT_BASE"}

# The default channel key ("AQ==", key 1): everyone running Meshtastic can read that channel.
DEFAULT_PSK = b"\x01"


def node_id(num: int) -> str:
    return f"!{num & 0xFFFFFFFF:08x}"


def position_fix(p: dict[str, Any]) -> dict[str, float | None] | None:
    """{"lat", "lon", "alt_m"} from a Position, or None when it has no fix."""
    lat_i, lon_i = p.get("latitude_i"), p.get("longitude_i")
    if not lat_i and not lon_i:
        return None
    lat, lon = (lat_i or 0) * 1e-7, (lon_i or 0) * 1e-7
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    alt = p.get("altitude")
    if alt is None:
        alt = p.get("altitude_hae")
    return {"lat": round(lat, 7), "lon": round(lon, 7), "alt_m": float(alt) if alt is not None else None}


def channel_url(settings: list[dict[str, Any]], lora_raw: bytes | None, add: bool = False) -> str:
    """The share link a Meshtastic app turns into these channels (and LoRa settings): what a
    channel QR code holds. `lora_raw` is the device's LoRa config, unchanged."""
    body = encode({"settings": settings, "lora_config": lora_raw}, CHANNEL_SET)
    b64 = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"https://meshtastic.org/e/{'?add=true' if add else ''}#{b64}"


def frame(payload: bytes) -> bytes:
    """The stream framing of the TCP and serial API: 0x94 0xC3, length (big-endian), protobuf."""
    if len(payload) > 512:
        raise ProtoError("ToRadio too large")
    return b"\x94\xc3" + struct.pack(">H", len(payload)) + payload
