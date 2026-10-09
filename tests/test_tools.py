"""Tool-level tests: every tool through the real MCP server wiring, backed by the fake API."""
from __future__ import annotations

import pytest
from builders import mandate, parliament, period, politician, poll, topic, vote

BT = (5, "Bundestag")
BT25 = (161, "Bundestag 2025 - 2029")
BT25_ELECTION = (160, "Bundestag Wahl 2025")
BT21 = (132, "Bundestag 2021 - 2025")
BT21_ELECTION = (128, "Bundestag Wahl 2021")
BY = (13, "Bayern")
BY23 = (149, "Bayern 2023 - 2028")
SCHOLZ = 66924


async def call(mcp_client, tool: str, **args):
    result = await mcp_client.call_tool(tool, args, raise_on_error=False)
    return result


def ok(result):
    assert not result.is_error, result.content[0].text if result.content else result
    return result.data


def error_text(result) -> str:
    assert result.is_error
    return result.content[0].text


@pytest.fixture
def reference(api):
    """Parliaments, periods and topics shared by most scenarios."""
    api.add("parliaments", [parliament(5, "Bundestag", BT25), parliament(13, "Bayern", BY23)])
    api.add("parliament-periods", [
        period(161, "Bundestag 2025 - 2029", BT, start="2025-03-25", end="2029-02-22", previous=BT25_ELECTION),
        period(160, "Bundestag Wahl 2025", BT, type_="election", start="2025-01-29", end="2025-02-23", previous=BT21, election_date="2025-02-23"),
        period(132, "Bundestag 2021 - 2025", BT, start="2021-10-26", end="2025-03-24", previous=BT21_ELECTION),
        period(128, "Bundestag Wahl 2021", BT, type_="election", start="2021-07-06", end="2021-09-26", election_date="2021-09-26"),
        period(149, "Bayern 2023 - 2028", BY, start="2023-10-30", end="2028-10-30"),
    ])
    api.add("topics", [topic(20, "Energie"), topic(22, "Öffentliche Finanzen, Steuern und Abgaben"), topic(9, "Umwelt"), topic(2900, "Klima", (9, "Umwelt"))])


@pytest.fixture
def scholz(api, reference):
    """Olaf Scholz: current Bundestag mandate, one earlier, two candidacies."""
    api.add("politicians/66924", politician(SCHOLZ, "Olaf", "Scholz"))
    api.add("candidacies-mandates", [
        mandate(68873, SCHOLZ, "Olaf Scholz", BT25, won="constituency"),
        mandate(53766, SCHOLZ, "Olaf Scholz", BT21),
        mandate(64889, SCHOLZ, "Olaf Scholz", BT25_ELECTION, type_="candidacy", party=(1, "SPD")),
        mandate(48686, SCHOLZ, "Olaf Scholz", BT21_ELECTION, type_="candidacy", party=(1, "SPD")),
    ], match={"politician": SCHOLZ, "current_on": "all"})


def polls_bt25():
    return [
        poll(4, "Gebäudeenergie", "2026-09-30", BT25, topics=[(20, "Energie")]),
        poll(3, "Tankrabatt", "2026-09-25", BT25, topics=[(20, "Energie"), (22, "Steuern")]),
        poll(2, "Bürgergeld", "2026-03-10", BT25, topics=[(22, "Steuern")], accepted=False),
        poll(1, "Rente", "2025-11-01", BT25, topics=[]),
    ]


def scholz_votes():
    label = "Olaf Scholz (Bundestag 2025 - 2029)"
    return [
        vote(104, 68873, label, 4, "yes", "SPD (Bundestag 2025 - 2029)"),
        vote(103, 68873, label, 3, "no_show", "SPD (Bundestag 2025 - 2029)"),
        vote(102, 68873, label, 2, "no", "SPD (Bundestag 2025 - 2029)"),
        vote(101, 68873, label, 1, "yes", "SPD (Bundestag 2025 - 2029)"),
    ]


