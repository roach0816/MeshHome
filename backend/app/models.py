"""Database schema. Field names are application design, not MeshCore wire fields."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        uuid.UUID: UUID(as_uuid=True),
    }


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    password_changed_at: Mapped[datetime] = mapped_column(default=utcnow)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # SHA-256 of the cookie token; the raw token is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    user_agent: Mapped[str | None] = mapped_column(String(256))
    # "web" (NULL before 0.8.0). "ios" and "android" were the mobile apps' sign-ins (removed in
    # 0.12.0); such sessions are refused and not listed.
    client: Mapped[str | None] = mapped_column(String(16))
    # The phone's name, set by the mobile apps; unused since 0.12.0.
    device_name: Mapped[str | None] = mapped_column(String(64))


# The table "push_devices" (phones signed up for push notifications through the mobile apps,
# removed in 0.12.0) is left in the database unused, so an older release still runs after a
# rollback.


class WebPushSubscription(Base):
    """A browser that receives web push notifications (app/services/web_push.py).

    Tied to the browser's sign-in: signing it out (or revoking it) removes the subscription. The
    endpoint is the browser's push service URL; p256dh and auth are the browser's keys that each
    notification is encrypted to (RFC 8291), so the push service only sees ciphertext.
    """

    __tablename__ = "web_push_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(128))
    auth: Mapped[str] = mapped_column(String(64))
    dms: Mapped[bool] = mapped_column(Boolean, default=True)
    channels: Mapped[bool] = mapped_column(Boolean, default=False)
    user_agent: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_sent_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(String(200))


class ApiKey(Base):
    """A key for other services and scripts (Authorization: Bearer mh_...)."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    # The first characters of the key, shown so the owner can tell keys apart.
    prefix: Mapped[str] = mapped_column(String(16))
    # SHA-256 of the key; the key itself is shown once and never stored.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scope: Mapped[str] = mapped_column(String(16))  # "read" | "write"
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Radio(Base):
    __tablename__ = "radios"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    public_key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    # "meshcore" | "meshtastic": the app shows the contacts, channels and messages of the radios
    # running the technology in use (app_settings.effective_tech).
    tech: Mapped[str] = mapped_column(String(16), default="meshcore", server_default="meshcore")
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    device_info: Mapped[dict[str, Any]] = mapped_column(default=dict)
    self_info: Mapped[dict[str, Any]] = mapped_column(default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_connected_at: Mapped[datetime | None]


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("radio_id", "public_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    radio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("radios.id", ondelete="CASCADE"))
    public_key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64), default="")
    alias: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[int] = mapped_column(Integer, default=1)  # MeshCore advert type (1 = chat)
    last_advert_at: Mapped[datetime | None]
    # Advertised position (WGS84). Null when the node does not share one.
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    on_radio: Mapped[bool] = mapped_column(Boolean, default=True)
    # Mirrors the firmware's favourite flag (contact flags bit 0): protected from being
    # overwritten when the radio's contact table is full.
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # App-side block: the radio cannot block, so messages are still received and archived but
    # suppressed (hidden, never unread, never notify). Reversible.
    blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Preferred repeater (services/preferred_route.py): the remembered route through it ("hops",
    # "source", "learned_at"), when it last failed ("failed_at"), and "off" if not used for this contact.
    routing: Mapped[dict[str, Any] | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("radio_id", "slot", "generation"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    radio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("radios.id", ondelete="CASCADE"))
    slot: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(64))
    # Keyed fingerprint of name + secret; detects slot reuse without storing the secret.
    fingerprint: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Region scope (name without "#") stamped on messages sent to this channel; None = radio default.
    # Kept app-side, like the MeshCore app does: the firmware has no per-channel scope.
    flood_scope: Mapped[str | None] = mapped_column(String(31))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("radio_id", "peer_prefix"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    radio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("radios.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # "channel" | "dm"
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), unique=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), unique=True
    )
    # For DMs from senders we cannot resolve to exactly one full key.
    peer_prefix: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(64))
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    muted: Mapped[bool] = mapped_column(Boolean, default=False)  # unused; superseded by `sound`
    # Per-conversation sound override: None follows the global setting, "on"/"off" override it.
    sound: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_message_at: Mapped[datetime | None]
    last_position: Mapped[int] = mapped_column(BigInteger, default=0)
    # Signed channel messages (services/signed_messages.py): sign what this radio sends here, and
    # show only messages signed by one of the radio's contacts (the rest are suppressed).
    sign_messages: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    contacts_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_position", "conversation_id", "position"),
        Index("ix_messages_fingerprint", "fingerprint"),
        Index("ix_messages_state", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Server-assigned, strictly increasing collection order (assigned under a DB lock).
    position: Mapped[int] = mapped_column(BigInteger, unique=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    direction: Mapped[str] = mapped_column(String(8))  # "in" | "out"
    sender_label: Mapped[str | None] = mapped_column(String(64))
    sender_key_prefix: Mapped[str | None] = mapped_column(String(64))
    body: Mapped[str] = mapped_column(Text)
    txt_type: Mapped[int] = mapped_column(Integer, default=0)
    sender_timestamp: Mapped[int | None] = mapped_column(BigInteger)  # remote/radio clock, epoch seconds
    created_at: Mapped[datetime] = mapped_column(default=utcnow)  # server receive / request time
    state: Mapped[str] = mapped_column(String(16))  # see app.services.states
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    client_message_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    expected_ack: Mapped[str | None] = mapped_column(String(16), index=True)
    ack_deadline: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    # True while the sender is blocked; hidden from history, unread counts, search and sounds.
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # A signed channel message whose signature checked out: the signer's public key (hex).
    signed_by: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SendAttempt(Base):
    __tablename__ = "send_attempts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    result: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text)
    # Direct messages: the attempt number sent to the radio (0 for the first of a round of automatic
    # retries; a manual retry starts a new round), its ACK code, and whether it was flooded.
    round_attempt: Mapped[int | None] = mapped_column(Integer)
    expected_ack: Mapped[str | None] = mapped_column(String(16), index=True)
    flood: Mapped[bool | None] = mapped_column(Boolean)
    # The preferred repeater's public key, when this attempt was sent on the route through it.
    via_repeater: Mapped[str | None] = mapped_column(String(64))


class ReadPosition(Base):
    __tablename__ = "read_positions"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class CollectionGap(Base):
    __tablename__ = "collection_gaps"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    radio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("radios.id", ondelete="SET NULL"))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    ended_at: Mapped[datetime | None]
    reason: Mapped[str] = mapped_column(String(128))


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)


