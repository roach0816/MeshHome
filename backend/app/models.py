"""Database schema. Field names are application design, not MeshCore wire fields."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
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
