"""MCP prompts: user-invoked workflows that encode the recommended tool strategy."""
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

CAVEATS = """\
Always state these caveats:
- Only roll-call votes are recorded; a missing vote is not a position.
- A party programme is not an individual MP's personal belief; faction discipline means a vote along the party line says little about conviction.
- Name the parliament and legislature period that each statement covers.
Report neutrally, quote programme text with page numbers, cite the abgeordnetenwatch.de URLs, never infer motives."""


def register_prompts(mcp: FastMCP) -> None:
    @mcp.prompt(name="compare_votes_with_program", description="Compare an MP's recent roll-call votes with the election programme of the party they ran for.")
    def compare_votes_with_program(
        politician: Annotated[str, Field(description='Name of the MP, e.g. "Olaf Scholz".')],
        topic: Annotated[str | None, Field(description="Optional focus topic, e.g. 'Energie' or 'Rente'.")] = None,
        since: Annotated[str | None, Field(description="Optional start date YYYY-MM-DD for the votes.")] = None,
    ) -> str:
        focus = f" Focus on the topic: {topic}." if topic else " Pick the 3-5 most prominent themes of the programme."
        window = f" Only consider polls since {since}." if since else " Consider the most recent mandate."
        return f"""Analyse whether the recent roll-call voting of {politician} matches the election programme of their party.{focus}{window}

Steps:
1. aw_find_politician("{politician}"). If needs_user_choice is true, ask me which person is meant before continuing.
2. aw_get_politician(politician_id) to see mandates, factions and the party they ran for.
3. aw_find_election_programs(politician_id) for the programme of the election that led to the mandate.
4. For each theme: aw_read_election_program(program_id, query=<theme words>) and note the position with its page number.
5. For the same theme: aw_politician_votes(politician_id, topic or keyword{', since' if since else ''}).
6. For the 1-3 most relevant polls per theme: aw_poll_result(poll_id, politician_id) and note whether the MP followed their faction.
7. Present a table per theme: programme position (quote, page) | votes (poll, date, vote) | verdict: consistent / inconsistent / no roll-call evidence.

{CAVEATS}"""

    @mcp.prompt(name="mp_overview", description="Neutral, cited overview of an MP: mandates, committees, side jobs and recent votes.")
    def mp_overview(politician: Annotated[str, Field(description='Name of the MP.')]) -> str:
        return f"""Write a neutral overview of {politician}.

1. aw_find_politician("{politician}"); ask me if several people fit.
2. aw_get_politician: party, mandates (parliament, period, faction, how the seat was won).
3. aw_committees(politician_id): committee roles.
4. aw_get_sidejobs(politician_id): categories, highest income level, how many entries have no income reported (Bundestag only).
5. aw_politician_votes(politician_id, limit=10): the latest roll-call votes.

Keep to facts from the data and cite the abgeordnetenwatch.de URLs. {CAVEATS}"""

    @mcp.prompt(name="explain_vote", description="Explain how a parliamentary vote went: result, factions, and who deviated.")
    def explain_vote(keyword: Annotated[str, Field(description='Words from the poll title, e.g. "Bürgergeld".')], parliament: Annotated[str | None, Field(description="Optional parliament, e.g. 'Bundestag'.")] = None) -> str:
        where = f", parliament='{parliament}'" if parliament else ""
        return f"""Explain the vote about "{keyword}".

1. aw_search_polls(keyword="{keyword}"{where}, limit=5). If several polls fit, list them and ask me which one.
2. aw_poll_result(poll_id, include_votes="dissenters"): overall result, each faction's position and the members who deviated from their faction.
3. Summarise: what was voted on (use the poll description), whether it was adopted, how each faction voted, notable dissent.

{CAVEATS}"""

    @mcp.prompt(name="party_positions_on_topic", description="Show how each faction voted on recent polls of a topic.")
    def party_positions_on_topic(
        topic: Annotated[str, Field(description="Topic name, e.g. 'Energie'.")],
        parliament: Annotated[str, Field(description="Parliament, e.g. 'Bundestag'.")] = "Bundestag",
        since: Annotated[str | None, Field(description="Optional start date YYYY-MM-DD.")] = None,
    ) -> str:
        window = f", since='{since}'" if since else ""
        return f"""Compare the factions' positions on "{topic}" in the {parliament}.

1. aw_search_polls(topic="{topic}", parliament="{parliament}"{window}, limit=8).
2. For each poll: aw_poll_result(poll_id) and record each faction's majority position.
3. Present a matrix: polls (date, title, adopted?) x factions (yes / no / abstain / split).

{CAVEATS}"""
