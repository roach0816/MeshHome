"""Message paths from the radio's raw packet log, message details, and deleting a message."""

import time

from app.radio import packets
from app.radio.base import IncomingMessage
from app.services.msg_paths import PathTracker
from tests.conftest import HEADERS, csrf, do_setup, radio_adapter, wait_for
from tests.test_api import _connected_conversations


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _grp(channel: int, body: bytes, hops: list[bytes]) -> packets.Packet:
    return packets.parse(packets.build(packets.TYPE_GRP_TXT, bytes([channel]) + body, hops))


def test_parse_matches_meshcore_layout():
    raw = packets.build(packets.TYPE_TXT_MSG, bytes([0xAA, 0xBB]) + b"mac+cipher", [b"\x12\x34", b"\x56\x78"])
    p = packets.parse(raw)
    assert (p.payload_type, p.hash_size, p.hops, p.flood) == (packets.TYPE_TXT_MSG, 2, ("1234", "5678"), True)
    # Transport-coded floods carry 4 extra bytes before path_len.
    t = packets.parse(
        packets.build(packets.TYPE_GRP_TXT, b"\x11xyz", [b"\x01"], route=packets.ROUTE_TRANSPORT_FLOOD)
    )
    assert t.hops == ("01",) and t.payload == b"\x11xyz"
    assert (
        packets.parse(b"\x05") is None and packets.parse(bytes([0x05, 0xC1, 1, 2, 3, 4])) is None
    )  # bad size
    # MeshCore's Public channel key -> channel hash 0x11 (16-byte key)
    assert packets.channel_hash(bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")) == 0x11


def test_tracker_claims_newest_matching_packet_and_follows_copies():
    clock = Clock()
    t = PathTracker(clock=clock)
    t.heard(_grp(0x22, b"other channel", [b"\x01"]), 5.0, -90)
    t.heard(_grp(0x11, b"old", [b"\x02"]), 4.0, -91)
    clock.t += 30  # too old to belong to the next message
    t.heard(_grp(0x11, b"the message", [b"\xa1"]), 7.25, -80)
    t.heard(_grp(0x11, b"the message", [b"\xb2", b"\xa1"]), 2.5, -97)  # same packet, longer route
    key, paths = t.claim(channel_hash=0x11)
    assert [p["hops"] for p in paths] == [["a1"], ["b2", "a1"]] and paths[0]["snr"] == 7.25
    t.follow(key, "msg-1", len(paths))
    clock.t += 2
    assert t.heard(_grp(0x11, b"the message", [b"\xc3"]), 1.0, -100) == (
        "msg-1",
        {"hops": ["c3"], "hash_size": 1, "route": "flood", "snr": 1.0, "rssi": -100},
    )
    assert t.heard(_grp(0x11, b"a new message", [b"\xa1"]), 6.0, -82) is None
    assert t.claim(channel_hash=0x33) == (None, [])
    clock.t += 120
    assert t.heard(_grp(0x11, b"the message", [b"\xd4"]), 0.0, -110) is None  # no longer followed


def test_tracker_dm_matches_sender_and_us():
    t = PathTracker(clock=Clock())
    dm = lambda dest, src, hops: packets.parse(  # noqa: E731
        packets.build(packets.TYPE_TXT_MSG, bytes([dest, src]) + b"x" * 10, hops)
    )
    t.heard(dm(0x99, 0x42, [b"\x01"]), 1.0, -90)  # for someone else
    t.heard(dm(0xAB, 0x42, [b"\x02"]), 3.0, -85)
    _, paths = t.claim(sender_hash=0x42, self_hash=0xAB)
    assert [p["hops"] for p in paths] == [["02"]]


async def _wait_paths(client, conv_id, text):
    async def found():
        msgs = (await client.get(f"/api/conversations/{conv_id}/messages?limit=200")).json()["messages"]
        m = next((m for m in msgs if m["body"] == text), None)
        return m if m and m["meta"].get("paths") else None

    return await wait_for(found)


async def test_paths_details_and_delete_end_to_end(client):
    from app.radio import simulated

    simulated._SIM_STATE = None
    await do_setup(client)
    convs = await _connected_conversations(client)
    public = next(c for c in convs if c["title"] == "Public")
    (await radio_adapter()).inject(
        IncomingMessage(
            kind="channel",
            text="hello over the hills",
            channel_slot=public["channel_slot"],
            sender_label="Tracker (sim)",
            sender_timestamp=int(time.time()),
            meta={"SNR": 6.5, "RSSI": -88, "path_len": 2, "path_hash_mode": 0},
        )
    )
    m = await _wait_paths(client, public["id"], "hello over the hills")
    info = (await client.get(f"/api/messages/{m['id']}/info")).json()
    assert info["received"] == {"snr": 6.5, "rssi": -88, "route": "flood", "hops": 2, "path_hash_size": 1}
    assert info["sender"]["contact"]["name"] == "Tracker (sim)" and info["sender"]["match"] == "name"
    hop_names = {n for p in info["paths"] for h in p["hops"] for n in h["names"]}
    assert hop_names <= {"Roof Repeater (sim)", "Hilltop Repeater (sim)"} and hop_names

    # Delete: archive only.
    r = await client.delete(f"/api/messages/{m['id']}", headers=csrf(client))
    assert r.status_code == 204
    assert (await client.get(f"/api/messages/{m['id']}/info")).status_code == 404
    r = await client.delete(f"/api/messages/{m['id']}", headers=HEADERS | csrf(client))
    assert r.status_code == 404
