"""The command bot, driven through the simulated radio's receive path."""

import asyncio
import time

from sqlalchemy import select

from app import db
from app.models import Message
from app.radio.base import IncomingMessage
from app.services import bot
from tests.conftest import csrf, do_setup, radio_adapter, wait_for
from tests.test_api import _connected_conversations


async def _send_to_node(name_key: str, text: str, age: int = 0, meta: dict | None = None) -> None:
    from app.radio import simulated

    (await radio_adapter()).inject(
        IncomingMessage(
            kind="dm",
            text=text,
            pubkey_prefix=simulated._key(name_key)[:12],
            sender_timestamp=int(time.time()) - age,
            meta=meta or {},
        )
    )


async def _settle() -> None:
    """Give the bot task time to act before checking that it did nothing."""
    await asyncio.sleep(0.5)


async def _bot_replies() -> list[Message]:
    async with db.session_factory()() as s:
        rows = (await s.execute(select(Message).where(Message.direction == "out"))).scalars().all()
    return [m for m in rows if (m.meta or {}).get("bot")]


async def _received(text: str) -> bool:
    async with db.session_factory()() as s:
        return (await s.execute(select(Message).where(Message.body == text))).first() is not None


async def test_bot_answers_commands_from_allowed_contacts(client, monkeypatch):
    from app.radio import simulated

    monkeypatch.setattr(bot, "PER_CONTACT_SECONDS", 1.5)

    simulated._SIM_STATE = None
    bot._last_reply.clear()
    bot._recent.clear()
    await do_setup(client)
    await _connected_conversations(client)
    assert (await client.get("/api/settings/bot")).json() == {"enabled": False, "allow": "favorites"}

    # Off by default: the command is archived, nothing is sent.
    await _send_to_node("tracker", "/info")
    await wait_for(lambda: _received("/info"))
    await _settle()
    assert await _bot_replies() == []

    r = await client.put(
        "/api/settings/bot", headers=csrf(client), json={"enabled": True, "allow": "favorites"}
    )
    assert r.status_code == 200
    # Not a favourite: ignored.
    await _send_to_node("neighbor", "/ping")
    await wait_for(lambda: _received("/ping"))
    await _settle()
    assert await _bot_replies() == []

    items = (await client.get("/api/contacts", params={"q": "Tracker"})).json()["items"]
    await client.post(
        f"/api/contacts/{items[0]['id']}/favorite", headers=csrf(client), json={"favorite": True}
    )

    await _send_to_node("tracker", "/INFO please")
    replies = await wait_for(_bot_replies)
    info = replies[0]
    assert info.body.startswith("Home (simulated) · MeshHome ") and "contacts" in info.body
    assert len(info.body.encode()) <= 150 and info.meta == {"bot": "info"}

    # A quick follow-up command waits its turn (1.5 s here, 10 s for real) and is then answered.
    await _send_to_node("tracker", "/ping", meta={"SNR": 7.25, "RSSI": -80, "path_len": 2})
    await wait_for(lambda: _received("/ping"))
    await _settle()
    assert len(await _bot_replies()) == 1  # still waiting

    async def two():
        r = await _bot_replies()
        return r if len(r) == 2 else None

    replies = await wait_for(two)
    assert any(m.body == "pong · SNR 7.25 dB, RSSI -80 dBm, 2 hops" for m in replies)
    gap = sorted(m.created_at for m in replies)
    assert (gap[1] - gap[0]).total_seconds() >= 1.0

    # Old commands (e.g. collected after an outage) are not answered.
    bot._last_reply.clear()
    await _send_to_node("tracker", "/ping", age=3600)
    await wait_for(lambda: _received("/ping"))
    await _settle()
    assert len(await _bot_replies()) == 2

    # Everyone: unknown commands get a hint.
    await client.put("/api/settings/bot", headers=csrf(client), json={"enabled": True, "allow": "everyone"})
    await _send_to_node("neighbor", "/forecast")

    async def three():
        r = await _bot_replies()
        return r if len(r) == 3 else None

    replies = await wait_for(three)
    assert any(m.body == "Unknown command /forecast. Try /help" for m in replies)

    # Replies go out through the normal queue.
    async def sent():
        return all(m.state != "queued" for m in await _bot_replies())

    await wait_for(sent)


def test_parse_and_fit():
    assert bot.parse("/info") == ("info", None)
    assert bot.parse("  /Ping@Home Extra words") == ("ping", "extra")
    assert bot.parse("/ha " + "x" * 30) == ("ha", "x" * 20)
    assert bot.parse("hello /info") is None and bot.parse("/") is None and bot.parse("") is None
    long = bot._fit("é" * 200)
    assert len(long.encode()) <= 150 and long.endswith("...")
    assert bot._route({"path_len": 255, "SNR": -3.5}) == "SNR -3.5 dB, direct route"
    assert bot._route({"path_len": 0}) == "0 hops (heard directly)"


async def test_wait_turn_queues_a_few_then_drops(monkeypatch):
    import uuid

    monkeypatch.setattr(bot, "PER_CONTACT_SECONDS", 0.2)
    bot._last_reply.clear()
    bot._recent.clear()
    bot._waiting.clear()
    who = uuid.uuid4()
    results = await asyncio.gather(*(bot._wait_turn(who) for _ in range(5)))
    # One goes at once, three wait their turn, the fifth is dropped.
    assert sorted(results) == [False, True, True, True, True]
    assert bot._waiting[who] == 0
