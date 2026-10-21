"""Tracing a route of repeaters (POST /api/radio/trace), on the simulated radio."""

from app.radio.simulated import _key
from tests.conftest import csrf, do_setup, radio_adapter


async def test_trace_out_and_back(client):
    from app.radio import simulated

    simulated._SIM_STATE = None
    await do_setup(client)
    await radio_adapter()
    roof, hill = _key("roof"), _key("hilltop")
    r = await client.post("/api/radio/trace", headers=csrf(client), json={"hops": [roof, hill]})
    assert r.status_code == 200, r.text
    t = r.json()
    # Out along the route and back the same way, so the probe returns to this radio.
    assert [h["hash"] for h in t["hops"]] == [roof[:8], hill[:8], roof[:8]]
    assert t["hops"][0]["names"] == ["Roof Repeater (sim)"] and t["hops"][1]["names"] == [
        "Hilltop Repeater (sim)"
    ]
    assert all(isinstance(h["snr"], float) for h in t["hops"]) and isinstance(t["final_snr"], float)
    assert t["elapsed_ms"] > 0

    # Short prefixes work too (1-byte hashes, like a typed path).
    r = await client.post("/api/radio/trace", headers=csrf(client), json={"hops": [roof[:2]]})
    assert r.status_code == 200 and [h["hash"] for h in r.json()["hops"]] == [roof[:2]]


async def test_trace_errors(client):
    from app.radio import simulated

    simulated._SIM_STATE = None
    await do_setup(client)
    await radio_adapter()
    # A hop that isn't a repeater: the probe never comes back.
    r = await client.post("/api/radio/trace", headers=csrf(client), json={"hops": [_key("roof"), "ab" * 32]})
    assert r.status_code == 504 and "didn't come back" in r.json()["detail"]
    for bad in ([], ["xyz"], ["abc"], ["ab"] * 9):
        r = await client.post("/api/radio/trace", headers=csrf(client), json={"hops": bad})
        assert r.status_code == 422, bad
    assert (
        await client.post("/api/radio/trace", json={"hops": [_key("roof")]})
    ).status_code == 403  # no CSRF