class LogEntry(Base):
    """MeshHome's log history (services/app_log.py), for Settings → Health & logs: warnings and
    errors, and events (the radio connecting and going offline, the app starting, settings changed).
    The same line repeated is one entry, with its count and the first and last time."""

    __tablename__ = "log_entries"
    __table_args__ = (Index("ix_log_entries_last_at", "last_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    first_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_at: Mapped[datetime] = mapped_column(default=utcnow)
    count: Mapped[int] = mapped_column(Integer, default=1)
    level: Mapped[str] = mapped_column(String(8))  # INFO | WARNING | ERROR | CRITICAL
    component: Mapped[str] = mapped_column(String(24))  # radio, database, tracking, ... (app_log.COMPONENTS)
    event: Mapped[str | None] = mapped_column(String(24))  # set for events (kept even at INFO)
    logger: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)  # an error's traceback


class SensorReading(Base):
    """A reading from the sensors attached to this Pi (services/sensors.py), every few minutes, for
    history. Any value can be missing (a sensor not fitted, or no GPS fix)."""

    __tablename__ = "sensor_readings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    temperature_c: Mapped[float | None] = mapped_column(Float)
    humidity: Mapped[float | None] = mapped_column(Float)
    pressure_hpa: Mapped[float | None] = mapped_column(Float)
    gas_ohms: Mapped[float | None] = mapped_column(Float)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    alt_m: Mapped[float | None] = mapped_column(Float)
    satellites: Mapped[int | None] = mapped_column(Integer)


class TrackedDevice(Base):
    """A contact whose position MeshHome asks for on a schedule (services/tracking.py), e.g. a GPS
    tracker carried by a runner. Positions come from telemetry replies and from its adverts."""

    __tablename__ = "tracked_devices"
    __table_args__ = (
        CheckConstraint("num_nonnulls(contact_id, mesh_node_num) = 1", name="ck_tracked_devices_one_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # One of the two: a MeshCore contact (asked for its position), or a Meshtastic node (its
    # position broadcasts are recorded; services/meshtastic.py).
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), unique=True
    )
    mesh_node_num: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("mesh_nodes.num", ondelete="CASCADE"), unique=True
    )
    # Shown instead of the contact's name, e.g. who carries it now ("Leg 7 – Sam").
    label: Mapped[str | None] = mapped_column(String(64))
    color: Mapped[str] = mapped_column(String(7), default="#e11d48")
    interval_s: Mapped[int] = mapped_column(Integer, default=60)
    polling: Mapped[bool] = mapped_column(Boolean, default=True)  # ask for its position on schedule
    show: Mapped[bool] = mapped_column(Boolean, default=True)  # on the tracking map
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_poll_at: Mapped[datetime | None]
    last_reply_at: Mapped[datetime | None]
    # ok | no_position | no_answer | error | new
    status: Mapped[str] = mapped_column(String(16), default="new")
    status_detail: Mapped[str | None] = mapped_column(String(300))
    failures: Mapped[int] = mapped_column(Integer, default=0)  # unanswered polls in a row
    battery_v: Mapped[float | None] = mapped_column(Float)
    # The highest battery voltage it reported: above 4.25 V, its battery is a high-voltage one
    # (full at 4.35 V or so, like the SenseCAP X1's), which changes how voltage maps to percent.
    battery_max_v: Mapped[float | None] = mapped_column(Float)
    # The course it's on, for progress and ETA (services/progress.py); None: the nearest one.
    course_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("courses.id", ondelete="SET NULL"))
    # The contact's advert time that was last recorded as a position, so each advert counts once.
    advert_seen_at: Mapped[datetime | None]
    # Meshtastic: the battery level the tracker reports (0-100; 101: on external power).
    battery_reported_pct: Mapped[int | None] = mapped_column(Integer)


