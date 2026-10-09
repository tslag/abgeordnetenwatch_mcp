"""PDF helpers, raw-query validation, mandate logic and server composition."""
from __future__ import annotations

import json
import re
from datetime import date

import pytest
from builders import mandate, parliament, period
from fastmcp import Client

from aw.errors import AWApiError, AWUserError
from aw.mandates import load_mandates
from resources.prompts import GUIDE_PROMPT, INSTRUCTION_PROMPT
from tools.programs import (
    chunk_text,
    extract_pages,
    parse_page_range,
    search_pages,
    strip_line_numbers,
    take_pages,
    validate_pdf_url,
)
from tools.raw import compact, validate_filters

EXPECTED_TOOLS = {
    "aw_lookup", "aw_find_politician", "aw_get_politician", "aw_search_polls", "aw_poll_result", "aw_politician_votes",
    "aw_compare_votes", "aw_get_sidejobs", "aw_committees", "aw_find_election_programs", "aw_read_election_program",
}


def make_pdf(pages: list[str]) -> bytes:
    """A minimal valid text PDF (one Helvetica line per page) so extraction is tested end to end."""
    objects: list[bytes] = []
    n = len(pages)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_obj = 3 + 2 * n
    for i, text in enumerate(pages):
        content_obj = 4 + 2 * i
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_obj} 0 R /Resources << /Font << /F1 {font_obj} 0 R >> >> >>".encode())
        stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode()
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


class TestPdfExtraction:
    def test_extracts_text_per_page(self):
        pages = extract_pages(make_pdf(["Seite eins Mindestlohn", "Seite zwei Steuern"]))
        assert pages == ["Seite eins Mindestlohn", "Seite zwei Steuern"]

    def test_garbage_is_an_error_for_the_caller(self):
        with pytest.raises(Exception):  # noqa: B017 - pypdf's error type varies; load_pdf_pages wraps it in AWApiError
            extract_pages(b"not a pdf")


class TestPdfUrlGuard:
    @pytest.mark.parametrize("url", [
        "https://www.abgeordnetenwatch.de/sites/default/files/x.pdf",
        "https://abgeordnetenwatch.de/x.pdf",
    ])
    def test_allowed(self, url):
        validate_pdf_url(url)

    @pytest.mark.parametrize("url", [
        "http://www.abgeordnetenwatch.de/x.pdf",
        "https://evil.example.com/abgeordnetenwatch.de.pdf",
        "https://abgeordnetenwatch.de.evil.com/x.pdf",
        "https://notabgeordnetenwatch.de/x.pdf",
        "file:///etc/passwd",
    ])
    def test_rejected(self, url):
        with pytest.raises(AWUserError, match="Refusing"):
            validate_pdf_url(url)


class TestLineNumbers:
    def test_margin_numbers_removed_but_real_numbers_kept(self):
        text = "857 Der Mindestlohn 858 steigt 2026 auf 15 Euro 859 und die 860 Tarifbindung 861 wächst um 20 Prozent 862 weiter"
        assert strip_line_numbers(text) == "Der Mindestlohn steigt 2026 auf 15 Euro und die Tarifbindung wächst um 20 Prozent weiter"

    def test_short_chains_are_left_alone(self):
        assert strip_line_numbers("In den Jahren 2025 2026 2027 stieg es") == "In den Jahren 2025 2026 2027 stieg es"

    def test_empty(self):
        assert strip_line_numbers("") == ""


class TestSearch:
    DOC = [
        "Inhaltsverzeichnis",
        "Wir erhöhen den Mindestlohn auf 15 Euro. Gute Löhne sichern Wohlstand. Die Tarifbindung wird gestärkt.",
        "Wir entlasten kleine Einkommen bei den Steuern. Hohe Vermögen tragen mehr bei. Das Steuersystem wird gerechter.",
        "Der Klimaschutz bleibt unser Ziel. Wir bauen erneuerbare Energien aus.",
    ]

    def test_stems_and_compounds(self):
        hits = search_pages(self.DOC, "Steuer", top_k=3, max_chars=2000)
        assert hits and hits[0]["page"] == 3
        assert "Steuersystem" in hits[0]["text"] or "Steuern" in hits[0]["text"]

    def test_multiple_terms_prefer_pages_with_more_hits(self):
        hits = search_pages(self.DOC, "Mindestlohn Löhne Tarifbindung", top_k=3, max_chars=2000)
        assert hits[0]["page"] == 2

    def test_no_terms_or_no_hits(self):
        assert search_pages(self.DOC, "und der die", 3, 1000) == []  # stop words only
        assert search_pages(self.DOC, "Raumfahrt", 3, 1000) == []

    def test_budget_limits_total_characters(self):
        hits = search_pages(self.DOC * 3, "Wir", top_k=10, max_chars=120)
        assert sum(len(h["text"]) for h in hits) <= 125

    def test_chunking_respects_sentences(self):
        chunks = chunk_text("Erster Satz hier. Zweiter Satz hier. Dritter Satz hier.", target=30)
        assert len(chunks) == 3 and all(c.endswith(".") for c in chunks)


