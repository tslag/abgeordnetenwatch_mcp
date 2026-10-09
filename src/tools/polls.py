"""aw_search_polls and aw_poll_result."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from aw.client import AWSession
from aw.endpoints import TTL_LIVE
from aw.envelope import ROLL_CALL_NOTE, envelope, page_info, tool_errors
from aw.errors import AWUserError
from aw.mandates import load_mandates
from aw.polls import clean_fraction_name, slim_poll, tally_votes
from aw.reference import _first, periods, resolve_parliament, resolve_topic_ids
from tools.common import READ_ONLY, new_session


def register_poll_tools(mcp: FastMCP) -> None:
    @mcp.tool(name="aw_search_polls", annotations=READ_ONLY)
    @tool_errors
    async def aw_search_polls(
        keyword: Annotated[str | None, Field(description='Substring of the poll TITLE (not the description), e.g. "Bürgergeld", "Steuer". Use one or two short German words.')] = None,
        topic: Annotated[str | None, Field(description="Topic name or id (see aw_lookup kind='topic'), e.g. 'Energie'.")] = None,
        parliament: Annotated[str | None, Field(description="Name, alias ('Bundestag', 'NRW', 'EU') or id.")] = None,
        period_id: Annotated[int | None, Field(gt=0, description="Legislature period id (aw_lookup kind='period').")] = None,
        since: Annotated[date | None, Field(description="Only polls on/after this date (YYYY-MM-DD).")] = None,
        until: Annotated[date | None, Field(description="Only polls on/before this date (YYYY-MM-DD).")] = None,
        accepted: Annotated[bool | None, Field(description="True = motion adopted, False = rejected.")] = None,
        sort: Annotated[Literal["newest", "oldest"], Field(description="By poll date.")] = "newest",
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict:
        """Find roll-call polls (namentliche Abstimmungen) in the Bundestag, EU Parliament and state parliaments.

        Returns poll ids, dates, topics and whether the motion was adopted. Feed a poll_id into
        aw_poll_result to see how each faction voted. At least one filter is recommended; with no
        filter you get the most recent polls across all 18 parliaments.
        """
        s = new_session(5)
        params: dict[str, Any] = {
            "label[cn]": keyword,
            "field_legislature": period_id,
            "field_poll_date[gte]": since.isoformat() if since else None,
            "field_poll_date[lte]": until.isoformat() if until else None,
            "field_accepted": accepted,
            "sort_by": "field_poll_date",
            "sort_direction": "desc" if sort == "newest" else "asc",
            "range_start": offset,
            "range_end": limit,
        }
        if topic:
            matches = await resolve_topic_ids(s, topic)
            if len(matches) > 1:
                names = "; ".join(f"{t['label']} (id {t['id']})" for t in matches[:10])
                raise AWUserError(f"Topic '{topic}' is ambiguous: {names}. Pass one id.")
            params["field_topics"] = matches[0]["id"]
        if parliament:
            params["field_legislature[entity.parliament.entity.id]"] = (await resolve_parliament(s, parliament))["id"]

        res = await s.list("poll", params, TTL_LIVE)
        period_idx = await periods(s)
        rows = [slim_poll(p, period_idx) for p in res.items]
        notes = []
        if keyword and not rows:
            notes.append("No title contains that keyword. Try a shorter or different German word, or search by topic/date instead.")
        if res.total > offset + len(rows):
            notes.append(f"{res.total - offset - len(rows)} more polls match; raise offset to page on.")
        return envelope(
            f"{res.total} poll(s) match" + (f"; showing {len(rows)} ({sort} first)" if rows else ""),
            rows,
            session=s,
            page=page_info(res.total, len(rows), offset),
            notes=notes,
            sources=[r["url"] for r in rows[:3]],
        )

    @mcp.tool(name="aw_poll_result", annotations=READ_ONLY)
    @tool_errors
    async def aw_poll_result(
        poll_id: Annotated[int, Field(gt=0, description="Poll id from aw_search_polls or aw_politician_votes.")],
        politician_id: Annotated[int | None, Field(gt=0, description="Also report this MP's vote and whether it followed their faction's majority.")] = None,
        include_votes: Annotated[Literal["none", "dissenters", "all"], Field(description="List individual MPs: only those who voted against their faction's majority, or everyone (capped at 200).")] = "none",
    ) -> dict:
        """Result of one roll-call poll, computed per faction from the individual votes.

        Gives overall yes/no/abstain/no_show counts and, for each faction, its counts and majority
        position. With politician_id it shows that MP's vote and whether they deviated from their
        faction - the most informative signal when comparing votes against a party programme,
        because a vote that merely follows the party line says little about personal conviction.
        """
        s = new_session(5)
        summary, data, notes, sources = await build_poll_result(s, poll_id, politician_id, include_votes)
        return envelope(summary, data, session=s, notes=notes, sources=sources)


async def build_poll_result(s: AWSession, poll_id: int, politician_id: int | None = None, include_votes: str = "none") -> tuple[str, dict, list[str], list[str | None]]:
    """Poll metadata + per-faction tally (+ optional MP block); shared with the aw://poll/{id} resource."""
    poll = await s.get_one("poll", poll_id, {"related_data": "votes"}, ttl=3600)
    votes = (poll.get("related_data") or {}).get("votes") or []
    period_idx = await periods(s)
    tally = tally_votes(votes)
    majority = {f["fraction"]: f["majority_position"] for f in tally["by_fraction"]}

    data: dict[str, Any] = {"poll": slim_poll(poll, period_idx, intro_chars=1200), **tally}
    notes = [ROLL_CALL_NOTE, "Counts are computed from the individual vote records, not an official total."]
    if len(votes) >= 1000:
        notes.append("The API caps related votes at 1000 - counts may be incomplete.")
    if not votes:
        notes.append("No individual votes are recorded for this poll.")

    if politician_id is not None:
        mandates = (await load_mandates(s, [politician_id], types=("mandate",)))[politician_id]
        mandate_ids = {m["mandate_id"] for m in mandates}
        mine = next((v for v in votes if _first(v.get("mandate")).get("id") in mandate_ids), None)
        if mine is None:
            data["politician"] = {"politician_id": politician_id, "voted": False}
            notes.append("This politician has no vote record in this poll (no mandate then, or not part of the roll call).")
        else:
            fraction = clean_fraction_name(_first(mine.get("fraction")).get("label"))
            position = mine.get("vote")
            faction_majority = majority.get(fraction)
            data["politician"] = {
                "politician_id": politician_id,
                "voted": position != "no_show",
                "vote": position,
                "reason_no_show": mine.get("reason_no_show"),
                "reason_no_show_other": mine.get("reason_no_show_other"),
                "fraction": fraction,
                "fraction_majority": faction_majority,
                "followed_fraction_majority": (position == faction_majority) if (position != "no_show" and faction_majority) else None,
            }

    if include_votes != "none":
        rows = []
        for v in votes:
            fraction = clean_fraction_name(_first(v.get("fraction")).get("label"))
            position = v.get("vote")
            dissent = position in ("yes", "no", "abstain") and majority.get(fraction) not in (None, position)
            if include_votes == "dissenters" and not dissent:
                continue
            rows.append({
                "mandate_id": _first(v.get("mandate")).get("id"),
                "name": clean_fraction_name(_first(v.get("mandate")).get("label")),
                "fraction": fraction,
                "vote": position,
                "fraction_majority": majority.get(fraction),
            })
        rows.sort(key=lambda r: (r["fraction"], r["name"] or ""))
        data["votes"] = rows[:200]
        if len(rows) > 200:
            notes.append(f"Listing capped at 200 of {len(rows)} entries.")

    o = tally["overall"]
    outcome = {True: "adopted", False: "rejected", None: "outcome unknown"}[data["poll"].get("accepted")]
    summary = f"{data['poll']['title']} ({data['poll'].get('date')}): {outcome}; yes {o['yes']}, no {o['no']}, abstain {o['abstain']}, not voting {o['no_show']}"
    return summary, data, notes, [data["poll"].get("url")]