class TrackPosition(Base):
    __tablename__ = "track_positions"
    __table_args__ = (Index("ix_track_positions_device_at", "device_id", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tracked_devices.id", ondelete="CASCADE"))
    at: Mapped[datetime] = mapped_column(default=utcnow)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    alt_m: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16))  # "telemetry" | "advert" | "meshtastic"
    battery_v: Mapped[float | None] = mapped_column(Float)
    # How the reply reached this radio (telemetry only, when the radio logged the packet): signal
    # of the last hop, "direct" or "flood", and the repeaters' hash prefixes, tracker's end first.
    # Meshtastic: "mesh", one entry per hop; only the last relay is known (the low byte of its node
    # number, "?" for the others).
    snr: Mapped[float | None] = mapped_column(Float)
    rssi: Mapped[float | None] = mapped_column(Float)
    route: Mapped[str | None] = mapped_column(String(8))
    hops: Mapped[list[Any] | None] = mapped_column(JSONB)
    # Its sensors, when it shares them (MeshCore app → Telemetry → Environment).
    temp_c: Mapped[float | None] = mapped_column(Float)
    pressure_hpa: Mapped[float | None] = mapped_column(Float)
    baro_alt_m: Mapped[float | None] = mapped_column(Float)


class MeshNode(Base):
    """A node the Meshtastic radio has heard (its node list), for naming and adding trackers."""

    __tablename__ = "mesh_nodes"

    num: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    node_id: Mapped[str] = mapped_column(String(9))  # "!a1b2c3d4"
    long_name: Mapped[str | None] = mapped_column(String(40))
    short_name: Mapped[str | None] = mapped_column(String(8))
    hw_model: Mapped[int | None] = mapped_column(Integer)
    role: Mapped[str | None] = mapped_column(String(16))  # CLIENT, ROUTER, TRACKER, ...
    public_key: Mapped[str | None] = mapped_column(String(64))
    last_heard_at: Mapped[datetime | None]
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    alt_m: Mapped[float | None] = mapped_column(Float)
    position_at: Mapped[datetime | None]
    battery_level: Mapped[int | None] = mapped_column(Integer)
    voltage: Mapped[float | None] = mapped_column(Float)
    snr: Mapped[float | None] = mapped_column(Float)
    hops_away: Mapped[int | None] = mapped_column(Integer)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Course(Base):
    """A course drawn on the tracking map, from a GPX file (services/courses.py)."""

    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80))
    color: Mapped[str] = mapped_column(String(7))
    visible: Mapped[bool] = mapped_column(Boolean, default=True)
    # [[[lat, lon], ...], ...]: one list per track segment or route.
    segments: Mapped[list[Any]] = mapped_column(JSONB)
    waypoints: Mapped[list[Any]] = mapped_column(JSONB)  # [{"name", "lat", "lon"}]
    distance_m: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class MapTile(Base):
    """A map tile saved for use without internet (services/offline_map.py)."""

    __tablename__ = "map_tiles"

    z: Mapped[int] = mapped_column(Integer, primary_key=True)
    x: Mapped[int] = mapped_column(Integer, primary_key=True)
    y: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_type: Mapped[str] = mapped_column(String(32))
    data: Mapped[bytes] = mapped_column(LargeBinary)