class TestPageRanges:
    @pytest.mark.parametrize(("spec", "expected"), [("7", (7, 7)), ("12-14", (12, 14)), (" 3 - 5 ", (3, 5)), ("2-99", (2, 20)), ("5-2", (5, 5))])
    def test_valid(self, spec, expected):
        assert parse_page_range(spec, 20) == expected

    @pytest.mark.parametrize("spec", ["abc", "1-2-3", "", "-3"])
    def test_invalid(self, spec):
        with pytest.raises(AWUserError, match="Cannot read pages"):
            parse_page_range(spec, 10)

    def test_out_of_range(self):
        with pytest.raises(AWUserError, match="does not exist"):
            parse_page_range("11", 10)

    def test_take_pages_stops_at_the_character_budget(self):
        doc = ["a" * 100, "b" * 100, "c" * 100]
        text, last = take_pages(doc, 1, 3, max_chars=230)
        assert last == 2 and "[page 2]" in text and "[page 3]" not in text


# --- raw query ------------------------------------------------------------------------------------------
class TestRawQuery:
    def test_valid_filters(self):
        f = {"label[cn]": "Klima", "field_poll_date[gte]": "2026-01-01", "party": 1, "politician[entity.party.entity.id][cn]": "so"}
        assert validate_filters(f) == f

    @pytest.mark.parametrize("bad", ["range_end", "sort_by", "related_data", "page[eq]", "x y", "a;b", "label[nope]"])
    def test_rejected_keys(self, bad):
        with pytest.raises(AWUserError):
            validate_filters({bad: 1})

    def test_compact_drops_noise_and_flattens_references(self):
        row = {"id": 1, "entity_type": "poll", "api_url": "x", "label": "L", "party": {"id": 1, "entity_type": "party", "label": "SPD", "api_url": "u"}, "n": 3}
        assert compact(row) == {"id": 1, "label": "L", "party": {"id": 1, "label": "SPD"}, "n": 3}

    def test_compact_keeps_small_top_level_rows_intact(self):
        party = {"id": 1, "entity_type": "party", "label": "SPD", "api_url": "u", "full_name": "Sozialdemokraten", "short_name": "SPD"}
        assert compact(party) == {"id": 1, "label": "SPD", "full_name": "Sozialdemokraten", "short_name": "SPD"}

    def test_compact_flattens_reference_lists(self):
        row = {"id": 1, "field_topics": [{"id": 20, "entity_type": "taxonomy_term", "label": "Energie", "api_url": "u", "abgeordnetenwatch_url": "w"}]}
        assert compact(row) == {"id": 1, "field_topics": [{"id": 20, "label": "Energie"}]}


# --- mandates ---------------------------------------------------------------------------------------------
class TestMandates:
    async def test_is_current_requires_running_period_and_no_end(self, api, client):
        BT = (5, "Bundestag")
        api.add("parliaments", [parliament(5, "Bundestag", (161, "Bundestag 2025 - 2029"))])
        api.add("parliament-periods", [period(161, "Bundestag 2025 - 2029", BT), period(132, "Bundestag 2021 - 2025", BT)])
        api.add("candidacies-mandates", [
            mandate(1, 9, "A B", (161, "Bundestag 2025 - 2029")),
            mandate(2, 9, "A B", (132, "Bundestag 2021 - 2025")),
        ])
        rows = (await load_mandates(client.session(), [9], today=date(2026, 10, 1)))[9]
        assert {r["period_id"]: r["is_current"] for r in rows} == {161: True, 132: False}

    async def test_resigned_mandate_is_not_current(self, api, client):
        BT = (5, "Bundestag")
        api.add("parliaments", [parliament(5, "Bundestag", (161, "Bundestag 2025 - 2029"))])
        api.add("parliament-periods", [period(161, "Bundestag 2025 - 2029", BT)])
        api.add("candidacies-mandates", [mandate(1, 9, "A B", (161, "Bundestag 2025 - 2029"), end_date="2026-02-01")])
        rows = (await load_mandates(client.session(), [9], today=date(2026, 10, 1)))[9]
        assert rows[0]["is_current"] is False

    async def test_requests_use_current_on_all_and_group_by_politician(self, api, client):
        BT = (5, "Bundestag")
        api.add("parliaments", [parliament(5, "Bundestag", (161, "x"))])
        api.add("parliament-periods", [period(161, "x", BT)])
        api.add("candidacies-mandates", [mandate(1, 1, "A", (161, "x")), mandate(2, 2, "B", (161, "x")), mandate(3, 3, "ignored", (161, "x"))])
        rows = await load_mandates(client.session(), [1, 2])
        assert set(rows) == {1, 2} and [r["mandate_id"] for r in rows[1]] == [1]
        call = api.calls("candidacies-mandates")[0]
        assert call["current_on"] == "all" and call["politician[in]"] == "[1,2]"


