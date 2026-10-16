"""Application-owned radio adapter contract.

These names describe *our* adapter, not upstream library methods. Concrete adapters
(simulated, MeshCore TCP) translate to and from this shape so the rest of the app
never touches the protocol library directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# Default UTF-8 byte budgets for message text. MeshCore does not expose these through the
# client library; they are conservative defaults and MUST be verified on real hardware
# (ASCII, newlines and multibyte emoji at the boundary) before being relied upon.
DM_MAX_BYTES = 150
CHANNEL_MAX_BYTES = 150  # includes the "<node name>: " prefix the firmware adds


class RadioError(Exception):
    """A radio operation failed in a known way."""


class NotSupported(RadioError):
    """The connected firmware or adapter does not support this operation."""


# Well-known MeshCore "Public" channel key (the default channel every companion ships with).
PUBLIC_CHANNEL_KEY = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")


def hashtag_key(name: str) -> bytes:
    """MeshCore derives the key of a "#name" channel from the name itself."""
    import hashlib

    return hashlib.sha256(name.encode("utf-8")).digest()[:16]


def channel_key_kind(name: str, secret: bytes) -> str:
    """Describe a channel key without revealing it."""
    if not any(secret):
        return "none"
    if secret == PUBLIC_CHANNEL_KEY:
        return "public"
    if name.startswith("#") and secret == hashtag_key(name):
        return "hashtag"
    return "private"


@dataclass
class DeviceSnapshot:
    public_key: str
    name: str
    is_simulated: bool
    model: str | None = None
    firmware: str | None = None
    radio: dict[str, Any] = field(default_factory=dict)  # freq/bw/sf/cr/tx power — no secrets
    raw_device_info: dict[str, Any] = field(default_factory=dict)
    lat: float | None = None  # the gateway's own advertised position, if set
    lon: float | None = None


@dataclass
class RadioContact:
    public_key: str
    name: str
    kind: int = 1
    last_advert: int | None = None  # epoch seconds, from the radio
    meta: dict[str, Any] = field(default_factory=dict)
    lat: float | None = None  # position from the node's advert, if it shares one
    lon: float | None = None
    flags: int = 0  # firmware contact flags; bit 0 = favourite
    # Routing: path_len -1 = flood (no known path), 0 = direct, n = n repeater hops given in path_hex.
    path_len: int = -1
    path_hex: str = ""


@dataclass
class RadioChannel:
    slot: int
    name: str
    # Used only to compute a keyed fingerprint; never persisted or returned by the API.
    secret: bytes = b""


@dataclass
class IncomingMessage:
    kind: str  # "dm" | "channel"
    text: str
    txt_type: int = 0
    sender_timestamp: int | None = None
    pubkey_prefix: str | None = None  # DMs: hex prefix of sender key
    channel_slot: int | None = None  # channel messages
    sender_label: str | None = None  # channel messages: label as supplied by the sender
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SendResult:
    ok: bool
    expected_ack: str | None = None
    suggested_timeout_ms: int | None = None
    error: str | None = None


# Remote administration of repeaters / room servers over the mesh.
#   login      arg = password             -> {"ok": bool, "admin": bool, "permissions": int | None}
#   logout                                -> True
#   cli        arg = command text         -> the reply arrives as a CLI message (see supervisor)
#   status / telemetry / acl / neighbours -> decoded response (binary requests; need a login)
#   owner / regions                       -> decoded response (anonymous requests)
REMOTE_KINDS = ("login", "logout", "cli", "status", "telemetry", "acl", "neighbours", "owner", "regions")


@dataclass
class RemoteTicket:
    """A remote request that has been handed to the radio; its reply is awaited separately."""

    kind: str
    public_key: str
    tag: str | None = None  # expected ACK / response tag, when the radio reports one
    timeout: float = 15.0  # seconds the radio suggests waiting for the reply
    handle: Any = field(default=None, repr=False, compare=False)  # adapter-private wait state


AckCallback = Callable[[str], Awaitable[None]]
WaitingCallback = Callable[[], Awaitable[None]]
DisconnectCallback = Callable[[str], Awaitable[None]]


class RadioAdapter(ABC):
    is_simulated: bool = False

    def __init__(self) -> None:
        self.on_ack: AckCallback | None = None
        self.on_messages_waiting: WaitingCallback | None = None
        self.on_disconnect: DisconnectCallback | None = None
        # The radio heard an advert or learned a path: its contact table (names, positions,
        # last heard, paths) may have changed.
        self.on_contacts_changed: WaitingCallback | None = None
        # Every packet the radio hears (raw bytes, SNR dB, RSSI dBm), for message paths.
        self.on_rx_packet: Callable[[bytes, float | None, float | None], Awaitable[None]] | None = None

    @abstractmethod
    async def connect(self) -> None:
        """Open the transport and complete the companion handshake. Raises RadioError."""

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def get_device_snapshot(self) -> DeviceSnapshot: ...

    @abstractmethod
    async def get_contacts(self) -> list[RadioContact]: ...

    @abstractmethod
    async def get_channels(self) -> list[RadioChannel]: ...

    @abstractmethod
    async def fetch_next_message(self) -> IncomingMessage | None:
        """Retrieve one queued message from the radio, or None when the queue is empty."""

    @abstractmethod
    async def send_channel(
        self, slot: int, text: str, timestamp: int, scope: str | None = None
    ) -> SendResult:
        """Send on a channel slot. `scope` is a region name (without "#") applied to this
        message only; None uses the radio's default flood scope."""

    @abstractmethod
    async def send_dm(self, public_key: str, text: str, timestamp: int) -> SendResult: ...

    async def sync_clock(self, epoch_seconds: int) -> None:  # noqa: B027 - optional hook
        """Set the radio clock where supported."""

    # ---- node configuration ---------------------------------------------------------------
    # read_config returns a plain dict (see app.api.radio_config.NodeConfig for the shape).
    # configure applies one named operation; unknown or unsupported ops raise NotSupported.

    def path_hash_size(self) -> int:
        """Bytes per hop in routing paths (firmware path_hash_mode + 1)."""
        return 1

    async def read_config(self) -> dict[str, Any]:
        raise NotSupported("this radio does not support remote configuration")

    async def configure(self, op: str, params: dict[str, Any]) -> dict[str, Any]:
        raise NotSupported("this radio does not support remote configuration")

    # ---- radio backups (app/services/radio_backup.py) -------------------------------------
    # The identity key and the radio's own contact records, as the firmware stores them.

    async def export_private_key(self) -> bytes:
        """The 64-byte private key. NotSupported if the firmware doesn't allow exporting it."""
        raise NotSupported("this radio can't export its identity key")

    async def import_private_key(self, private_key: bytes) -> None:
        raise NotSupported("this radio can't import an identity key")

    async def contact_records(self) -> list[dict[str, Any]]:
        """The radio's contacts as full records (see CONTACT_RECORD_FIELDS), for add_contact_record."""
        raise NotSupported("this radio can't export its contacts")

    async def add_contact_record(self, record: dict[str, Any]) -> None:
        raise NotSupported("this radio can't import contacts")

    # ---- remote administration (repeaters, room servers) -----------------------------------
    # remote_send only hands the request to the radio (quick; done under the command lock).
    # remote_wait waits for the reply over the mesh, which can take many seconds.

    async def remote_send(self, public_key: str, kind: str, arg: str | None = None) -> RemoteTicket:
        raise NotSupported("this radio does not support remote administration")

    async def remote_wait(self, ticket: RemoteTicket, timeout: float) -> Any:
        raise NotSupported("this radio does not support remote administration")


