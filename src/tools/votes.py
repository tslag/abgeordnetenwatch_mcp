"""aw_politician_votes (core composite) and aw_compare_votes.

The API's ``votes`` rows carry neither date nor topic, and ``votes`` cannot be filtered by poll
fields (HTTP 500). A voting record is therefore built as a join, one legislature period at a time:

    votes of the mandate (1 request)  JOIN  polls of that period, filtered by topic/date/title (1 request)
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from aw.client import AWSession
from aw.endpoints import TTL_CLOSED, TTL_LIVE
from aw.envelope import ROLL_CALL_NOTE, envelope, page_info, tool_errors
from aw.errors import AWUserError
from aw.mandates import load_mandates
from aw.polls import clean_fraction_name, slim_poll
from aw.reference import _first, periods, resolve_topic_ids
from tools.common import READ_ONLY, new_session

POSITIONS = ("yes", "no", "abstain")


async def _resolve_single_topic(s: AWSession, topic: str | None) -> dict | None:
    if not topic:
        return None
    matches = await resolve_topic_ids(s, topic)
    if len(matches) > 1:
        names = "; ".join(f"{t['label']} (id {t['id']})" for t in matches[:10])
        raise AWUserError(f"Topic '{topic}' is ambiguous: {names}. Pass one topic id.")
    return matches[0]


def choose_scope(mandates: list[dict], period_id: int | None, all_periods: bool, politician_id: int) -> list[dict]:
    """Default scope is the most recent mandate; widen with all_periods or pick a period_id."""
    if not mandates:
        raise AWUserError(
            f"Politician {politician_id} has no mandates on record, so there are no roll-call votes. "
            "Check the id with aw_find_politician / aw_get_politician."
        )
    if period_id is not None:
        scope = [m for m in mandates if m["period_id"] == period_id]
        if not scope:
            have = ", ".join(f"{m['period']} (period_id {m['period_id']})" for m in mandates)
            raise AWUserError(f"Politician {politician_id} had no mandate in period {period_id}. Available: {have}.")
        return scope
    return list(mandates) if all_periods else mandates[:1]


async def collect_vote_rows(
    s: AWSession,
    politician_id: int,
    scope: list[dict],
    *,
    topic_id: int | None = None,
    keyword: str | None = None,
    since: date | None = None,
    until: date | None = None,
) -> list[dict]:
    """Join the votes of ``scope`` (mandates of one politician) with their polls."""
    period_idx = await periods(s)
    poll_filtered = any(v is not None for v in (topic_id, keyword, since, until))

    ttl = TTL_LIVE if any(m.get("is_current") for m in scope) else TTL_CLOSED
    if len(scope) == 1:
        vote_params: dict[str, Any] = {"mandate": scope[0]["mandate_id"]}
    else:  # several mandates = all of the person's: one request via the reference chain (verified live)
        vote_params = {"mandate[entity.politician.entity.id]": politician_id}

    async def polls_of(period: int) -> list[dict]:
        params: dict[str, Any] = {
            "field_legislature": period,
            "field_topics": topic_id,
            "label[cn]": keyword,
            "field_poll_date[gte]": since.isoformat() if since else None,
            "field_poll_date[lte]": until.isoformat() if until else None,
            "sort_by": "field_poll_date",
            "sort_direction": "desc",
        }
        return (await s.list_all("poll", params, ttl, max_items=3000)).items

    # votes and the polls of every period are independent: fetch them concurrently
    # (the client's rate limiter still spaces the actual HTTP requests)
    period_ids = list(dict.fromkeys(m["period_id"] for m in scope))
    vote_result, *poll_lists = await asyncio.gather(
        s.list_all("vote", vote_params, ttl, max_items=5000),
        *(polls_of(pid) for pid in period_ids),
    )
    votes = vote_result.items
    polls_by_id: dict[int, dict] = {}
    for items in poll_lists:
        for p in items:
            polls_by_id[p["id"]] = slim_poll(p, period_idx, intro_chars=None)

    rows = []
    for v in votes:
        poll_id = _first(v.get("poll")).get("id")
        poll = polls_by_id.get(poll_id)
        if poll is None:
            if poll_filtered:
                continue  # poll was filtered out by topic/date/title
            poll = {"poll_id": poll_id, "title": _first(v.get("poll")).get("label"), "url": _first(v.get("poll")).get("abgeordnetenwatch_url")}
        rows.append({
            **poll,
            "vote": v.get("vote"),
            "reason_no_show": v.get("reason_no_show_other") or v.get("reason_no_show"),
            "fraction": clean_fraction_name(_first(v.get("fraction")).get("label")),
        })
    rows.sort(key=lambda r: (r.get("date") or "", r["poll_id"] or 0), reverse=True)
    return rows


def _filter_rows(rows: list[dict], vote: str | None, include_no_show: bool) -> list[dict]:
    out = rows
    if vote:
        out = [r for r in out if r["vote"] == vote]
    if not include_no_show:
        out = [r for r in out if r["vote"] != "no_show"]
    return out


def _counts(rows: list[dict]) -> dict:
    c = Counter(r["vote"] for r in rows)
    return {"yes": c["yes"], "no": c["no"], "abstain": c["abstain"], "no_show": c["no_show"], "total": len(rows)}


def register_vote_tools(mcp: FastMCP) -> None:
    @mcp.tool(name="aw_politician_votes", annotations=READ_ONLY)
    @tool_errors
    async def aw_politician_votes(
        politician_id: Annotated[int, Field(gt=0, description="Numeric id from aw_find_politician.")],
        topic: Annotated[str | None, Field(description="Topic name or id (aw_lookup kind='topic'), e.g. 'Energie'. Matches polls tagged with exactly that topic.")] = None,
        keyword: Annotated[str | None, Field(description="Substring of the poll title, e.g. 'Bürgergeld'.")] = None,
        since: Annotated[date | None, Field(description="Only polls on/after this date (YYYY-MM-DD).")] = None,
        until: Annotated[date | None, Field(description="Only polls on/before this date (YYYY-MM-DD).")] = None,
        period_id: Annotated[int | None, Field(gt=0, description="Restrict to one legislature period (see aw_get_politician).")] = None,
        all_periods: Annotated[bool, Field(description="Include every mandate (e.g. earlier Bundestag terms or another parliament). Default: only the most recent mandate.")] = False,
        vote: Annotated[Literal["yes", "no", "abstain", "no_show"] | None, Field(description="Only this vote value.")] = None,
        include_no_show: Annotated[bool, Field(description="Include polls the MP did not take part in.")] = True,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict:
        """How one MP voted in roll-call polls, newest first, with poll date, title and topics.

        By default covers the MP's most recent mandate; `summary` counts cover ALL matches, not
        just the returned page. Use topic/keyword/since to focus (e.g. recent votes on energy).
        To see whether a vote followed the party line, call aw_poll_result with this
        politician_id. Only roll-call votes exist in the data - absence is not a position.
        """
        s = new_session(4)
        mandates = (await load_mandates(s, [politician_id], types=("mandate",)))[politician_id]
        scope = choose_scope(mandates, period_id, all_periods, politician_id)
        s.budget = 6 + 2 * len(scope)  # one polls request per period on top of mandates/votes/reference data
        topic_ref = await _resolve_single_topic(s, topic)

        rows = await collect_vote_rows(
            s, politician_id, scope, topic_id=topic_ref["id"] if topic_ref else None, keyword=keyword, since=since, until=until
        )
        matched = _filter_rows(rows, vote, include_no_show)
        page = matched[offset: offset + limit]
        dates = [r["date"] for r in matched if r.get("date")]

        data = {
            "politician_id": politician_id,
            "periods_covered": [{"mandate_id": m["mandate_id"], "parliament": m["parliament"], "period": m["period"], "period_id": m["period_id"], "faction": m.get("faction")} for m in scope],
            "filters": {k: v for k, v in {"topic": topic_ref and topic_ref["label"], "keyword": keyword, "since": since and since.isoformat(), "until": until and until.isoformat(), "vote": vote}.items() if v},
            "summary": {**_counts(matched), "date_range": [min(dates), max(dates)] if dates else None},
            "votes": page,
        }
        notes = [ROLL_CALL_NOTE]
        if not (all_periods or period_id) and len(mandates) > 1:
            notes.append(f"Only the most recent mandate ({scope[0]['period']}) is included; {len(mandates) - 1} earlier mandate(s) exist - use all_periods=true or period_id.")
        if not rows:
            notes.append("No roll-call votes recorded for this MP in the selected scope (not every parliament or period has roll-call data).")
        elif not matched:
            notes.append("Votes exist in this scope but none match the filters.")
        if topic_ref:
            notes.append(f"Topic filter '{topic_ref['label']}' matches polls tagged exactly with it (sub-topics are separate topics).")
        counts = data["summary"]
        summary = f"{counts['total']} matching roll-call vote(s): yes {counts['yes']}, no {counts['no']}, abstain {counts['abstain']}, did not vote {counts['no_show']}"
        return envelope(summary, data, session=s, page=page_info(len(matched), len(page), offset), notes=notes, sources=[r.get("url") for r in page[:3]])

    @mcp.tool(name="aw_compare_votes", annotations=READ_ONLY)
    @tool_errors
    async def aw_compare_votes(
        politician_ids: Annotated[list[int], Field(min_length=2, max_length=3, description="Two or three ids from aw_find_politician.")],
        topic: Annotated[str | None, Field(description="Topic name or id.")] = None,
        keyword: Annotated[str | None, Field(description="Poll title substring.")] = None,
        since: Annotated[date | None, Field(description="Only polls on/after this date.")] = None,
        period_id: Annotated[int | None, Field(gt=0, description="Legislature period; default: the latest period all of them sat in.")] = None,
        max_disagreements: Annotated[int, Field(ge=0, le=50)] = 20,
    ) -> dict:
        """How often did two or three MPs vote the same way? Lists the polls where they differed.

        Compares yes/no/abstain only (polls where someone did not take part are excluded from the
        agreement rate). All MPs must have sat in the same legislature period.
        """
        s = new_session(6)
        mandates = await load_mandates(s, list(politician_ids), types=("mandate",))
        by_period = [{m["period_id"]: m for m in mandates[pid]} for pid in politician_ids]
        shared = set(by_period[0]).intersection(*by_period[1:])
        if period_id is not None:
            if period_id not in shared:
                raise AWUserError(f"Not all of them had a mandate in period {period_id}.")
            chosen = period_id
        elif not shared:
            raise AWUserError("These politicians never sat in the same legislature period, so there are no shared polls to compare.")
        else:
            idx = await periods(s)
            chosen = max(shared, key=lambda pid: (idx.get(pid, {}).get("start_date") or "", pid))
        s.budget = 6 + 2 * len(politician_ids)
        topic_ref = await _resolve_single_topic(s, topic)

        per_person: list[dict[int, dict]] = []
        for pid, periods_map in zip(politician_ids, by_period, strict=True):
            rows = await collect_vote_rows(
                s, pid, [periods_map[chosen]], topic_id=topic_ref["id"] if topic_ref else None, keyword=keyword, since=since
            )
            per_person.append({r["poll_id"]: r for r in rows})

        common = set(per_person[0]).intersection(*per_person[1:])
        compared, agree, differing = 0, 0, []
        for poll_id in sorted(common, key=lambda i: per_person[0][i].get("date") or "", reverse=True):
            votes = [pp[poll_id]["vote"] for pp in per_person]
            if any(v not in POSITIONS for v in votes):
                continue
            compared += 1
            if len(set(votes)) == 1:
                agree += 1
            else:
                base = per_person[0][poll_id]
                differing.append({
                    "poll_id": poll_id, "date": base.get("date"), "title": base.get("title"), "url": base.get("url"),
                    "votes": {str(pid): per_person[i][poll_id]["vote"] for i, pid in enumerate(politician_ids)},
                })
        period_label = by_period[0][chosen]["period"]
        data = {
            "period": {"id": chosen, "label": period_label},
            "politician_ids": list(politician_ids),
            "polls_compared": compared,
            "polls_agreeing": agree,
            "agreement_rate": round(agree / compared, 3) if compared else None,
            "disagreements": differing[:max_disagreements],
        }
        notes = [ROLL_CALL_NOTE, "Polls where any of them did not take part are excluded from the rate."]
        if len(differing) > max_disagreements:
            notes.append(f"Showing {max_disagreements} of {len(differing)} disagreements.")
        if not compared:
            notes.append("No shared yes/no/abstain votes in this period/filter.")
        rate = f"{agree}/{compared} ({data['agreement_rate']:.0%})" if compared else "no shared votes"
        return envelope(f"Voted alike in {rate} of shared polls ({period_label})", data, session=s, notes=notes)
