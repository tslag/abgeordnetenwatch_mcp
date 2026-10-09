"""Poll slimming and server-side vote tallies (the API has no aggregate endpoint)."""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .reference import _first
from .text import strip_html

POSITIONS = ("yes", "no", "abstain")
_PERIOD_SUFFIX = re.compile(r"\s*\([^()]*\d{4}[^()]*\)\s*$")


def clean_fraction_name(label: str | None) -> str:
    """'SPD (Bundestag 2025 - 2029)' -> 'SPD' (also drops soft hyphens the API embeds)."""
    if not label:
        return "unknown"
    return _PERIOD_SUFFIX.sub("", label.replace("\xad", "")).strip() or label


def slim_poll(p: dict, period_idx: dict[int, dict] | None = None, *, intro_chars: int | None = 280) -> dict:
    """Compact poll record. ``intro_chars=None`` drops the intro, 0 keeps it in full."""
    period_ref = _first(p.get("field_legislature"))
    pinfo = (period_idx or {}).get(period_ref.get("id"), {})
    out: dict[str, Any] = {
        "poll_id": p["id"],
        "title": p.get("label"),
        "date": p.get("field_poll_date"),
        "accepted": p.get("field_accepted"),
        "parliament": pinfo.get("parliament"),
        "parliament_id": pinfo.get("parliament_id"),
        "period_id": period_ref.get("id"),
        "period": period_ref.get("label"),
        "topics": [{"id": t["id"], "label": t.get("label")} for t in (p.get("field_topics") or [])],
        "committee": _first(p.get("field_committees")).get("label"),
        "url": p.get("abgeordnetenwatch_url"),
    }
    if intro_chars is not None:
        out["intro"] = strip_html(p.get("field_intro"), None if intro_chars == 0 else intro_chars)
    return out


def _blank() -> dict[str, int]:
    return {"yes": 0, "no": 0, "abstain": 0, "no_show": 0}


def tally_votes(votes: Iterable[dict]) -> dict:
    """Overall and per-faction counts from raw vote records.

    ``majority_position`` is the most common position among members who actually voted
    (yes/no/abstain); it is None when nobody voted or the top positions are tied.
    """
    overall = _blank()
    per_fraction: dict[str, dict[str, int]] = {}
    for v in votes:
        value = v.get("vote")
        if value not in overall:
            continue
        name = clean_fraction_name(_first(v.get("fraction")).get("label"))
        overall[value] += 1
        per_fraction.setdefault(name, _blank())[value] += 1

    fractions = []
    for name, counts in sorted(per_fraction.items(), key=lambda kv: -sum(kv[1].values())):
        fractions.append({"fraction": name, **counts, "members": sum(counts.values()), **faction_position(counts)})
    return {"overall": {**overall, "members": sum(overall.values())}, "by_fraction": fractions}


def faction_position(counts: dict[str, int]) -> dict:
    voted = sum(counts[p] for p in POSITIONS)
    if voted == 0:
        return {"majority_position": None, "majority_share": None, "unanimous": None}
    ranked = sorted(((counts[p], p) for p in POSITIONS), reverse=True)
    top, runner_up = ranked[0], ranked[1]
    majority = None if top[0] == runner_up[0] else top[1]
    return {
        "majority_position": majority,
        "majority_share": round(top[0] / voted, 3),
        "unanimous": runner_up[0] == 0,
    }