MAX_PATH_BYTES = 64  # firmware out_path buffer
# A contact as the companion protocol stores it (meshcore's contact dict, without its cache fields).
CONTACT_RECORD_FIELDS = (
    "public_key",
    "type",
    "flags",
    "out_path_len",
    "out_path",
    "out_path_hash_mode",
    "adv_name",
    "last_advert",
    "adv_lat",
    "adv_lon",
)


def hops_from_path(path_hex: str, path_len: int, hash_size: int) -> list[str]:
    if path_len <= 0 or not path_hex:
        return []
    step = hash_size * 2
    return [path_hex[i : i + step] for i in range(0, path_len * step, step)]


def advert_position(lat: Any, lon: Any) -> tuple[float | None, float | None]:
    """Normalise an advertised position. MeshCore uses 0,0 for "no location"; reject out-of-range values."""
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None
    if (la == 0 and lo == 0) or not (-90 <= la <= 90 and -180 <= lo <= 180):
        return None, None
    return la, lo


def split_channel_text(text: str) -> tuple[str | None, str]:
    """Channel text arrives as "<sender name>: <body>". Keep the label as supplied.

    Returns (label, body). If there is no recognisable prefix the whole text is the body.
    """
    head, sep, tail = text.partition(": ")
    if sep and 0 < len(head) <= 32 and "\n" not in head:
        return head, tail
    return None, text
