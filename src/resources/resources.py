"""MCP resources: guide, coverage, codebook and reference data (cached, no per-call cost)."""
from __future__ import annotations

import json

from fastmcp import FastMCP

from aw.endpoints import (
    COMMITTEE_ROLES,
    CURRENT_ON_NOTE,
    ENTITY_TYPES,
    INCOME_LEVELS,
    MANDATE_WON,
    REASON_NO_SHOW,
    SIDEJOB_CATEGORIES,
    SIDEJOB_INTERVALS,
    VOTE_VALUES,
    income_level_text,
)
from aw.envelope import tool_errors
from aw.reference import parliaments, periods, topics
from resources.prompts import GUIDE_PROMPT, LICENCE_COVERAGE_PROMPT
from tools.common import new_session
from tools.politicians import build_politician
from tools.polls import build_poll_result


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def register_resources(mcp: FastMCP) -> None:
    @mcp.resource(
        uri="aw://guide",
        name="Abgeordnetenwatch MCP guide",
        description="How to use the tools: routing table, recipes (incl. votes vs. party programme), pitfalls, limits.",
        mime_type="text/markdown",
    )
    def guide() -> str:
        return f"{GUIDE_PROMPT}\n{LICENCE_COVERAGE_PROMPT}"

    @mcp.resource(
        uri="aw://info",
        name="Abgeordnetenwatch MCP server info",
        description="Server, data source and licence metadata.",
        mime_type="application/json",
    )
    def info() -> dict:
        return {
            "name": "Abgeordnetenwatch MCP Server",
            "description": "Open transparency data on German MPs: roll-call votes, side jobs, committees, parties, election programmes",
            "dataSource": {"name": "Abgeordnetenwatch API v2", "licence": "CC0 1.0", "url": "https://www.abgeordnetenwatch.de/api"},
            "coverage": "Bundestag, EU-Parlament, 16 state parliaments",
        }

    @mcp.resource(
        uri="aw://codebook",
        name="Code book",
        description="Decoders for coded values: vote values, no-show reasons, income levels 0-10 (EUR ranges), side-job categories and intervals, committee roles, mandate types.",
        mime_type="application/json",
    )
    def codebook() -> str:
        return _json({
            "vote_values": VOTE_VALUES,
            "reason_no_show": REASON_NO_SHOW,
            "income_levels": {str(k): income_level_text(k) for k in INCOME_LEVELS},
            "income_note": "Published by the Bundestagsverwaltung as ranges. An exact 'income' exists only for some entries; many have none (not reported, not zero).",
            "sidejob_intervals": {str(k): v for k, v in SIDEJOB_INTERVALS.items()},
            "sidejob_categories": {str(k): {"label": en, "label_de": de} for k, (en, de) in SIDEJOB_CATEGORIES.items()},
            "committee_roles": COMMITTEE_ROLES,
            "mandate_won": MANDATE_WON,
            "entry_types": ENTITY_TYPES,
            "current_on": CURRENT_ON_NOTE,
        })

    @mcp.resource(
        uri="aw://parliaments",
        name="Parliaments",
        description="The 18 covered parliaments with ids and their current legislature period.",
        mime_type="application/json",
    )
    @tool_errors
    async def parliaments_resource() -> str:
        return _json(await parliaments(new_session(2)))

    @mcp.resource(
        uri="aw://coverage",
        name="Data coverage",
        description="What is covered per dataset and parliament, with the current period of each parliament.",
        mime_type="application/json",
    )
    @tool_errors
    async def coverage() -> str:
        return _json({
            "parliaments": await parliaments(new_session(2)),
            "datasets": {
                "roll_call_votes": "Where Abgeordnetenwatch recorded namentliche Abstimmungen (Bundestag, EU, states); not all periods.",
                "sidejobs": "Bundestag members only.",
                "committees_and_mandates": "All parliaments.",
                "election_programs": "PDF per party and election (~680).",
            },
            "not_covered": ["Austria", "Switzerland", "municipal councils"],
        })

    @mcp.resource(
        uri="aw://parliament/{parliament_id}/periods",
        name="Periods of a parliament",
        description="Legislature and election periods of one parliament with dates and the previous election.",
        mime_type="application/json",
    )
    @tool_errors
    async def parliament_periods(parliament_id: int) -> str:
        idx = await periods(new_session(3))
        rows = sorted((p for p in idx.values() if p["parliament_id"] == parliament_id), key=lambda p: p["start_date"] or "", reverse=True)
        return _json(rows)

    @mcp.resource(
        uri="aw://topics",
        name="Topics",
        description="The DIP21 topic taxonomy used to tag polls and side jobs (id, label, parent).",
        mime_type="application/json",
    )
    @tool_errors
    async def topics_resource() -> str:
        return _json(await topics(new_session(2)))

    @mcp.resource(
        uri="aw://politician/{politician_id}",
        name="Politician profile",
        description="Profile and career timeline (mandates, candidacies) of one politician.",
        mime_type="application/json",
    )
    @tool_errors
    async def politician_resource(politician_id: int) -> str:
        summary, data, notes, sources = await build_politician(new_session(6), politician_id)
        return _json({"summary": summary, "data": data, "notes": notes, "sources": sources})

    @mcp.resource(
        uri="aw://poll/{poll_id}",
        name="Poll result",
        description="One roll-call poll with the per-faction tally.",
        mime_type="application/json",
    )
    @tool_errors
    async def poll_resource(poll_id: int) -> str:
        summary, data, notes, sources = await build_poll_result(new_session(5), poll_id)
        return _json({"summary": summary, "data": data, "notes": notes, "sources": sources})
