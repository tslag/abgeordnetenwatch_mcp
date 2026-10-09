"""Cached reference data: parliaments, legislature/election periods, topics, parties.

All of it changes rarely (24 h TTL) and is small, so one request each serves a whole session.
"""
from __future__ import annotations

from typing import Any

from .client import AWSession
from .endpoints import TTL_REFERENCE
from .errors import AWUserError

_PARLIAMENT_ALIASES = {
    "bt": 5, "bundestag": 5, "bund": 5,
    "eu": 1, "europarlament": 1, "europaparlament": 1, "ep": 1, "europa": 1,
    "nrw": 4, "bw": 6, "mv": 9, "rlp": 7, "sh": 18, "nds": 12, "st": 8, "th": 15, "sn": 17, "bb": 16, "by": 13,
    "he": 11, "hb": 10, "hh": 3, "be": 2, "sl": 14,
}


def _first(value: Any) -> dict:
    """Reference fields come as an object or a one-element list; normalise to a dict."""
    if isinstance(value, list):
        return value[0] if value else {}
    return value or {}


def slim_parliament(p: dict) -> dict:
    cur = _first(p.get("current_project"))
    return {
        "id": p["id"],
        "name": p.get("label"),
        "full_name": p.get("label_external_long") or p.get("label"),
        "current_period": {"id": cur.get("id"), "label": cur.get("label")} if cur else None,
        "url": p.get("abgeordnetenwatch_url"),
    }


def slim_period(p: dict) -> dict:
    parliament = _first(p.get("parliament"))
    previous = _first(p.get("previous_period"))
    return {
        "id": p["id"],
        "label": p.get("label"),
        "type": p.get("type"),
        "parliament_id": parliament.get("id"),
        "parliament": parliament.get("label"),
        "election_date": p.get("election_date"),
        "start_date": p.get("start_date_period"),
        "end_date": p.get("end_date_period"),
        "previous_period_id": previous.get("id"),
        "previous_period_label": previous.get("label"),
        "url": p.get("abgeordnetenwatch_url"),
    }


def slim_topic(t: dict) -> dict:
    parent = _first(t.get("parent"))
    return {
        "id": t["id"],
        "label": t.get("label"),
        "parent_id": parent.get("id"),
        "parent_label": parent.get("label"),
        "url": t.get("abgeordnetenwatch_url"),
    }


async def parliaments(s: AWSession) -> list[dict]:
    result = await s.list("parliament", {"range_end": 100}, TTL_REFERENCE)
    return [slim_parliament(p) for p in result.items]


async def periods(s: AWSession) -> dict[int, dict]:
    """All legislature and election periods keyed by id (one request, ~200 rows)."""
    result = await s.list_all("parliament_period", {}, TTL_REFERENCE, max_items=2000)
    return {p["id"]: slim_period(p) for p in result.items}


async def topics(s: AWSession) -> list[dict]:
    result = await s.list("topic", {"range_end": 500}, TTL_REFERENCE)
    return sorted((slim_topic(t) for t in result.items), key=lambda t: t["label"] or "")


async def resolve_parliament(s: AWSession, value: str | int) -> dict:
    """Accept an id, a name ('Bundestag', 'Bayern') or a short alias ('NRW', 'EU')."""
    all_parliaments = await parliaments(s)
    text = str(value).strip()
    if text.isdigit():
        for p in all_parliaments:
            if p["id"] == int(text):
                return p
        raise AWUserError(f"No parliament with id {text}. Use aw_lookup(kind='parliament') to list them.")
    key = text.casefold()
    if key in _PARLIAMENT_ALIASES:
        wanted = _PARLIAMENT_ALIASES[key]
        return next(p for p in all_parliaments if p["id"] == wanted)
    exact = [p for p in all_parliaments if key in ((p["name"] or "").casefold(), (p["full_name"] or "").casefold())]
    if len(exact) == 1:
        return exact[0]
    partial = [p for p in all_parliaments if key in (p["name"] or "").casefold() or key in (p["full_name"] or "").casefold()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        names = ", ".join(p["name"] for p in all_parliaments)
        raise AWUserError(f"Unknown parliament '{value}'. Available: {names}.")
    names = ", ".join(f"{p['name']} (id {p['id']})" for p in partial)
    raise AWUserError(f"'{value}' matches several parliaments: {names}. Pass the id.")


async def resolve_topic_ids(s: AWSession, value: str | int) -> list[dict]:
    """Topic by id or (partial) label. Sub-topics are not expanded automatically."""
    all_topics = await topics(s)
    text = str(value).strip()
    if text.isdigit():
        hit = [t for t in all_topics if t["id"] == int(text)]
        if hit:
            return hit
    key = text.casefold()
    exact = [t for t in all_topics if (t["label"] or "").casefold() == key]
    if exact:
        return exact
    partial = [t for t in all_topics if key in (t["label"] or "").casefold()]
    if not partial:
        raise AWUserError(f"No topic matches '{value}'. Browse them with aw_lookup(kind='topic') or read aw://topics.")
    return partial


async def resolve_party(s: AWSession, value: str | int) -> dict:
    text = str(value).strip()
    if text.isdigit():
        party = await s.get_one("party", int(text), ttl=TTL_REFERENCE)
        return {"id": party["id"], "label": party.get("label")}
    result = await s.list("party", {"label[cn]": text, "range_end": 50}, TTL_REFERENCE)
    key = text.casefold()
    exact = [p for p in result.items if (p.get("label") or "").casefold() == key or (p.get("short_name") or "").casefold() == key]
    pool = exact or result.items
    if not pool:
        raise AWUserError(f"No party matches '{value}'. Try the short name, e.g. 'SPD', 'CDU', 'GRÜNE'.")
    if len(pool) > 1:
        names = ", ".join(f"{p.get('label')} (id {p['id']})" for p in pool[:8])
        raise AWUserError(f"'{value}' matches several parties: {names}. Pass the id.")
    return {"id": pool[0]["id"], "label": pool[0].get("label")}
