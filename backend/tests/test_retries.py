"""Automatic retries of direct messages, as the MeshCore app does (services/messaging.py): up to 3
attempts with the same timestamp; on a stored route two direct, then the route is reset and the
last is flooded; with no route, 2 flood attempts. An ACK for any attempt means delivered."""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app import db
from app.models import SendAttempt
from app.radio import simulated
from app.radio import supervisor as supervisor_module
from app.radio.supervisor import supervisor
from app.services.messaging import next_retry
from tests.conftest import csrf, do_setup, radio_adapter, wait_for
from tests.test_api import _connected_conversations


def test_retry_plan():
    def a(n, flood):
        return SendAttempt(attempt_no=n + 1, round_attempt=n, flood=flood)

    assert next_retry([]) is None
    assert next_retry([a(0, False)]) == (1, False)
    assert next_retry([a(0, False), a(1, False)]) == (2, True)  # reset the route, then flood
    assert next_retry([a(0, False), a(1, False), a(2, True)]) is None
    assert next_retry([a(0, True)]) == (1, False)  # no stored route: flood again
    assert next_retry([a(0, True), a(1, True)]) is None  # two floods are the limit


@pytest.fixture
def quick_acks(monkeypatch):
    """Wait a moment, not 10 seconds, for each ACK."""
    monkeypatch.setattr(supervisor_module, "ACK_WAIT_MIN_SECONDS", 0.4)
    monkeypatch.setattr(supervisor_module, "ACK_WAIT_MAX_SECONDS", 0.4)


async def _tracker_conversation(client, *, route: bool):
    simulated._SIM_STATE = None
    await do_setup(client)
    await _connected_conversations(client)
    (await radio_adapter()).ack_probability = 0.0  # nothing is ever confirmed
    contacts = (await client.get("/api/contacts")).json()["items"]
    tracker = next(c for c in contacts if c["name"] == "Tracker (sim)")
    st = simulated.sim_state()
    if route:
        st["contact_paths"][tracker["public_key"]] = (1, "a1")
    else:
        st["contact_paths"].pop(tracker["public_key"], None)
    conv = (await client.post(f"/api/contacts/{tracker['id']}/conversation", headers=csrf(client))).json()
    return conv["conversation_id"], tracker["public_key"]


async def _send(client, conv_id: str, body: str) -> str:
    r = await client.post(
        f"/api/conversations/{conv_id}/messages",
        headers=csrf(client),
        json={"client_message_id": uuid.uuid4().hex, "body": body},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _message(client, conv_id: str, mid: str) -> dict:
    page = (await client.get(f"/api/conversations/{conv_id}/messages")).json()
    return next(m for m in page["messages"] if m["id"] == mid)


def _sent(body: str) -> list[dict]:
    return [d for d in simulated.sim_state().get("sent_dms", []) if d["text"] == body]


async def _settled(client, conv_id, mid, state):
    async def done():
        m = await _message(client, conv_id, mid)
        return m if m["state"] == state else None

    return await wait_for(done, timeout=15)


async def test_stored_route_two_direct_then_flood(client, quick_acks):
    conv_id, key = await _tracker_conversation(client, route=True)
    mid = await _send(client, conv_id, "are you there?")
    m = await _settled(client, conv_id, mid, "no_ack")
    sent = _sent("are you there?")
    assert [d["attempt"] for d in sent] == [0, 1, 2]
    assert [d["flood"] for d in sent] == [False, False, True]  # the route was reset before the last
    assert len({d["timestamp"] for d in sent}) == 1  # the same message each time
    assert key not in simulated.sim_state()["contact_paths"]
    assert m["meta"]["delivery"] == {"attempt": 3, "of": 3, "route": "flood"}
    assert m["sender_timestamp"] == sent[0]["timestamp"]

    # A manual retry starts a new round: no route now, so two floods.
    r = await client.post(
        f"/api/messages/{mid}/retry", headers=csrf(client), json={"confirm_possible_duplicate": True}
    )
    assert r.status_code == 200

    async def second_round_sent():
        return len(_sent("are you there?")) == 5

    await wait_for(second_round_sent, timeout=15)
    m = await _settled(client, conv_id, mid, "no_ack")
    again = _sent("are you there?")[3:]
    assert [d["attempt"] for d in again] == [0, 1] and all(d["flood"] for d in again)
    assert m["meta"]["delivery"] == {"attempt": 2, "of": 2, "route": "flood"}


async def test_no_route_two_floods(client, quick_acks):
    conv_id, _ = await _tracker_conversation(client, route=False)
    mid = await _send(client, conv_id, "hello by flood")
    await _settled(client, conv_id, mid, "no_ack")
    assert [(d["attempt"], d["flood"]) for d in _sent("hello by flood")] == [(0, True), (1, True)]


async def test_late_ack_of_an_earlier_attempt(client, monkeypatch):
    monkeypatch.setattr(supervisor_module, "ACK_WAIT_MIN_SECONDS", 1.5)
    monkeypatch.setattr(supervisor_module, "ACK_WAIT_MAX_SECONDS", 1.5)
    conv_id, _ = await _tracker_conversation(client, route=True)
    mid = await _send(client, conv_id, "late ack")

    async def second_sent():
        return len(_sent("late ack")) == 2

    await wait_for(second_sent, timeout=10)
    async with db.session_factory()() as s:
        first = (
            await s.execute(
                select(SendAttempt).where(
                    SendAttempt.message_id == uuid.UUID(mid), SendAttempt.round_attempt == 0
                )
            )
        ).scalar_one()
    await supervisor._on_ack(first.expected_ack)  # the first attempt's ACK, arriving late
    m = await _settled(client, conv_id, mid, "acknowledged")
    assert m["meta"]["delivery"]["attempt"] == 2
    await asyncio.sleep(2.5)
    assert len(_sent("late ack")) == 2  # no third attempt
    assert (await _message(client, conv_id, mid))["state"] == "acknowledged"
