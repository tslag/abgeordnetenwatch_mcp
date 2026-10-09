"""Record builders mirroring the real response shapes captured from the live API (API v2.9.0)."""
from __future__ import annotations

from typing import Any

API = "https://www.abgeordnetenwatch.de/api/v2/"


def ref(entity_type: str, id_: int, label: str, **extra: Any) -> dict:
    return {"id": id_, "entity_type": entity_type, "label": label, "api_url": f"{API}x/{id_}", **extra}


def politician(id_: int, first: str, last: str, party: tuple[int, str] | None = (1, "SPD"), **extra: Any) -> dict:
    return {
        "id": id_, "entity_type": "politician", "label": f"{first} {last}", "first_name": first, "last_name": last,
        "party": ref("party", *party) if party else None, "year_of_birth": 1958, "occupation": "Test",
        "abgeordnetenwatch_url": f"https://www.abgeordnetenwatch.de/profile/{first.lower()}-{last.lower()}", "field_title": None,
        "statistic_questions": "3", "statistic_questions_answered": None, "qid_wikidata": None, "ext_id_bundestagsverwaltung": None,
        "sex": "m", "party_past": None, "education": None, "residence": None, **extra,
    }


def mandate(id_: int, pol_id: int, pol_name: str, period: tuple[int, str], *, type_: str = "mandate", faction: str | None = "SPD",
            party: tuple[int, str] | None = None, end_date: str | None = None, won: str | None = "list") -> dict:
    return {
        "id": id_, "entity_type": "candidacy_mandate", "label": f"{pol_name} ({period[1]})", "type": type_,
        "parliament_period": ref("parliament_period", *period), "politician": ref("politician", pol_id, pol_name),
        "party": ref("party", *party) if party else None, "start_date": None, "end_date": end_date,
        "fraction_membership": [{"id": id_ * 10, "label": faction, "fraction": ref("fraction", 400 + id_, f"{faction} ({period[1]})"), "valid_from": None, "valid_until": None}] if (faction and type_ == "mandate") else None,
        "electoral_data": {"mandate_won": won, "list_position": 1, "constituency": None, "constituency_result": None, "electoral_list": None} if type_ == "mandate" else {"mandate_won": None, "list_position": 1, "constituency": None, "constituency_result": None, "electoral_list": None},
    }


def period(id_: int, label: str, parliament: tuple[int, str], *, type_: str = "legislature", start: str | None = None, end: str | None = None,
           previous: tuple[int, str] | None = None, election_date: str | None = None) -> dict:
    return {
        "id": id_, "entity_type": "parliament_period", "label": label, "type": type_, "parliament": ref("parliament", *parliament),
        "previous_period": ref("parliament_period", *previous) if previous else None, "election_date": election_date,
        "start_date_period": start, "end_date_period": end, "abgeordnetenwatch_url": f"https://www.abgeordnetenwatch.de/p/{id_}",
    }


def parliament(id_: int, label: str, current: tuple[int, str]) -> dict:
    return {"id": id_, "entity_type": "parliament", "label": label, "label_external_long": label, "abgeordnetenwatch_url": f"https://www.abgeordnetenwatch.de/{label.lower()}",
            "current_project": ref("parliament_period", *current)}


def poll(id_: int, label: str, date: str, period_: tuple[int, str], *, topics: list[tuple[int, str]] = (), accepted: bool = True, intro: str = "<p>Intro <b>text</b></p>") -> dict:
    return {
        "id": id_, "entity_type": "node", "label": label, "field_poll_date": date, "field_accepted": accepted, "field_intro": intro,
        "field_legislature": ref("parliament_period", *period_), "field_committees": None,
        "field_topics": [ref("taxonomy_term", t[0], t[1]) for t in topics],
        "abgeordnetenwatch_url": f"https://www.abgeordnetenwatch.de/bundestag/21/abstimmungen/{id_}",
    }


def vote(id_: int, mandate_id: int, mandate_label: str, poll_id: int, value: str, fraction: str, *, poll_label: str = "Poll") -> dict:
    return {
        "id": id_, "entity_type": "vote", "label": f"{mandate_label} - {poll_label}", "vote": value, "reason_no_show": None, "reason_no_show_other": None,
        "mandate": ref("candidacy_mandate", mandate_id, mandate_label), "poll": ref("node", poll_id, poll_label),
        "fraction": ref("fraction", 400 + mandate_id, fraction),
    }


def topic(id_: int, label: str, parent: tuple[int, str] | None = None) -> dict:
    return {"id": id_, "entity_type": "taxonomy_term", "label": label, "abgeordnetenwatch_url": f"https://www.abgeordnetenwatch.de/themen-dip21/{id_}",
            "description": None, "parent": [ref("taxonomy_term", *parent)] if parent else None}