# =============================================================================================
# aw_find_politician
# =============================================================================================
class TestFindPolitician:
    async def test_unique_exact_match_includes_mandates(self, api, mcp_client, scholz):
        api.add("politicians", [politician(SCHOLZ, "Olaf", "Scholz")], match={"last_name[cn]": "Scholz", "first_name[cn]": "Olaf"})
        d = ok(await call(mcp_client, "aw_find_politician", name="Olaf Scholz"))
        assert d["data"]["unique_exact_match"] == SCHOLZ and not d["data"]["needs_user_choice"]
        match = d["data"]["matches"][0]
        assert match["is_current_mp"] is True
        assert [m["period"] for m in match["mandates"]] == ["Bundestag 2025 - 2029", "Bundestag 2021 - 2025"]
        assert "https://www.abgeordnetenwatch.de/profile/olaf-scholz" in d["sources"]

    async def test_mandates_are_always_requested_with_current_on_all(self, api, mcp_client, scholz):
        api.add("politicians", [politician(SCHOLZ, "Olaf", "Scholz")], match={"last_name[cn]": "Scholz"})
        ok(await call(mcp_client, "aw_find_politician", name="Scholz"))
        assert all(c.get("current_on") == "all" for c in api.calls("candidacies-mandates"))

    async def test_two_people_with_the_same_name_need_a_user_choice(self, api, mcp_client, reference):
        api.add("politicians", [politician(2, "Thomas", "Müller", (1, "SPD")), politician(1, "Thomas", "Müller", (2, "CDU"))], match={"last_name[cn]": "Müller", "first_name[cn]": "Thomas"})
        api.add("candidacies-mandates", [
            mandate(10, 2, "Thomas Müller", BT25), mandate(11, 1, "Thomas Müller", BY23, faction="CSU"),
        ])
        d = ok(await call(mcp_client, "aw_find_politician", name="Thomas Müller"))
        assert d["data"]["unique_exact_match"] is None and d["data"]["needs_user_choice"] is True
        assert {m["party"] for m in d["data"]["matches"]} == {"SPD", "CDU"}
        assert any("ask the user" in n for n in d["notes"])

    async def test_ascii_spelling_falls_back_to_umlaut_variant(self, api, mcp_client, reference):
        api.add("politicians", [], total=0, match={"last_name[cn]": "Mueller"})
        api.add("politicians", [politician(7, "Anna", "Müller")], match={"last_name[cn]": "Müller", "first_name[cn]": "Anna"})
        api.add("candidacies-mandates", [mandate(70, 7, "Anna Müller", BY23)])
        d = ok(await call(mcp_client, "aw_find_politician", name="Anna Mueller"))
        assert d["data"]["unique_exact_match"] == 7  # ranked against the spelling that matched
        assert any("Müller" in n for n in d["notes"])

    async def test_nothing_found(self, api, mcp_client, reference):
        api.add("politicians", [], total=0)
        d = ok(await call(mcp_client, "aw_find_politician", name="Zzzzzz"))
        assert d["data"]["matches"] == [] and d["data"]["unique_exact_match"] is None

    async def test_parliament_filter_uses_mandate_chain_filter(self, api, mcp_client, reference):
        api.add("candidacies-mandates", [mandate(70, 7, "Anna Müller", BY23)], match={"politician[entity.last_name][cn]": "Müller", "parliament_period[entity.parliament.entity.id]": 13})
        api.add("politicians", [politician(7, "Anna", "Müller")], match={"id[in]": "[7]"})
        api.add("candidacies-mandates", [mandate(70, 7, "Anna Müller", BY23)], match={"politician": 7})
        d = ok(await call(mcp_client, "aw_find_politician", name="Müller", parliament="Bayern"))
        assert d["data"]["matches"][0]["politician_id"] == 7

    async def test_unknown_parliament_is_a_clear_error(self, mcp_client, reference):
        assert "Unknown parliament" in error_text(await call(mcp_client, "aw_find_politician", name="Müller", parliament="Atlantis"))


# =============================================================================================
# aw_get_politician
# =============================================================================================
class TestGetPolitician:
    async def test_profile_and_timeline(self, mcp_client, scholz):
        d = ok(await call(mcp_client, "aw_get_politician", politician_id=SCHOLZ))
        data = d["data"]
        assert data["is_current_mp"] and data["profile"]["name"] == "Olaf Scholz" and data["profile"]["questions_asked"] == 3
        assert [m["period_id"] for m in data["mandates"]] == [161, 132]
        # every mandate names its ELECTION period - the key to programme lookup
        assert data["mandates"][0]["election_period"] == {"id": 160, "label": "Bundestag Wahl 2025"}
        assert data["candidacies"][0]["party_ran_for"] == "SPD"
        assert d["summary"] == "Olaf Scholz: Bundestag (SPD)"

    async def test_unknown_id(self, api, mcp_client, reference):
        api.add_error("politicians/5", "There is no politician entity with id 5")
        assert "Not found" in error_text(await call(mcp_client, "aw_get_politician", politician_id=5))

    async def test_former_mp_gets_a_note(self, api, mcp_client, reference):
        api.add("politicians/8", politician(8, "Old", "Timer"))
        api.add("candidacies-mandates", [mandate(80, 8, "Old Timer", BT21)])
        d = ok(await call(mcp_client, "aw_get_politician", politician_id=8))
        assert d["data"]["is_current_mp"] is False and any("No current mandate" in n for n in d["notes"])


