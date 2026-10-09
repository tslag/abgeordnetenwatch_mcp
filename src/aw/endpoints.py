"""Endpoint map and code books for the Abgeordnetenwatch API v2.

The API is irregular (``candidacies-mandates``, *singular* ``election-program``), so paths are
hard-coded here and never derived from entity names.
"""
from __future__ import annotations

BASE_URL = "https://www.abgeordnetenwatch.de/api/v2/"

# entity key (snake_case, as used by aw_query) -> URL path segment
ENDPOINTS: dict[str, str] = {
    "parliament": "parliaments",
    "parliament_period": "parliament-periods",
    "politician": "politicians",
    "candidacy_mandate": "candidacies-mandates",
    "poll": "polls",
    "vote": "votes",
    "party": "parties",
    "fraction": "fractions",
    "committee": "committees",
    "committee_membership": "committee-memberships",
    "topic": "topics",
    "sidejob": "sidejobs",
    "sidejob_organization": "sidejob-organizations",
    "constituency": "constituencies",
    "electoral_list": "electoral-lists",
    "election_program": "election-program",
    "country": "countries",
    "city": "cities",
}

# Operators the API accepts in ``field[op]=value`` filters.
FILTER_OPERATORS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "sw", "cn", "ew", "in", "notin"})

# --- cache lifetimes (seconds) -------------------------------------------------------------
TTL_REFERENCE = 24 * 3600  # parliaments, periods, topics, parties
TTL_ENTITY = 3600  # politicians, mandates, sidejobs, committees
TTL_CLOSED = 24 * 3600  # polls/votes of finished legislatures
TTL_LIVE = 600  # polls/votes of the running legislature

# --- code books ----------------------------------------------------------------------------
VOTE_VALUES = {
    "yes": "Voted yes (Ja)",
    "no": "Voted no (Nein)",
    "abstain": "Abstained (Enthaltung)",
    "no_show": "Did not take part (nicht beteiligt / abwesend)",
}

REASON_NO_SHOW = {
    "maternity_protection": "Maternity protection (Mutterschutz)",
    "fell_ill": "Illness (krank)",
    "other": "Other reason (see reason_no_show_other)",
}

# Bundestag income levels as published by the Bundestagsverwaltung. Key 0 = 1-1.000 EUR.
INCOME_LEVELS: dict[int, tuple[int, int | None]] = {
    0: (1, 1_000),
    1: (1_000, 3_500),
    2: (3_500, 7_000),
    3: (7_000, 15_000),
    4: (15_000, 30_000),
    5: (30_000, 50_000),
    6: (50_000, 75_000),
    7: (75_000, 100_000),
    8: (100_000, 150_000),
    9: (150_000, 250_000),
    10: (250_000, None),
}


def income_level_text(key: int | str | None) -> str | None:
    """Human-readable range for an income-level key ('3' -> '7.000-15.000 EUR')."""
    if key is None or key == "":
        return None
    try:
        lo, hi = INCOME_LEVELS[int(key)]
    except (KeyError, ValueError):
        return None

    def fmt(n: int) -> str:
        return f"{n:,}".replace(",", ".")

    return f"over {fmt(lo)} EUR" if hi is None else f"{fmt(lo)}-{fmt(hi)} EUR"


SIDEJOB_INTERVALS = {0: "one-off", 1: "monthly", 2: "yearly"}

# category id -> (English label, German original)
SIDEJOB_CATEGORIES: dict[int, tuple[str, str]] = {
    29231: ("Stakes in companies/partnerships", "Beteiligung an Kapital- oder Personengesellschaften"),
    29647: ("Paid activities besides the mandate", "Entgeltliche Tätigkeiten neben dem Mandat"),
    29229: ("Roles in public-law bodies", "Funktionen in Körperschaften und Anstalten des öffentlichen Rechts"),
    29228: ("Roles in companies", "Funktionen in Unternehmen"),
    29230: ("Roles in associations/foundations", "Funktionen in Vereinen, Verbänden und Stiftungen"),
    29232: ("Donations for political work", "Spenden/Zuwendungen für politische Tätigkeit"),
    29233: ("Agreements on future activities/benefits", "Vereinbarungen über künftige Tätigkeiten oder Vermögensvorteile"),
    29234: ("Occupation before joining the Bundestag", "Berufliche Tätigkeit vor der Mitgliedschaft im Bundestag"),
}

COMMITTEE_ROLES = {
    "chairperson": "Chair",
    "vice_chairperson": "Vice chair",
    "foreperson": "Foreperson",
    "spokesperson": "Spokesperson",
    "alternate_spokesperson": "Alternate spokesperson",
    "secretary": "Secretary",
    "alternate_secretary": "Alternate secretary",
    "advisory_member": "Advisory member",
    "eligible_member": "Eligible member",
    "member": "Member",
    "alternate_member": "Alternate member",
    "rapporteur": "Rapporteur",
}

MANDATE_WON = {
    "constituency": "Won the constituency (Direktmandat)",
    "list": "Entered via party list (Listenmandat)",
    "moved_up": "Moved up as successor (Nachrücker)",
}

ENTITY_TYPES = {
    "mandate": "A seat held during a legislature period.",
    "candidacy": "A candidacy in an election (carries the party they ran for).",
}

CURRENT_ON_NOTE = (
    "The API hides mandates that are no longer current unless current_on=all is sent; "
    "this server always sends current_on=all and reports is_current itself."
)
