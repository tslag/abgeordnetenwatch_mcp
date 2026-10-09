"""Unit tests for the pure helper modules: text, codebooks, caches, tallies."""
from __future__ import annotations

import pytest
from builders import poll, vote

from aw.cache import MISS, DiskCache, LayeredCache, TTLCache
from aw.endpoints import ENDPOINTS, INCOME_LEVELS, SIDEJOB_CATEGORIES, income_level_text
from aw.polls import clean_fraction_name, faction_position, slim_poll, tally_votes
from aw.text import name_variants, parse_name, strip_html


# --- text ------------------------------------------------------------------------------------------
class TestStripHtml:
    def test_removes_tags_and_decodes_entities(self):
        assert strip_html("<p>Der <a href='x'>Bundestag</a> &amp; mehr</p><p>Zweiter&nbsp;Absatz</p>") == "Der Bundestag & mehr\nZweiter Absatz"

    def test_truncates_at_word_boundary(self):
        out = strip_html("<p>" + "wort " * 100 + "</p>", max_chars=24)
        assert out.endswith("…") and len(out) <= 25 and " wort…" not in out[:-1]

    def test_empty_and_none(self):
        assert strip_html(None) is None
        assert strip_html("<p> </p>") is None

    def test_ignores_script_content(self):
        assert strip_html("<p>a</p><script>evil()</script>") == "a"


class TestParseName:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("Olaf Scholz", ("Olaf", "Scholz")),
            ("Scholz", (None, "Scholz")),
            ("Scholz, Olaf", ("Olaf", "Scholz")),
            ("Dr. Angela Merkel", ("Angela", "Merkel")),
            ("Ursula von der Leyen", ("Ursula", "von der Leyen")),
            ("Karl-Theodor zu Guttenberg", ("Karl-Theodor", "zu Guttenberg")),
            ("  Hans   Peter  Müller ", ("Hans Peter", "Müller")),
        ],
    )
    def test_splits(self, query, expected):
        assert parse_name(query) == expected


class TestNameVariants:
    def test_ascii_to_umlaut_and_back(self):
        assert name_variants("Mueller") == ["Mueller", "Müller"]
        assert name_variants("Müller") == ["Müller", "Mueller"]
        assert name_variants("Strauß") == ["Strauß", "Strauss"]

    def test_plain_name_has_single_variant(self):
        assert name_variants("Scholz") == ["Scholz"]


# --- codebooks ---------------------------------------------------------------------------------------
class TestCodebook:
    def test_irregular_endpoint_names(self):
        assert ENDPOINTS["election_program"] == "election-program"  # singular!
        assert ENDPOINTS["candidacy_mandate"] == "candidacies-mandates"
        assert ENDPOINTS["committee_membership"] == "committee-memberships"

    def test_income_levels_are_0_to_10_and_contiguous(self):
        assert sorted(INCOME_LEVELS) == list(range(11))
        bounds = [INCOME_LEVELS[k] for k in range(11)]
        for (_, hi), (lo, _) in zip(bounds, bounds[1:], strict=False):
            assert hi == lo
        assert INCOME_LEVELS[10][1] is None

    @pytest.mark.parametrize(
        ("key", "text"),
        [(0, "1-1.000 EUR"), ("1", "1.000-3.500 EUR"), (3, "7.000-15.000 EUR"), (10, "over 250.000 EUR"), (None, None), ("", None), ("x", None), (11, None)],
    )
    def test_income_level_text(self, key, text):
        assert income_level_text(key) == text

    def test_sidejob_categories_complete(self):
        assert set(SIDEJOB_CATEGORIES) == {29228, 29229, 29230, 29231, 29232, 29233, 29234, 29647}


# --- caches ------------------------------------------------------------------------------------------
class TestTTLCache:
    def test_expiry(self):
        now = [0.0]
        cache = TTLCache(clock=lambda: now[0])
        cache.set("a", 1, ttl=10)
        assert cache.get("a") == 1
        now[0] = 10.0
        assert cache.get("a", "gone") == "gone"

    def test_lru_eviction(self):
        cache = TTLCache(max_items=2)
        cache.set("a", 1, 100)
        cache.set("b", 2, 100)
        cache.get("a")  # touch a: b is now least recently used
        cache.set("c", 3, 100)
        assert cache.get("b") is None and cache.get("a") == 1 and cache.get("c") == 3