# =============================================================================================
# aw_politician_votes
# =============================================================================================
class TestPoliticianVotes:
    @pytest.fixture(autouse=True)
    def _routes(self, api, scholz):
        api.add("votes", scholz_votes(), match={"mandate": 68873})
        api.add("polls", polls_bt25(), match={"field_legislature": 161})

    async def test_join_sorts_newest_first_and_counts_everything(self, mcp_client):
        d = ok(await call(mcp_client, "aw_politician_votes", politician_id=SCHOLZ, limit=2))
        rows = d["data"]["votes"]
        assert [r["poll_id"] for r in rows] == [4, 3]  # by poll date, not vote id
        assert rows[0]["date"] == "2026-09-30" and rows[0]["topics"] == [{"id": 20, "label": "Energie"}] and rows[0]["fraction"] == "SPD"
        assert d["data"]["summary"] == {"yes": 2, "no": 1, "abstain": 0, "no_show": 1, "total": 4, "date_range": ["2025-11-01", "2026-09-30"]}
        assert d["page"] == {"total": 4, "returned": 2, "offset": 0, "has_more": True}

    async def test_default_scope_is_most_recent_mandate_and_says_so(self, mcp_client):
        d = ok(await call(mcp_client, "aw_politician_votes", politician_id=SCHOLZ))
        assert [p["period_id"] for p in d["data"]["periods_covered"]] == [161]
        assert any("most recent mandate" in n for n in d["notes"])
        assert any("roll-call" in n for n in d["notes"])

    async def test_topic_filter_is_pushed_to_the_polls_request(self, api, mcp_client):
        api.add("polls", [p for p in polls_bt25() if p["id"] in (3, 4)], match={"field_legislature": 161, "field_topics": 20})
        d = ok(await call(mcp_client, "aw_politician_votes", politician_id=SCHOLZ, topic="Energie"))
        assert {r["poll_id"] for r in d["data"]["votes"]} == {3, 4}
        assert d["data"]["summary"]["total"] == 2
        assert d["data"]["filters"]["topic"] == "Energie"
        assert all("poll" not in c for c in api.calls("votes"))  # votes cannot be filtered by poll fields

    async def test_date_and_keyword_filters(self, api, mcp_client):
        api.add("polls", [p for p in polls_bt25() if p["id"] == 2], match={"field_legislature": 161, "field_poll_date[gte]": "2026-01-01", "label[cn]": "Bürger"})
        d = ok(await call(mcp_client, "aw_politician_votes", politician_id=SCHOLZ, since="2026-01-01", keyword="Bürger"))
        assert [r["poll_id"] for r in d["data"]["votes"]] == [2] and d["data"]["votes"][0]["vote"] == "no"

    async def test_vote_value_and_no_show_filters(self, mcp_client):
        only_no = ok(await call(mcp_client, "aw_politician_votes", politician_id=SCHOLZ, vote="no"))
        assert [r["poll_id"] for r in only_no["data"]["votes"]] == [2]
        present = ok(await call(mcp_client, "aw_politician_votes", politician_id=SCHOLZ, include_no_show=False))
        assert present["data"]["summary"]["total"] == 3 and present["data"]["summary"]["no_show"] == 0

    async def test_all_periods_uses_one_politician_chain_request(self, api, mcp_client):
        label21 = "Olaf Scholz (Bundestag 2021 - 2025)"
        api.add("votes", scholz_votes() + [vote(50, 53766, label21, 90, "yes", "SPD (Bundestag 2021 - 2025)")], match={"mandate[entity.politician.entity.id]": SCHOLZ})
        api.add("polls", [poll(90, "Altes Gesetz", "2023-05-05", BT21)], match={"field_legislature": 132})
        d = ok(await call(mcp_client, "aw_politician_votes", politician_id=SCHOLZ, all_periods=True))
        assert d["data"]["summary"]["total"] == 5
        assert {p["period_id"] for p in d["data"]["periods_covered"]} == {161, 132}
        assert len(api.calls("votes")) == 1

    async def test_explicit_period(self, api, mcp_client):
        api.add("votes", [vote(50, 53766, "Olaf Scholz (Bundestag 2021 - 2025)", 90, "yes", "SPD (Bundestag 2021 - 2025)")], match={"mandate": 53766})
        api.add("polls", [poll(90, "Altes Gesetz", "2023-05-05", BT21)], match={"field_legislature": 132})
        d = ok(await call(mcp_client, "aw_politician_votes", politician_id=SCHOLZ, period_id=132))
        assert [r["poll_id"] for r in d["data"]["votes"]] == [90]

    async def test_unknown_period_lists_available_ones(self, mcp_client):
        text = error_text(await call(mcp_client, "aw_politician_votes", politician_id=SCHOLZ, period_id=999))
        assert "period_id 161" in text and "period_id 132" in text

    async def test_ambiguous_topic_is_rejected_with_candidates(self, mcp_client):
        text = error_text(await call(mcp_client, "aw_politician_votes", politician_id=SCHOLZ, topic="en"))
        assert "ambiguous" in text and "Energie" in text and "Pass one topic id" in text

    async def test_unknown_politician(self, api, mcp_client, reference):
        api.add("candidacies-mandates", [], total=0, match={"politician": 1})
        assert "no mandates on record" in error_text(await call(mcp_client, "aw_politician_votes", politician_id=1))

    async def test_mandate_without_roll_calls(self, api, mcp_client):
        api.add("votes", [], total=0, match={"mandate": 68873})
        d = ok(await call(mcp_client, "aw_politician_votes", politician_id=SCHOLZ))
        assert d["data"]["votes"] == [] and any("No roll-call votes recorded" in n for n in d["notes"])


