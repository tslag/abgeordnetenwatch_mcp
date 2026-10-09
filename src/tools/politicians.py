"""aw_find_politician and aw_get_politician."""
from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from aw.client import AWSession
from aw.endpoints import TTL_ENTITY
from aw.envelope import envelope, page_info, tool_errors
from aw.mandates import load_mandates
from aw.reference import _first, resolve_parliament, resolve_party
from aw.text import name_variants, parse_name
from tools.common import READ_ONLY, new_session

PROFILE_SEARCH_LIMIT = 100  # candidates fetched before local ranking
ENRICH_LIMIT = 25  # candidates whose mandates are loaded


def _match_score(p: dict, first: str | None, last: str) -> float:
    """0..2.5: exact last name, exact first name, plus a small prefix bonus."""
    pl, pf = (p.get("last_name") or "").casefold(), (p.get("first_name") or "").casefold()
    score = 0.0
    if pl == last.casefold():
        score += 1
    elif pl.startswith(last.casefold()):
        score += 0.5
    if first:
        if pf == first.casefold():
            score += 1
        elif pf.startswith(first.casefold()) or first.casefold() in pf:
            score += 0.5
    return score


async def _search_by_name(s: AWSession, first: str | None, last: str, raw: str, party_id: int | None) -> tuple[list[dict], int, tuple[str | None, str] | None]:
    """Try the name as typed, then spelling variants (the API does not transliterate), then swapped."""
    attempts: list[tuple[str | None, str]] = []
    for last_v in name_variants(last):
        for first_v in (name_variants(first) if first else [None]):
            attempts.append((first_v, last_v))
    attempts = attempts[:3]
    if first:
        attempts.append((last, first))  # "Scholz Olaf"
    for f, l in attempts:
        params: dict[str, Any] = {"last_name[cn]": l, "range_end": PROFILE_SEARCH_LIMIT}
        if f:
            params["first_name[cn]"] = f
        if party_id:
            params["party"] = party_id
        res = await s.list("politician", params)
        if res.total:
            return res.items, res.total, (f, l)
    params = {"label[cn]": raw, "range_end": PROFILE_SEARCH_LIMIT}
    if party_id:
        params["party"] = party_id
    res = await s.list("politician", params)
    return res.items, res.total, (first, last)


async def _search_in_parliament(s: AWSession, first: str | None, last: str, parliament_id: int, party_id: int | None) -> tuple[list[dict], int, tuple[str | None, str] | None]:
    """Name search restricted to people who held a mandate in one parliament."""
    for last_v in name_variants(last)[:3]:
        params: dict[str, Any] = {
            "current_on": "all",
            "type": "mandate",
            "politician[entity.last_name][cn]": last_v,
            "parliament_period[entity.parliament.entity.id]": parliament_id,
            "range_end": 1000,
        }
        if first:
            params["politician[entity.first_name][cn]"] = first
        res = await s.list("candidacy_mandate", params)
        ids = list(dict.fromkeys(_first(m.get("politician")).get("id") for m in res.items))
        ids = [i for i in ids if i]
        if not ids:
            continue
        people = await s.list("politician", {"id[in]": ids[:PROFILE_SEARCH_LIMIT], "range_end": PROFILE_SEARCH_LIMIT, **({"party": party_id} if party_id else {})})
        return people.items, len(ids), (first, last_v)
    return [], 0, None


