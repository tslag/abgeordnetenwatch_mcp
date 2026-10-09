"""aw_lookup: resolve names to ids and browse small reference sets."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from aw.endpoints import TTL_ENTITY, TTL_REFERENCE
from aw.envelope import envelope, page_info, tool_errors
from aw.errors import AWUserError
from aw.reference import (
    _first,
    parliaments,
    periods,
    resolve_parliament,
    topics,
)
from tools.common import READ_ONLY, new_session

Kind = Literal["parliament", "period", "party", "fraction", "topic", "committee", "constituency"]


def register_lookup_tools(mcp: FastMCP) -> None:
    @mcp.tool(name="aw_lookup", annotations=READ_ONLY)
    @tool_errors
    async def aw_lookup(
        kind: Annotated[Kind, Field(description="What to look up.")],
        query: Annotated[str | None, Field(description="Name fragment, case-insensitive 'contains'. Omit to list (parliaments, periods and topics are small).")] = None,
        parliament: Annotated[str | None, Field(description="Restrict to one parliament: name, alias ('NRW', 'EU') or id. Applies to period, fraction, committee.")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict:
        """Turn a name into an Abgeordnetenwatch id, or browse a small reference list.

        Use this to get ids for other tools: parliament ids, election/legislature period ids
        (period lists show type, dates and the previous election), topic ids (53 DIP21 topics, with
        parent topic), party, fraction, committee and constituency ids. Covers 18 parliaments:
        Bundestag, EU-Parlament and the 16 state parliaments.
        """
        s = new_session(4)
        q = (query or "").strip()
        parl = await resolve_parliament(s, parliament) if parliament else None

        rows: list[dict[str, Any]]
        total: int
        if kind == "parliament":
            rows = [p for p in await parliaments(s) if not q or q.casefold() in f"{p['name']} {p['full_name']}".casefold()]
            total = len(rows)
        elif kind == "period":
            all_periods = sorted((await periods(s)).values(), key=lambda p: (p["start_date"] or "", p["id"]), reverse=True)
            rows = [
                p for p in all_periods
                if (not q or q.casefold() in (p["label"] or "").casefold()) and (parl is None or p["parliament_id"] == parl["id"])
            ]
            total = len(rows)
        elif kind == "topic":
            rows = [t for t in await topics(s) if not q or q.casefold() in (t["label"] or "").casefold()]
            total = len(rows)
        elif kind == "party":
            res = await s.list("party", {"label[cn]": q or None, "sort_by": "label", "sort_direction": "asc", "range_end": limit}, TTL_REFERENCE)
            rows = [{"id": p["id"], "label": p.get("label"), "full_name": p.get("full_name")} for p in res.items]
            total = res.total
        elif kind == "fraction":
            params: dict[str, Any] = {"label[cn]": q or None, "range_end": limit}
            if parl:
                params["legislature[entity.parliament.entity.id]"] = parl["id"]
            res = await s.list("fraction", params, TTL_REFERENCE)
            rows = [
                {"id": f["id"], "label": f.get("label"), "short_name": f.get("short_name"), "period": _first(f.get("legislature")).get("label"), "period_id": _first(f.get("legislature")).get("id")}
                for f in res.items
            ]
            total = res.total
        elif kind == "committee":
            params = {"label[cn]": q or None, "range_end": limit}
            if parl:
                params["field_legislature[entity.parliament.entity.id]"] = parl["id"]
            res = await s.list("committee", params, TTL_ENTITY)
            rows = [
                {"id": c["id"], "label": c.get("label"), "period": _first(c.get("field_legislature")).get("label"), "period_id": _first(c.get("field_legislature")).get("id"),
                 "topics": [t.get("label") for t in (c.get("field_topics") or [])], "url": c.get("abgeordnetenwatch_url")}
                for c in res.items
            ]
            total = res.total
        else:  # constituency
            if not q:
                raise AWUserError("kind='constituency' needs a query (there are over 12,000 constituency records).")
            res = await s.list("constituency", {"label[cn]": q, "range_end": limit}, TTL_REFERENCE)
            rows = [{"id": c["id"], "label": c.get("label")} for c in res.items]
            total = res.total

        shown = rows[:limit]
        notes = []
        if total > len(shown):
            notes.append(f"{total} matches, showing {len(shown)}. Add a query to narrow down.")
        if kind == "topic":
            notes.append("Topics are the DIP21 taxonomy; sub-topics have parent_id set. Filtering polls by a parent topic does not include its sub-topics.")
        return envelope(
            f"{len(shown)} of {total} {kind} result(s)" + (f" for '{q}'" if q else ""),
            shown,
            session=s,
            page=page_info(total, len(shown)),
            notes=notes,
        )