# =============================================================================================
# aw_poll_result
# =============================================================================================
class TestPollResult:
    @pytest.fixture
    def poll_with_votes(self, api, reference):
        p = poll(6683, "Übergewinnsteuer", "2026-09-25", BT25, topics=[(20, "Energie")], accepted=False, intro="<p>Antrag der Grünen</p>")
        spd = "SPD (Bundestag 2025 - 2029)"
        gr = "BÜNDNIS 90/\xadDIE GRÜNEN (Bundestag 2025 - 2029)"
        votes = [vote(i, 68873 if i == 1 else 1000 + i, f"MP{i} (Bundestag 2025 - 2029)", 6683, v, spd) for i, v in enumerate(["no", "no", "no", "yes"], start=1)]
        votes += [vote(10 + i, 2000 + i, f"Green{i} (Bundestag 2025 - 2029)", 6683, "yes", gr) for i in range(3)]
        p["related_data"] = {"votes": votes}
        api.add("polls/6683", p, match={"related_data": "votes"})

    async def test_tally_per_faction(self, mcp_client, poll_with_votes):
        d = ok(await call(mcp_client, "aw_poll_result", poll_id=6683))
        assert d["data"]["overall"] == {"yes": 4, "no": 3, "abstain": 0, "no_show": 0, "members": 7}
        by = {f["fraction"]: f for f in d["data"]["by_fraction"]}
        assert by["SPD"]["majority_position"] == "no" and by["BÜNDNIS 90/DIE GRÜNEN"]["unanimous"] is True
        assert "rejected" in d["summary"] and d["data"]["poll"]["intro"] == "Antrag der Grünen"
        assert any("computed from the individual vote records" in n for n in d["notes"])

    async def test_politician_followed_faction(self, api, mcp_client, poll_with_votes, scholz):
        d = ok(await call(mcp_client, "aw_poll_result", poll_id=6683, politician_id=SCHOLZ))
        assert d["data"]["politician"] == {
            "politician_id": SCHOLZ, "voted": True, "vote": "no", "reason_no_show": None, "reason_no_show_other": None,
            "fraction": "SPD", "fraction_majority": "no", "followed_fraction_majority": True,
        }

    async def test_dissenters_are_listed(self, mcp_client, poll_with_votes):
        d = ok(await call(mcp_client, "aw_poll_result", poll_id=6683, include_votes="dissenters"))
        assert [(v["name"], v["vote"], v["fraction_majority"]) for v in d["data"]["votes"]] == [("MP4", "yes", "no")]

    async def test_politician_without_vote_record(self, api, mcp_client, poll_with_votes, reference):
        api.add("candidacies-mandates", [mandate(99, 5, "Other Person", BT25)], match={"politician": 5})
        d = ok(await call(mcp_client, "aw_poll_result", poll_id=6683, politician_id=5))
        assert d["data"]["politician"] == {"politician_id": 5, "voted": False}

    async def test_unknown_poll(self, api, mcp_client, reference):
        api.add_error("polls/1", "There is no poll entity with id 1")
        assert "Not found" in error_text(await call(mcp_client, "aw_poll_result", poll_id=1))


