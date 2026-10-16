"""Smoke tests against the REAL Abgeordnetenwatch API (deselected by default).

Run with:  uv run pytest -m live -v
They verify the assumptions the design rests on (filters, joins, request budgets). Facts about people
and votes can change, so assertions stick to structure and long-settled history (Olaf Scholz, id 66924).
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from fastmcp import Client

from aw.client import AWClient, get_client, set_client
from aw.ratelimit import RateLimiter
from server import build_server

# one event loop and ONE client/limiter for the whole module: that is how the real server process behaves,
# and the API's rate limit is per IP, so separate limiters per test would only create artificial bursts
pytestmark = [pytest.mark.live, pytest.mark.asyncio(loop_scope="module")]
SCHOLZ = 66924
RATE_LIMITED: list[float] = []  # one entry per HTTP 429 the API sent during this run


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live():
    original = RateLimiter.on_rate_limited

    def spy(self, retry_after=None):
        RATE_LIMITED.append(retry_after or 0.0)
        return original(self, retry_after)

    RateLimiter.on_rate_limited = spy
    real = AWClient()
    set_client(real)
    async with Client(build_server()) as c:
        yield c
    await real.aclose()
    set_client(None)
    RateLimiter.on_rate_limited = original


async def run(c: Client, tool: str, **args) -> dict:
    result = await c.call_tool(tool, args, raise_on_error=False)
    assert not result.is_error, result.content[0].text
    return result.data


async def test_find_olaf_scholz_is_unique_and_cheap(live):
    d = await run(live, "aw_find_politician", name="Olaf Scholz")
    assert d["data"]["unique_exact_match"] == SCHOLZ
    assert d["meta"]["requests"] <= 5
    parliaments = {m["parliament"] for m in d["data"]["matches"][0]["mandates"]}
    assert {"Bundestag", "Hamburg"} <= parliaments  # one person, several parliaments


async def test_umlaut_spelling_is_resolved_server_side(live):
    d = await run(live, "aw_find_politician", name="Ruth Mueller", parliament="Bayern")
    assert any(m["name"] == "Ruth Müller" for m in d["data"]["matches"])


async def test_votes_join_is_cheap_and_sorted(live):
    d = await run(live, "aw_politician_votes", politician_id=SCHOLZ, limit=10)
    dates = [v["date"] for v in d["data"]["votes"]]
    assert dates == sorted(dates, reverse=True) and d["data"]["summary"]["total"] > 0
    assert d["meta"]["requests"] <= 8
    again = await run(live, "aw_politician_votes", politician_id=SCHOLZ, topic="Energie", limit=5)
    assert again["meta"]["requests"] <= 3  # votes and reference data come from the cache


async def test_poll_tally_matches_vote_records(live):
    d = await run(live, "aw_poll_result", poll_id=6683, politician_id=SCHOLZ)
    overall = d["data"]["overall"]
    assert overall["members"] == sum(f["members"] for f in d["data"]["by_fraction"]) > 500
    assert d["data"]["politician"]["vote"] in {"yes", "no", "abstain", "no_show"}


async def test_sidejobs_levels_and_coverage(live):
    d = await run(live, "aw_get_sidejobs", politician_id=SCHOLZ)
    assert d["data"]["coverage"] == "bundestag" and d["data"]["summary"]["total"] >= 1
    ruth = await run(live, "aw_get_sidejobs", politician_id=133707)  # Landtag member
    assert ruth["data"]["coverage"] == "not_available"


async def test_programme_lookup_and_text_search(live):
    d = await run(live, "aw_find_election_programs", politician_id=SCHOLZ)
    program = d["data"][0]
    assert program["party"] == "SPD" and program["pdf_url"].endswith(".pdf")
    hits = await run(live, "aw_read_election_program", program_id=program["program_id"], query="Mindestlohn", top_k=2)
    assert hits["data"]["passages"] and hits["data"]["total_pages"] > 10


async def test_reference_data(live):
    parliaments = await run(live, "aw_lookup", kind="parliament")
    assert len(parliaments["data"]) == 18 and any(p["name"] == "EU-Parlament" for p in parliaments["data"])
    topics = await run(live, "aw_lookup", kind="topic", query="Energie")
    assert topics["data"][0]["id"] == 20


async def test_concurrent_requests_are_paced(live):
    """Sixteen distinct requests started at the same instant (like asyncio.gather inside a tool)."""
    session = get_client().session(budget=100)
    results = await asyncio.gather(*(session.list("politician", {"last_name[cn]": ch, "range_end": 1}) for ch in "abcdefghijklmnop"))
    assert len(results) == 16 and session.usage()["requests"] == 16


async def test_no_429_during_the_whole_live_run(live):
    """The API rejects bursts (<1 s apart) and anything above ~15 requests/minute sustained.

    Everything this module did - tool calls, a concurrent flood, several minutes of traffic - must have been
    accepted without a single HTTP 429 (this test runs last).
    """
    assert RATE_LIMITED == [], f"API answered HTTP 429 {len(RATE_LIMITED)} time(s)"
