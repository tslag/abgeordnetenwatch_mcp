"""aw_get_sidejobs and aw_committees."""
from __future__ import annotations

import re
from collections import Counter
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from aw.endpoints import (
    COMMITTEE_ROLES,
    SIDEJOB_CATEGORIES,
    SIDEJOB_INTERVALS,
    TTL_ENTITY,
    income_level_text,
)
from aw.envelope import envelope, page_info, tool_errors
from aw.errors import AWUserError
from aw.mandates import load_mandates
from aw.polls import clean_fraction_name
from aw.reference import _first, resolve_parliament
from aw.text import strip_html
from tools.common import READ_ONLY, new_session

BUNDESTAG_ID = 5
_DATE_HINT = re.compile(r"\b(?:bis|seit|ab|von)\s+\d{1,2}\.\d{1,2}\.\d{2,4}\b", re.IGNORECASE)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def decode_sidejob(j: dict) -> dict:
    category = _int(j.get("category"))
    level = _int(j.get("income_level"))
    interval = _int(j.get("interval"))
    extra = strip_html(j.get("additional_information"), 500)
    hint_source = " ".join(filter(None, [j.get("label"), j.get("job_title_extra"), extra]))
    hint = _DATE_HINT.search(hint_source)
    out = {
        "sidejob_id": j["id"],
        "title": j.get("label"),
        "title_extra": j.get("job_title_extra"),
        "organization": _first(j.get("sidejob_organization")).get("label"),
        "category": {"id": category, "label": (SIDEJOB_CATEGORIES.get(category) or (None, None))[0], "label_de": (SIDEJOB_CATEGORIES.get(category) or (None, None))[1]} if category else None,
        "income_level": level,
        "income_level_range": income_level_text(level),
        "income_exact": j.get("income"),
        "income_total": j.get("income_total"),
        "interval": SIDEJOB_INTERVALS.get(interval) if interval is not None else None,
        "city": _first(j.get("field_city")).get("label"),
        "country": _first(j.get("field_country")).get("label"),
        "topics": [t.get("label") for t in (j.get("field_topics") or [])],
        "date_hint": hint.group(0) if hint else None,
        "details": extra,
        "last_changed": j.get("data_change_date"),
        "mandate_ids": [m.get("id") for m in (j.get("mandates") or [])],
    }
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def register_sidejob_tools(mcp: FastMCP) -> None:
    @mcp.tool(name="aw_get_sidejobs", annotations=READ_ONLY)
    @tool_errors
    async def aw_get_sidejobs(
        politician_id: Annotated[int, Field(gt=0, description="Numeric id from aw_find_politician.")],
        category: Annotated[int | None, Field(description="Only this category id, e.g. 29647 = paid activities besides the mandate (see aw://codebook).")] = None,
        min_income_level: Annotated[int | None, Field(ge=0, le=10, description="Only entries with at least this Bundestag income level.")] = None,
        all_mandates: Annotated[bool, Field(description="Include earlier Bundestag terms. Default: most recent Bundestag mandate only.")] = False,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict:
        """Declared side jobs and outside income of a Bundestag member.

        Covers the Bundestag ONLY - state parliament and EU members return coverage='not_available',
        which is not the same as 'no side jobs'. Income is published as a level 0-10 range
        (level 3 = 7.000-15.000 EUR); an exact amount is present only for some entries, and many
        entries have no income at all (not reported, which does not mean zero). End dates appear
        only inside titles ('date_hint'). Report neutrally; do not infer motives.
        """
        s = new_session(5)
        mandates = (await load_mandates(s, [politician_id], types=("mandate",)))[politician_id]
        if not mandates:
            raise AWUserError(f"Politician {politician_id} has no mandates on record. Check the id with aw_find_politician.")
        bundestag = [m for m in mandates if m.get("parliament_id") == BUNDESTAG_ID]
        if not bundestag:
            places = ", ".join(sorted({m.get("parliament") or "?" for m in mandates}))
            return envelope(
                "Side-job data is not available for this politician (it covers Bundestag members only).",
                {"coverage": "not_available", "politician_id": politician_id, "mandates_in": places, "sidejobs": []},
                session=s,
                notes=[f"This politician only sat in: {places}. Abgeordnetenwatch publishes side jobs for the Bundestag only. This does NOT mean there are none."],
            )
        scope = bundestag if all_mandates else bundestag[:1]
        s.budget = 3 + len(scope)

        raw: list[dict] = []
        for m in scope:
            res = await s.list_all("sidejob", {"mandates": m["mandate_id"]}, TTL_ENTITY, max_items=1000)
            raw.extend(res.items)
        seen: set[int] = set()
        jobs = [decode_sidejob(j) for j in raw if not (j["id"] in seen or seen.add(j["id"]))]

        if category is not None:
            jobs = [j for j in jobs if (j.get("category") or {}).get("id") == category]
        if min_income_level is not None:
            jobs = [j for j in jobs if (j.get("income_level") is not None and j["income_level"] >= min_income_level)]
        jobs.sort(key=lambda j: (j.get("income_level") is not None, j.get("income_level") or 0, j.get("last_changed") or ""), reverse=True)

        with_income = [j for j in jobs if j.get("income_level") is not None or j.get("income_exact") is not None]
        levels = [j["income_level"] for j in jobs if j.get("income_level") is not None]
        by_cat = Counter((j.get("category") or {}).get("label") or "uncategorised" for j in jobs)
        summary_block = {
            "total": len(jobs),
            "income_reported": len(with_income),
            "income_not_reported": len(jobs) - len(with_income),
            "highest_income_level": max(levels) if levels else None,
            "highest_income_level_range": income_level_text(max(levels)) if levels else None,
            "by_category": dict(by_cat.most_common()),
        }
        shown = jobs[:limit]
        notes = [
            "'income_not_reported' means no income figure was published for the entry, not that it was unpaid.",
            "Income levels are the Bundestagsverwaltung's ranges; they are not exact amounts.",
        ]
        if not (all_mandates) and len(bundestag) > 1:
            notes.append(f"Only the most recent Bundestag mandate ({scope[0]['period']}) is included; use all_mandates=true for earlier terms.")
        if len(jobs) > limit:
            notes.append(f"Showing {limit} of {len(jobs)}.")
        data = {"politician_id": politician_id, "coverage": "bundestag", "mandate": {"mandate_id": scope[0]["mandate_id"], "period": scope[0]["period"]}, "summary": summary_block, "sidejobs": shown}
        text = f"{len(jobs)} declared side job(s); income reported for {len(with_income)}" + (f", highest level {max(levels)} ({income_level_text(max(levels))})" if levels else "")
        return envelope(text, data, session=s, page=page_info(len(jobs), len(shown)), notes=notes)

    @mcp.tool(name="aw_committees", annotations=READ_ONLY)
    @tool_errors
    async def aw_committees(
        politician_id: Annotated[int | None, Field(gt=0, description="List this MP's committee memberships.")] = None,
        committee_id: Annotated[int | None, Field(gt=0, description="List the members of this committee.")] = None,
        parliament: Annotated[str | None, Field(description="Browse committees of this parliament (name, alias or id).")] = None,
        keyword: Annotated[str | None, Field(description="Browse committees whose name contains this text.")] = None,
        all_mandates: Annotated[bool, Field(description="With politician_id: include earlier mandates, not just the most recent.")] = False,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict:
        """Committee memberships. Use exactly ONE mode: politician_id (their memberships), committee_id
        (its members, with politician ids) or parliament/keyword (browse committees)."""
        modes = [politician_id is not None, committee_id is not None, bool(parliament or keyword)]
        if sum(modes) != 1:
            raise AWUserError("Use exactly one mode: politician_id, committee_id, or parliament/keyword.")
        s = new_session(6)

        if politician_id is not None:
            mandates = (await load_mandates(s, [politician_id], types=("mandate",)))[politician_id]
            if not mandates:
                raise AWUserError(f"Politician {politician_id} has no mandates on record.")
            scope = mandates if all_mandates else mandates[:1]
            s.budget = 3 + len(scope)
            rows = []
            for m in scope:
                res = await s.list_all("committee_membership", {"candidacy_mandate": m["mandate_id"]}, TTL_ENTITY, max_items=500)
                for cm in res.items:
                    committee = _first(cm.get("committee"))
                    rows.append({
                        "committee_id": committee.get("id"),
                        "committee": committee.get("label"),
                        "role": COMMITTEE_ROLES.get(cm.get("committee_role"), cm.get("committee_role")),
                        "additional_roles": [COMMITTEE_ROLES.get(r, r) for r in (cm.get("committee_roles_additional") or [])],
                        "parliament": m["parliament"],
                        "period": m["period"],
                        "url": committee.get("abgeordnetenwatch_url"),
                    })
            notes = [] if rows else ["No committee memberships recorded for this scope."]
            if not all_mandates and len(mandates) > 1:
                notes.append(f"Only the most recent mandate ({scope[0]['period']}) is included; use all_mandates=true for earlier ones.")
            return envelope(f"{len(rows)} committee membership(s)", rows[:limit], session=s, page=page_info(len(rows), min(len(rows), limit)), notes=notes, sources=[r["url"] for r in rows[:3]])

        if committee_id is not None:
            committee = await s.get_one("committee", committee_id, ttl=TTL_ENTITY)
            res = await s.list_all("committee_membership", {"committee": committee_id}, TTL_ENTITY, max_items=500)
            mandate_ids = [_first(cm.get("candidacy_mandate")).get("id") for cm in res.items]
            who: dict[int, dict] = {}
            for i in range(0, len(mandate_ids), 100):
                chunk = [m for m in mandate_ids[i:i + 100] if m]
                if chunk:
                    got = await s.list("candidacy_mandate", {"id[in]": chunk, "current_on": "all", "range_end": 100}, TTL_ENTITY)
                    for m in got.items:
                        memberships = m.get("fraction_membership") or []
                        who[m["id"]] = {"politician_id": _first(m.get("politician")).get("id"), "faction": memberships[-1].get("label") if memberships else None}
            members = []
            for cm in res.items:
                mid = _first(cm.get("candidacy_mandate")).get("id")
                members.append({
                    "name": clean_fraction_name(_first(cm.get("candidacy_mandate")).get("label")),
                    "politician_id": who.get(mid, {}).get("politician_id"),
                    "faction": who.get(mid, {}).get("faction"),
                    "role": COMMITTEE_ROLES.get(cm.get("committee_role"), cm.get("committee_role")),
                    "additional_roles": [COMMITTEE_ROLES.get(r, r) for r in (cm.get("committee_roles_additional") or [])],
                })
            members.sort(key=lambda m: (m["role"] == "Member", m["faction"] or "", m["name"] or ""))
            data = {"committee": {"committee_id": committee_id, "label": committee.get("label"), "period": _first(committee.get("field_legislature")).get("label"), "topics": [t.get("label") for t in (committee.get("field_topics") or [])]}, "members": members[:limit]}
            return envelope(f"{committee.get('label')}: {len(members)} member(s)", data, session=s, page=page_info(len(members), min(len(members), limit)), sources=[committee.get("abgeordnetenwatch_url")])

        params: dict[str, Any] = {"label[cn]": keyword, "range_end": limit}
        if parliament:
            params["field_legislature[entity.parliament.entity.id]"] = (await resolve_parliament(s, parliament))["id"]
        res = await s.list("committee", params, TTL_ENTITY)
        rows = [
            {"committee_id": c["id"], "label": c.get("label"), "period": _first(c.get("field_legislature")).get("label"),
             "period_id": _first(c.get("field_legislature")).get("id"), "topics": [t.get("label") for t in (c.get("field_topics") or [])], "url": c.get("abgeordnetenwatch_url")}
            for c in res.items
        ]
        return envelope(f"{res.total} committee(s) match", rows, session=s, page=page_info(res.total, len(rows)), sources=[r["url"] for r in rows[:3]])