# =============================================================================================
# aw_search_polls
# =============================================================================================
class TestSearchPolls:
    async def test_filters_are_sent_to_the_api(self, api, mcp_client, reference):
        api.add("polls", polls_bt25()[:2])
        d = ok(await call(mcp_client, "aw_search_polls", keyword="Steuer", topic="Energie", parliament="NRW" if False else "Bundestag", since="2026-01-01", accepted=True, limit=2))
        c = api.calls("polls")[0]
        assert c["label[cn]"] == "Steuer" and c["field_topics"] == "20" and c["field_legislature[entity.parliament.entity.id]"] == "5"
        assert c["field_poll_date[gte]"] == "2026-01-01" and c["field_accepted"] == "1" and c["sort_by"] == "field_poll_date" and c["sort_direction"] == "desc"
        assert [r["poll_id"] for r in d["data"]] == [4, 3] and d["data"][0]["parliament"] == "Bundestag"

    async def test_oldest_first(self, api, mcp_client, reference):
        api.add("polls", [])
        ok(await call(mcp_client, "aw_search_polls", sort="oldest"))
        assert api.calls("polls")[0]["sort_direction"] == "asc"

    async def test_paging_note(self, api, mcp_client, reference):
        api.add("polls", polls_bt25()[:2], total=10)
        d = ok(await call(mcp_client, "aw_search_polls", limit=2))
        assert d["page"]["has_more"] and any("raise offset" in n for n in d["notes"])

    async def test_keyword_without_hits_gives_advice(self, api, mcp_client, reference):
        api.add("polls", [], total=0)
        d = ok(await call(mcp_client, "aw_search_polls", keyword="xyz"))
        assert any("shorter or different German word" in n for n in d["notes"])


# =============================================================================================
# aw_compare_votes
# =============================================================================================
class TestCompareVotes:
    async def test_agreement_and_disagreements(self, api, mcp_client, scholz, reference):
        api.add("politicians/2", politician(2, "Other", "Person"))
        api.add("candidacies-mandates", [mandate(200, 2, "Other Person", BT25, faction="CDU")], match={"politician": 2, "current_on": "all"})
        # a combined request for both people (politician[in]) returns both mandate lists
        api.add("candidacies-mandates", [mandate(68873, SCHOLZ, "Olaf Scholz", BT25), mandate(200, 2, "Other Person", BT25, faction="CDU")], match={"politician[in]": f"[{SCHOLZ},2]"})
        api.add("votes", scholz_votes(), match={"mandate": 68873})
        other_label = "Other Person (Bundestag 2025 - 2029)"
        api.add("votes", [
            vote(204, 200, other_label, 4, "yes", "CDU (Bundestag 2025 - 2029)"),
            vote(203, 200, other_label, 3, "yes", "CDU (Bundestag 2025 - 2029)"),
            vote(202, 200, other_label, 2, "yes", "CDU (Bundestag 2025 - 2029)"),
            vote(201, 200, other_label, 1, "yes", "CDU (Bundestag 2025 - 2029)"),
        ], match={"mandate": 200})
        api.add("polls", polls_bt25(), match={"field_legislature": 161})
        d = ok(await call(mcp_client, "aw_compare_votes", politician_ids=[SCHOLZ, 2]))
        data = d["data"]
        # poll 3 (Scholz no_show) is excluded; poll 2 differs (no vs yes); polls 4 and 1 agree
        assert (data["polls_compared"], data["polls_agreeing"], data["agreement_rate"]) == (3, 2, round(2 / 3, 3))
        assert [x["poll_id"] for x in data["disagreements"]] == [2]
        assert data["disagreements"][0]["votes"] == {str(SCHOLZ): "no", "2": "yes"}

    async def test_no_shared_period(self, api, mcp_client, reference):
        api.add("candidacies-mandates", [mandate(1, 1, "A B", BT25), mandate(2, 2, "C D", BY23)], match={"politician[in]": "[1,2]"})
        assert "never sat in the same legislature period" in error_text(await call(mcp_client, "aw_compare_votes", politician_ids=[1, 2]))