def register_politician_tools(mcp: FastMCP) -> None:
    @mcp.tool(name="aw_find_politician", annotations=READ_ONLY)
    @tool_errors
    async def aw_find_politician(
        name: Annotated[str, Field(min_length=2, description='Name as the user wrote it, e.g. "Olaf Scholz", "Scholz", "Müller, Thomas". Umlaut spellings (Mueller/Müller) are handled.')],
        parliament: Annotated[str | None, Field(description="Only people who held a mandate there: name, alias ('Bundestag', 'NRW', 'EU') or id.")] = None,
        party: Annotated[str | None, Field(description='Party short name ("SPD", "CDU") or id; current party.')] = None,
        active_only: Annotated[bool, Field(description="Only people with a current mandate.")] = False,
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
    ) -> dict:
        """Find an MP by name and get their politician_id. ALWAYS call this first: every other
        person-level tool needs the numeric politician_id.

        Names match as 'contains', so common surnames return many people (e.g. 'Müller' ~350).
        Check `unique_exact_match` / `needs_user_choice`: if several people fit, ask the user
        which one (use party, parliament, birth year) - never guess, a wrong id attributes votes
        to the wrong person. Candidates include their mandates, so former MPs are found too.
        """
        s = new_session(8)
        first, last = parse_name(name)
        party_ref = await resolve_party(s, party) if party else None
        party_id = party_ref["id"] if party_ref else None

        if parliament:
            parl = await resolve_parliament(s, parliament)
            items, total, used = await _search_in_parliament(s, first, last, parl["id"], party_id)
        else:
            items, total, used = await _search_by_name(s, first, last, name, party_id)
        # rank against the spelling that actually matched (e.g. 'Müller' for a typed 'Mueller')
        q_first, q_last = used if used else (first, last)

        if not items:
            hint = " Try fewer words (just the surname) or drop the party/parliament filter."
            return envelope(f"No politician found for '{name}'.", {"matches": [], "total_matches": 0, "unique_exact_match": None, "needs_user_choice": False}, session=s, notes=[hint.strip()])

        ranked = sorted(items, key=lambda p: (_match_score(p, q_first, q_last), p["id"]), reverse=True)[:ENRICH_LIMIT]
        mandates = await load_mandates(s, [p["id"] for p in ranked], types=("mandate",))

        candidates = []
        for p in ranked:
            rows = mandates.get(p["id"], [])
            candidates.append({
                "politician_id": p["id"],
                "name": p.get("label"),
                "title": p.get("field_title"),
                "party": _first(p.get("party")).get("label"),
                "year_of_birth": p.get("year_of_birth"),
                "occupation": p.get("occupation"),
                "is_current_mp": any(r.get("is_current") for r in rows),
                "mandates": [
                    {k: r.get(k) for k in ("mandate_id", "parliament", "period", "faction", "is_current")}
                    for r in rows[:4]
                ],
                "url": p.get("abgeordnetenwatch_url"),
                "_score": _match_score(p, q_first, q_last),
            })
        candidates.sort(key=lambda c: (c["_score"], c["is_current_mp"], bool(c["mandates"])), reverse=True)
        if active_only:
            candidates = [c for c in candidates if c["is_current_mp"]]
        exact = [c for c in candidates if c["_score"] >= (2 if q_first else 1)]
        unique_id = exact[0]["politician_id"] if (len(exact) == 1 and (q_first or total == 1)) else None
        if total == 1 and candidates:
            unique_id = candidates[0]["politician_id"]
        shown = candidates[:limit]
        for c in shown:
            c.pop("_score", None)

        notes = []
        if used and (used[1] != last or (used[0] or "") != (first or "")):
            notes.append(f"No hit for the spelling as typed; matched using: {' '.join(filter(None, used))}.")
        if total > len(shown):
            notes.append(f"{total} people match; showing the {len(shown)} best. Add a first name, party or parliament to narrow down.")
        if unique_id is None and len(shown) > 1:
            notes.append("Several people fit - ask the user which one before using any politician_id.")
        data = {"matches": shown, "total_matches": total, "unique_exact_match": unique_id, "needs_user_choice": unique_id is None and len(shown) > 1}
        summary = (
            f"Unique match: {shown[0]['name']} (politician_id {unique_id})" if unique_id and shown
            else f"{total} match(es) for '{name}', no unique exact match"
        )
        return envelope(summary, data, session=s, page=page_info(total, len(shown)), notes=notes, sources=[c["url"] for c in shown[:3]])

    @mcp.tool(name="aw_get_politician", annotations=READ_ONLY)
    @tool_errors
    async def aw_get_politician(
        politician_id: Annotated[int, Field(gt=0, description="Numeric id from aw_find_politician.")],
    ) -> dict:
        """Profile and full career timeline of one politician.

        Returns bio fields plus every mandate (parliament, legislature period, faction, how the
        seat was won, constituency) and every election candidacy (with the party they ran for).
        Each entry names its ELECTION period - use that with aw_find_election_programs. A person
        can sit in several parliaments over time (e.g. Hamburg and Bundestag).
        """
        s = new_session(6)
        summary, data, notes, sources = await build_politician(s, politician_id)
        return envelope(summary, data, session=s, notes=notes, sources=sources)


async def build_politician(s: AWSession, politician_id: int) -> tuple[str, dict, list[str], list[str | None]]:
    """Profile + career timeline; shared by aw_get_politician and the aw://politician/{id} resource."""
    p = await s.get_one("politician", politician_id, ttl=TTL_ENTITY)
    rows = (await load_mandates(s, [politician_id]))[politician_id]
    mandates = [r for r in rows if r["type"] == "mandate"]
    candidacies = [r for r in rows if r["type"] == "candidacy"]
    current = [m for m in mandates if m.get("is_current")]
    profile = {
        "politician_id": p["id"],
        "name": p.get("label"),
        "title": p.get("field_title"),
        "sex": p.get("sex"),
        "year_of_birth": p.get("year_of_birth"),
        "party": _first(p.get("party")).get("label"),
        "party_past": p.get("party_past"),
        "education": p.get("education"),
        "occupation": p.get("occupation"),
        "residence": p.get("residence"),
        "questions_asked": _to_int(p.get("statistic_questions")),
        "questions_answered": _to_int(p.get("statistic_questions_answered")),
        "wikidata": p.get("qid_wikidata"),
        "bundestag_admin_id": p.get("ext_id_bundestagsverwaltung"),
        "url": p.get("abgeordnetenwatch_url"),
    }
    data = {
        "profile": {k: v for k, v in profile.items() if v not in (None, "")},
        "is_current_mp": bool(current),
        "mandates": mandates,
        "candidacies": candidacies,
    }
    where = ", ".join(f"{m['parliament']} ({m.get('faction') or 'no faction'})" for m in current) or "no current mandate"
    notes = []
    if not mandates:
        notes.append("No mandates on record (candidate only, or data not yet entered).")
    elif not current:
        notes.append("No current mandate - votes and side jobs exist only for past periods; pass period_id to the vote tools.")
    return f"{profile['name']}: {where}", data, notes, [p.get("abgeordnetenwatch_url")]


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
