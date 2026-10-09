"""Mandate/candidacy handling shared by several tools."""
from __future__ import annotations

from datetime import date
from typing import Any

from .client import AWSession
from .endpoints import MANDATE_WON, TTL_ENTITY
from .reference import _first, parliaments, periods


def _faction_label(m: dict) -> str | None:
    """Faction at the end of the membership list (the most recent one), e.g. 'SPD'."""
    memberships = m.get("fraction_membership") or []
    if not memberships:
        return None
    return memberships[-1].get("label") or _first(memberships[-1].get("fraction")).get("label")


def slim_mandate(m: dict, period_idx: dict[int, dict], current_period_ids: set[int], today: date) -> dict:
    period_ref = _first(m.get("parliament_period"))
    pid = period_ref.get("id")
    pinfo = period_idx.get(pid, {})
    electoral = m.get("electoral_data") or {}
    constituency = _first(electoral.get("constituency"))
    end = m.get("end_date")
    is_mandate = m.get("type") == "mandate"
    out: dict[str, Any] = {
        "mandate_id": m["id"],
        "type": m.get("type"),
        "parliament_id": pinfo.get("parliament_id"),
        "parliament": pinfo.get("parliament"),
        "period_id": pid,
        "period": period_ref.get("label"),
        "period_start": pinfo.get("start_date"),
        "period_end": pinfo.get("end_date"),
        "start_date": m.get("start_date"),
        "end_date": end,
        "faction": _faction_label(m) if is_mandate else None,
        "party_ran_for": _first(m.get("party")).get("label"),
        "party_id": _first(m.get("party")).get("id"),
        "mandate_won": electoral.get("mandate_won"),
        "mandate_won_text": MANDATE_WON.get(electoral.get("mandate_won") or ""),
        "constituency": constituency.get("label"),
        "list_position": electoral.get("list_position"),
        "constituency_result_percent": electoral.get("constituency_result"),
        "is_current": bool(is_mandate and pid in current_period_ids and (end is None or end >= today.isoformat())),
    }
    if pinfo.get("type") == "legislature" and pinfo.get("previous_period_id"):
        out["election_period"] = {"id": pinfo["previous_period_id"], "label": pinfo.get("previous_period_label")}
    elif pinfo.get("type") == "election":
        out["election_period"] = {"id": pid, "label": period_ref.get("label")}
    return {k: v for k, v in out.items() if v is not None}


async def current_period_ids(s: AWSession) -> set[int]:
    return {p["current_period"]["id"] for p in await parliaments(s) if p.get("current_period")}


async def load_mandates(
    s: AWSession,
    politician_ids: list[int],
    *,
    types: tuple[str, ...] = ("mandate", "candidacy"),
    today: date | None = None,
) -> dict[int, list[dict]]:
    """Slim mandates/candidacies per politician id, newest period first.

    Always sends current_on=all - the API default hides former mandates (see aw.endpoints).
    """
    today = today or date.today()
    if not politician_ids:
        return {}
    params: dict[str, Any] = {"current_on": "all"}
    if len(politician_ids) == 1:
        params["politician"] = politician_ids[0]
    else:
        params["politician[in]"] = politician_ids
    result = await s.list_all("candidacy_mandate", params, TTL_ENTITY, max_items=2000)
    period_idx = await periods(s)
    current_ids = await current_period_ids(s)
    grouped: dict[int, list[dict]] = {pid: [] for pid in politician_ids}
    for m in result.items:
        if m.get("type") not in types:
            continue
        pol_id = _first(m.get("politician")).get("id")
        if pol_id in grouped:
            grouped[pol_id].append(slim_mandate(m, period_idx, current_ids, today))
    for rows in grouped.values():
        rows.sort(key=lambda r: (r.get("period_end") or "9999", r.get("period_start") or "", r["mandate_id"]), reverse=True)
    return grouped