# =============================================================================================
# aw_get_sidejobs
# =============================================================================================
def sidejob(id_, label, *, category="29647", level=None, income=None, interval=None, extra=None, info=None, mandate_id=68873):
    return {
        "id": id_, "entity_type": "sidejob", "label": label, "job_title_extra": extra, "category": category, "income_level": level, "income": income,
        "interval": interval, "income_total": None, "data_change_date": "2026-09-16", "additional_information": info,
        "sidejob_organization": {"id": 1, "label": "Org e.V."}, "field_city": {"id": 1, "label": "Berlin"}, "field_country": {"id": 61, "label": "Deutschland"},
        "field_topics": [{"id": 28, "label": "Gesundheit"}], "mandates": [{"id": mandate_id, "label": "x"}],
    }


class TestSidejobs:
    async def test_decodes_levels_categories_and_missing_income(self, api, mcp_client, scholz):
        api.add("sidejobs", [
            sidejob(1, "Facharzt", level="6", income=60636.77, interval="2", extra="Einkommen im Jahr 2025", info="<p>Gewinn vor Steuern</p>"),
            sidejob(2, "Beirat (bis 03.09.2025)", category="29230"),
            sidejob(3, "Berater", level="3"),
        ], match={"mandates": 68873})
        d = ok(await call(mcp_client, "aw_get_sidejobs", politician_id=SCHOLZ))
        jobs = d["data"]["sidejobs"]
        assert [j["sidejob_id"] for j in jobs] == [1, 3, 2]  # highest level first, unreported last
        assert jobs[0]["income_level_range"] == "50.000-75.000 EUR" and jobs[0]["income_exact"] == 60636.77 and jobs[0]["interval"] == "yearly"
        assert jobs[0]["category"]["label"] == "Paid activities besides the mandate" and jobs[0]["details"] == "Gewinn vor Steuern"
        assert jobs[2]["date_hint"] == "bis 03.09.2025" and "income_level" not in jobs[2]
        summary = d["data"]["summary"]
        assert summary["income_reported"] == 2 and summary["income_not_reported"] == 1 and summary["highest_income_level"] == 6
        assert any("not that it was unpaid" in n for n in d["notes"])

    async def test_filters(self, api, mcp_client, scholz):
        api.add("sidejobs", [sidejob(1, "A", level="6"), sidejob(2, "B", level="2", category="29230")], match={"mandates": 68873})
        by_level = ok(await call(mcp_client, "aw_get_sidejobs", politician_id=SCHOLZ, min_income_level=5))
        assert [j["sidejob_id"] for j in by_level["data"]["sidejobs"]] == [1]  # compared as numbers, not strings
        by_cat = ok(await call(mcp_client, "aw_get_sidejobs", politician_id=SCHOLZ, category=29230))
        assert [j["sidejob_id"] for j in by_cat["data"]["sidejobs"]] == [2]

    async def test_non_bundestag_member_is_not_available_rather_than_empty(self, api, mcp_client, reference):
        api.add("candidacies-mandates", [mandate(60402, 133707, "Ruth Müller", BY23)], match={"politician": 133707})
        d = ok(await call(mcp_client, "aw_get_sidejobs", politician_id=133707))
        assert d["data"]["coverage"] == "not_available" and "Bayern" in d["data"]["mandates_in"]
        assert any("does NOT mean there are none" in n for n in d["notes"])
        assert api.calls("sidejobs") == []