# --- server composition -----------------------------------------------------------------------------------
class TestServer:
    async def test_registered_tools_and_annotations(self, mcp_client):
        tools = {t.name: t for t in await mcp_client.list_tools()}
        assert set(tools) == EXPECTED_TOOLS
        for t in tools.values():
            a = t.annotations
            assert a.read_only_hint is True and a.destructive_hint is False and a.idempotent_hint is True and a.open_world_hint is True
            assert t.description and len(t.description) > 40

    async def test_raw_query_is_off_by_default_and_enabled_by_env(self, mcp_client, monkeypatch):
        from server import build_server

        assert "aw_query" not in {t.name for t in await mcp_client.list_tools()}
        monkeypatch.setenv("AW_ENABLE_RAW_QUERY", "1")
        async with Client(build_server()) as c:
            assert "aw_query" in {t.name for t in await c.list_tools()}

    async def test_resources_templates_and_prompts(self, mcp_client):
        uris = {str(r.uri) for r in await mcp_client.list_resources()}
        assert {"aw://guide", "aw://info", "aw://codebook", "aw://parliaments", "aw://coverage", "aw://topics"} <= uris
        templates = {t.uri_template for t in await mcp_client.list_resource_templates()}
        assert templates == {"aw://parliament/{parliament_id}/periods", "aw://politician/{politician_id}", "aw://poll/{poll_id}"}
        prompts = {p.name for p in await mcp_client.list_prompts()}
        assert prompts == {"compare_votes_with_program", "mp_overview", "explain_vote", "party_positions_on_topic"}

    async def test_codebook_resource(self, mcp_client):
        text = (await mcp_client.read_resource("aw://codebook"))[0].text
        book = json.loads(text)
        assert book["income_levels"]["0"] == "1-1.000 EUR" and book["income_levels"]["10"] == "over 250.000 EUR"
        assert book["sidejob_categories"]["29647"]["label"] == "Paid activities besides the mandate"
        assert set(book["vote_values"]) == {"yes", "no", "abstain", "no_show"}

    async def test_instructions_and_guide_only_mention_real_tools(self, mcp_client):
        mentioned = set(re.findall(r"\baw_[a-z_]+\b", INSTRUCTION_PROMPT + GUIDE_PROMPT))
        assert mentioned - {"aw_query"} <= EXPECTED_TOOLS, mentioned - EXPECTED_TOOLS

    async def test_prompts_only_mention_real_tools(self, mcp_client):
        for p in await mcp_client.list_prompts():
            args = {a.name: "Olaf Scholz" if a.required else None for a in (p.arguments or [])}
            result = await mcp_client.get_prompt(p.name, {k: v for k, v in args.items() if v})
            text = " ".join(m.content.text for m in result.messages)
            assert set(re.findall(r"\baw_[a-z_]+\b", text)) <= EXPECTED_TOOLS, p.name
            assert "roll-call" in text.lower()  # the mandatory caveats are present

    def test_instruction_prompt_is_short(self):
        assert len(INSTRUCTION_PROMPT.split()) < 330


class TestToolErrorTranslation:
    async def test_api_error_is_a_tool_error_not_a_crash(self, api, mcp_client):
        api.add_error("parliaments", "kaputt")
        result = await mcp_client.call_tool("aw_lookup", {"kind": "parliament"}, raise_on_error=False)
        assert result.is_error and "kaputt" in result.content[0].text

    async def test_rate_limit_message(self, api, mcp_client):
        import httpx

        api.add_response("parliaments", lambda r: httpx.Response(429))
        result = await mcp_client.call_tool("aw_lookup", {"kind": "parliament"}, raise_on_error=False)
        assert result.is_error and "rate limiting" in result.content[0].text


def test_api_error_class_exposes_message():
    assert AWApiError("x", 500).message == "x"
