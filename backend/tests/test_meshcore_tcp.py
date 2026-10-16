"""Reply matching in the TCP adapter, with events shaped like meshcore 2.3.14's reader."""

from meshcore import EventType
from meshcore.events import Event

from app.radio.base import RemoteTicket
from app.radio.meshcore_tcp import MeshCoreTcpRadio

KEY = "ab" * 32


def _ticket(kind: str) -> RemoteTicket:
    return RemoteTicket(kind=kind, public_key=KEY, tag="0a0b0c0d")


def test_status_reply_tag_is_read_from_attributes():
    radio = MeshCoreTcpRadio("localhost", 5000)
    status = {"pubkey_pre": KEY[:12], "bat": 4100, "uptime": 60}
    # Binary response path: tag only in the attributes, not in the payload.
    ok = Event(EventType.STATUS_RESPONSE, status, {"pubkey_prefix": KEY[:12], "tag": "0a0b0c0d"})
    assert radio._match(_ticket("status"), ok) == {"bat": 4100, "uptime": 60}
    other = Event(EventType.STATUS_RESPONSE, status, {"pubkey_prefix": KEY[:12], "tag": "ffffffff"})
    assert radio._match(_ticket("status"), other) is None
    # Push path (older firmware): no tag, matched by key prefix.
    push = Event(EventType.STATUS_RESPONSE, status, {"pubkey_prefix": KEY[:12]})
    assert radio._match(_ticket("status"), push) == {"bat": 4100, "uptime": 60}
    stranger = Event(
        EventType.STATUS_RESPONSE, {**status, "pubkey_pre": "cd" * 6}, {"pubkey_prefix": "cd" * 6}
    )
    assert radio._match(_ticket("status"), stranger) is None


def test_other_replies_match_by_tag():
    radio = MeshCoreTcpRadio("localhost", 5000)
    acl = {"tag": "0a0b0c0d", "acl_data": [{"key": "112233445566", "perm": 3}], "pubkey_prefix": KEY[:12]}
    ev = Event(EventType.ACL_RESPONSE, acl, {"tag": "0a0b0c0d", "pubkey_prefix": KEY[:12]})
    assert radio._match(_ticket("acl"), ev) == {"acl": acl["acl_data"]}
    owner = Event(
        EventType.BINARY_RESPONSE,
        {"tag": "0a0b0c0d", "data": (b"\0" * 4 + b"Roof\nMe").hex()},
        {"tag": "0a0b0c0d"},
    )
    assert radio._match(_ticket("owner"), owner) == {"text": "Roof\nMe"}
    untagged = Event(EventType.BINARY_RESPONSE, {"data": "00"}, {})
    assert radio._match(_ticket("owner"), untagged) is None


async def test_contact_add_builds_a_record_the_library_can_send():
    """contact_add goes through meshcore's own update_contact packing (CMD_ADD_UPDATE_CONTACT)."""
    from meshcore.commands import CommandHandler

    sent: list[bytes] = []

    class Commands:
        async def send(self, data, expected):
            sent.append(data)
            return Event(EventType.OK, {})

        async def add_contact(self, contact):
            return await CommandHandler.update_contact(self, contact)

    class MC:
        is_connected = True
        commands = Commands()

    radio = MeshCoreTcpRadio("radio.invalid", 5000)
    radio._mc = MC()
    await radio.configure("contact_add", {"public_key": KEY, "name": "Trail Buddy", "kind": 2})
    (data,) = sent
    assert data[0] == 0x09 and data[1:33] == bytes.fromhex(KEY)
    assert data[33] == 2 and data[34] == 0  # type, flags
    assert data[35] == 255  # no path yet: flood
    assert data[100:132].rstrip(b"\0") == b"Trail Buddy"