# =============================================================================================
# aw_committees
# =============================================================================================
class TestCommittees:
    async def test_exactly_one_mode(self, mcp_client, reference):
        assert "exactly one mode" in error_text(await call(mcp_client, "aw_committees"))
        assert "exactly one mode" in error_text(await call(mcp_client, "aw_committees", politician_id=1, committee_id=2))

    async def test_memberships_of_a_politician(self, api, mcp_client, scholz):
        api.add("committee-memberships", [{
            "id": 1, "committee": {"id": 6089, "label": "Haushaltsausschuss", "abgeordnetenwatch_url": "https://x/h"},
            "candidacy_mandate": {"id": 68873, "label": "Olaf Scholz"}, "committee_role": "chairperson", "committee_roles_additional": ["spokesperson"],
        }], match={"candidacy_mandate": 68873})
        d = ok(await call(mcp_client, "aw_committees", politician_id=SCHOLZ))
        assert d["data"][0]["role"] == "Chair" and d["data"][0]["additional_roles"] == ["Spokesperson"] and d["data"][0]["parliament"] == "Bundestag"

    async def test_members_of_a_committee_with_politician_ids(self, api, mcp_client, reference):
        api.add("committees/6089", {"id": 6089, "label": "Haushaltsausschuss", "field_legislature": {"id": 161, "label": "Bundestag 2025 - 2029"}, "field_topics": [{"id": 22, "label": "Haushalt"}]})
        api.add("committee-memberships", [
            {"id": 1, "committee": {"id": 6089}, "candidacy_mandate": {"id": 300, "label": "Anna Beispiel (Bundestag 2025 - 2029)"}, "committee_role": "member", "committee_roles_additional": None},
            {"id": 2, "committee": {"id": 6089}, "candidacy_mandate": {"id": 301, "label": "Bernd Muster (Bundestag 2025 - 2029)"}, "committee_role": "chairperson", "committee_roles_additional": None},
        ], match={"committee": 6089})
        api.add("candidacies-mandates", [mandate(300, 31, "Anna Beispiel", BT25, faction="CDU/CSU"), mandate(301, 32, "Bernd Muster", BT25, faction="SPD")], match={"id[in]": "[300,301]"})
        d = ok(await call(mcp_client, "aw_committees", committee_id=6089))
        members = d["data"]["members"]
        assert [(m["name"], m["politician_id"], m["faction"], m["role"]) for m in members] == [("Bernd Muster", 32, "SPD", "Chair"), ("Anna Beispiel", 31, "CDU/CSU", "Member")]

    async def test_browse_by_parliament_and_keyword(self, api, mcp_client, reference):
        api.add("committees", [{"id": 6089, "label": "Haushaltsausschuss", "field_legislature": {"id": 161, "label": "Bundestag 2025 - 2029"}, "field_topics": [], "abgeordnetenwatch_url": "https://x/h"}])
        d = ok(await call(mcp_client, "aw_committees", parliament="Bundestag", keyword="Haushalt"))
        c = api.calls("committees")[0]
        assert c["label[cn]"] == "Haushalt" and c["field_legislature[entity.parliament.entity.id]"] == "5"
        assert d["data"][0]["committee_id"] == 6089


# =============================================================================================
# programmes
# =============================================================================================
def program(id_, title, party, election, file_):
    return {"id": id_, "label": title, "party": {"id": party[0], "label": party[1]}, "parliament_period": {"id": election[0], "label": election[1]}, "file": file_, "link": [{"uri": None, "title": None, "options": None}]}


class TestElectionPrograms:
    async def test_for_politician_uses_candidacy_party_and_election(self, api, mcp_client, scholz):
        api.add("election-program", [program(598, "Bundestagswahlprogramm der SPD", (1, "SPD"), BT25_ELECTION, "https://www.abgeordnetenwatch.de/sites/default/files/x.pdf")], match={"party": 1, "parliament_period": 160})
        d = ok(await call(mcp_client, "aw_find_election_programs", politician_id=SCHOLZ))
        p = d["data"][0]
        assert (p["program_id"], p["party"], p["election"]["label"], p["election"]["date"]) == (598, "SPD", "Bundestag Wahl 2025", "2025-02-23")
        assert p["pdf_url"].endswith("x.pdf") and p["links"] == []
        assert any("PDFs only" in n for n in d["notes"])

    async def test_legislature_id_is_mapped_to_the_election_before_it(self, api, mcp_client, reference):
        api.add("election-program", [], total=0, match={"parliament_period": 160})
        d = ok(await call(mcp_client, "aw_find_election_programs", period_id=161))
        assert any("legislature" in n for n in d["notes"])
        assert api.calls("election-program")[0]["parliament_period"] == "160"

    async def test_latest_only_keeps_newest_per_party(self, api, mcp_client, reference):
        api.add("parties", [{"id": 1, "label": "SPD"}], match={"label[cn]": "SPD"})
        api.add("election-program", [
            program(367, "SPD 2021", (1, "SPD"), BT21_ELECTION, "https://www.abgeordnetenwatch.de/a.pdf"),
            program(598, "SPD 2025", (1, "SPD"), BT25_ELECTION, "https://www.abgeordnetenwatch.de/b.pdf"),
        ], match={"party": 1})
        newest = ok(await call(mcp_client, "aw_find_election_programs", party="SPD"))
        assert [p["program_id"] for p in newest["data"]] == [598]
        every = ok(await call(mcp_client, "aw_find_election_programs", party="SPD", latest_only=False))
        assert [p["program_id"] for p in every["data"]] == [598, 367]

    async def test_endpoint_is_singular(self, api, mcp_client, reference):
        api.add("election-program", [])
        ok(await call(mcp_client, "aw_find_election_programs"))
        assert len(api.calls("election-program")) == 1 and api.calls("election-programs") == []