class TestDiskCache:
    def test_roundtrip_and_expiry(self, tmp_path):
        now = [1000.0]
        disk = DiskCache(tmp_path / "c.sqlite", clock=lambda: now[0])
        disk.set("k", {"x": [1, 2, "ä"]}, ttl=60)
        assert disk.get("k") == {"x": [1, 2, "ä"]}
        now[0] = 1061.0
        assert disk.get("k", "expired") == "expired"
        disk.close()

    def test_unusable_path_degrades_to_noop(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        disk = DiskCache(blocker / "sub" / "c.sqlite")  # parent is a file -> cannot be created
        disk.set("k", 1, 10)
        assert disk.get("k", "none") == "none"

    def test_layered_promotes_disk_hits(self, tmp_path):
        disk = DiskCache(tmp_path / "c.sqlite")
        disk.set("k", [1, 2], 600)
        layered = LayeredCache(TTLCache(), disk)
        assert layered.get("k") == [1, 2]
        assert layered.memory.get("k") == [1, 2]
        assert layered.get("missing") is MISS


# --- polls: fractions and tallies ----------------------------------------------------------------------
class TestFractionNames:
    def test_strips_period_suffix_and_soft_hyphen(self):
        assert clean_fraction_name("SPD (Bundestag 2025 - 2029)") == "SPD"
        assert clean_fraction_name("BÜNDNIS 90/\xadDIE GRÜNEN (Bundestag 2025 - 2029)") == "BÜNDNIS 90/DIE GRÜNEN"
        assert clean_fraction_name("fraktionslos (Bundestag 2017 - 2021)") == "fraktionslos"

    def test_keeps_names_without_period(self):
        assert clean_fraction_name("Die Linke") == "Die Linke"
        assert clean_fraction_name(None) == "unknown"

    def test_strips_mandate_label(self):
        assert clean_fraction_name("Dorothee Martin (Bundestag 2017 - 2021)") == "Dorothee Martin"


class TestTally:
    def _votes(self):
        rows = []
        for i, (frac, value) in enumerate(
            [("SPD", "yes")] * 6 + [("SPD", "no")] * 1 + [("SPD", "no_show")] * 2 + [("AfD", "no")] * 3 + [("Grüne", "yes"), ("Grüne", "no")]
        ):
            rows.append(vote(i, i, f"MP {i} (Bundestag 2025 - 2029)", 1, value, f"{frac} (Bundestag 2025 - 2029)"))
        return rows

    def test_overall_and_by_fraction(self):
        t = tally_votes(self._votes())
        assert t["overall"] == {"yes": 7, "no": 5, "abstain": 0, "no_show": 2, "members": 14}
        spd = next(f for f in t["by_fraction"] if f["fraction"] == "SPD")
        assert (spd["yes"], spd["no"], spd["no_show"], spd["members"]) == (6, 1, 2, 9)
        assert spd["majority_position"] == "yes" and not spd["unanimous"]
        assert round(spd["majority_share"], 3) == round(6 / 7, 3)  # no_show members do not count as voting

    def test_fractions_sorted_by_size(self):
        names = [f["fraction"] for f in tally_votes(self._votes())["by_fraction"]]
        assert names == ["SPD", "AfD", "Grüne"]

    def test_tie_has_no_majority(self):
        assert faction_position({"yes": 2, "no": 2, "abstain": 0, "no_show": 1})["majority_position"] is None

    def test_nobody_voted(self):
        assert faction_position({"yes": 0, "no": 0, "abstain": 0, "no_show": 4}) == {"majority_position": None, "majority_share": None, "unanimous": None}

    def test_unanimous(self):
        assert faction_position({"yes": 5, "no": 0, "abstain": 0, "no_show": 1})["unanimous"] is True

    def test_unknown_vote_values_are_ignored(self):
        v = vote(1, 1, "A (Bundestag 2025 - 2029)", 1, "weird", "SPD (Bundestag 2025 - 2029)")
        assert tally_votes([v])["overall"]["members"] == 0


class TestSlimPoll:
    def test_fields(self):
        idx = {161: {"parliament": "Bundestag", "parliament_id": 5}}
        p = slim_poll(poll(7, "Titel", "2026-01-02", (161, "Bundestag 2025 - 2029"), topics=[(20, "Energie")], accepted=False), idx)
        assert p["poll_id"] == 7 and p["date"] == "2026-01-02" and p["accepted"] is False
        assert p["parliament"] == "Bundestag" and p["topics"] == [{"id": 20, "label": "Energie"}]
        assert p["intro"] == "Intro text"

    def test_intro_can_be_dropped(self):
        assert "intro" not in slim_poll(poll(7, "T", "2026-01-02", (161, "X")), intro_chars=None)