class TestReadElectionProgram:
    PAGES = [
        "Titelseite Wahlprogramm",
        "Wir erhöhen den Mindestlohn auf 15 Euro. Die Tarifbindung wird gestärkt. Gute Löhne sind die Grundlage für Wohlstand im ganzen Land.",
        "Steuern: Wir entlasten kleine und mittlere Einkommen. Sehr hohe Vermögen sollen stärker zur Finanzierung beitragen. Das ist gerecht.",
    ]

    @pytest.fixture(autouse=True)
    def _program(self, api, reference, monkeypatch):
        api.add("election-program/598", program(598, "Bundestagswahlprogramm der SPD", (1, "SPD"), BT25_ELECTION, "https://www.abgeordnetenwatch.de/f.pdf"))

        async def fake_pages(url, program_id):
            return self.PAGES

        monkeypatch.setattr("tools.programs.load_pdf_pages", fake_pages)

    async def test_query_returns_ranked_passages_with_pages(self, mcp_client):
        d = ok(await call(mcp_client, "aw_read_election_program", program_id=598, query="Mindestlohn"))
        assert d["data"]["untrusted_content"] is True and d["data"]["total_pages"] == 3
        assert d["data"]["passages"][0]["page"] == 2 and "15 Euro" in d["data"]["passages"][0]["text"]
        assert any("untrusted" in n for n in d["notes"])

    async def test_stemming_finds_inflected_words(self, mcp_client):
        d = ok(await call(mcp_client, "aw_read_election_program", program_id=598, query="Steuer"))
        assert d["data"]["passages"][0]["page"] == 3

    async def test_no_match_has_advice(self, mcp_client):
        d = ok(await call(mcp_client, "aw_read_election_program", program_id=598, query="Raumfahrt"))
        assert d["data"]["passages"] == [] and any("No page contains" in n for n in d["notes"])

    async def test_page_range(self, mcp_client):
        d = ok(await call(mcp_client, "aw_read_election_program", program_id=598, pages="2-3"))
        assert d["data"]["pages"]["from"] == 2 and d["data"]["pages"]["to"] == 3 and "[page 3]" in d["data"]["pages"]["text"]

    async def test_default_reads_the_first_pages(self, mcp_client):
        d = ok(await call(mcp_client, "aw_read_election_program", program_id=598))
        assert d["data"]["pages"]["from"] == 1

    async def test_bad_page_spec(self, mcp_client):
        assert "Cannot read pages" in error_text(await call(mcp_client, "aw_read_election_program", program_id=598, pages="abc"))
        assert "does not exist" in error_text(await call(mcp_client, "aw_read_election_program", program_id=598, pages="99"))

    async def test_programme_without_file(self, api, mcp_client):
        api.add("election-program/7", program(7, "Ohne PDF", (1, "SPD"), BT25_ELECTION, None))
        assert "no PDF" in error_text(await call(mcp_client, "aw_read_election_program", program_id=7))


# =============================================================================================
# aw_lookup
# =============================================================================================
class TestLookup:
    async def test_parliaments_and_periods(self, mcp_client, reference):
        parliaments = ok(await call(mcp_client, "aw_lookup", kind="parliament", query="bay"))
        assert [p["name"] for p in parliaments["data"]] == ["Bayern"]
        periods = ok(await call(mcp_client, "aw_lookup", kind="period", parliament="Bundestag"))
        assert [p["id"] for p in periods["data"]] == [161, 160, 132, 128]
        assert next(p for p in periods["data"] if p["id"] == 161)["previous_period_id"] == 160

    async def test_topics_with_parent(self, mcp_client, reference):
        d = ok(await call(mcp_client, "aw_lookup", kind="topic", query="klima"))
        assert d["data"] == [{"id": 2900, "label": "Klima", "parent_id": 9, "parent_label": "Umwelt", "url": "https://www.abgeordnetenwatch.de/themen-dip21/2900"}]

    async def test_party_and_committee_queries_hit_the_api(self, api, mcp_client, reference):
        api.add("parties", [{"id": 1, "label": "SPD", "full_name": None}])
        api.add("committees", [])
        ok(await call(mcp_client, "aw_lookup", kind="party", query="SPD"))
        ok(await call(mcp_client, "aw_lookup", kind="committee", query="Haushalt", parliament="Bundestag"))
        assert api.calls("parties")[0]["label[cn]"] == "SPD"
        assert api.calls("committees")[0]["field_legislature[entity.parliament.entity.id]"] == "5"

    async def test_constituency_needs_a_query(self, mcp_client, reference):
        assert "needs a query" in error_text(await call(mcp_client, "aw_lookup", kind="constituency"))
