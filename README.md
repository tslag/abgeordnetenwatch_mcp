# Abgeordnetenwatch MCP

A read-only [MCP](https://modelcontextprotocol.io) server for the open [Abgeordnetenwatch API](https://www.abgeordnetenwatch.de/api)
(CC0): roll-call votes, MPs, side jobs, committees and party election programmes for the **Bundestag, the EU Parliament
and the 16 state parliaments**. Built so an LLM can answer questions such as

> *How does Olaf Scholz's recent voting match the election programme of his party?*

without a planner tool inside the server: the client model plans, the server provides a small set of task-oriented,
request-efficient, hard-to-misuse tools.

## Run

```bash
uv sync
uv run python src/server.py          # stdio transport (src/main.py does the same)
```

Register it in Claude Code:

```bash
claude mcp add abgeordnetenwatch -- uv run --directory <path-to-this-repo> python src/server.py
```

or in a desktop client's `mcp.json`:

```json
{ "mcpServers": { "abgeordnetenwatch": { "command": "uv", "args": ["run", "--directory", "<path-to-this-repo>", "python", "src/server.py"] } } }
```

| Environment variable | Default | Meaning |
|---|---|---|
| `AW_CACHE_PATH` | *(off)* | sqlite file; keeps cached API answers across restarts |
| `AW_RATE_LIMIT` | `12` | sustained API requests per minute; the API documents 30, but a measured steady state of ~15/min was where 429s began |
| `AW_RATE_BURST` | `4` | requests that may go out back to back (spaced 1.5 s apart) before the sustained rate applies |
| `AW_ENABLE_RAW_QUERY` | *(off)* | set to `1` to also expose the `aw_query` escape hatch |

## Tools

All tools are read-only. Typical flow: **`aw_find_politician` → id → everything else.**

| Tool | Purpose |
|---|---|
| `aw_find_politician` | Name → `politician_id`. Handles umlaut spellings, returns every candidate with mandates and says when the user must choose. |
| `aw_get_politician` | Profile and career timeline: mandates (parliament, period, faction, how the seat was won) and candidacies (party they ran for), each with its *election* period. |
| `aw_politician_votes` | How an MP voted, newest first, with poll date/title/topics. Filter by topic, keyword, dates, vote value. Counts cover all matches, not just the page. Default scope: most recent mandate. |
| `aw_search_polls` | Find roll-call polls by title keyword, topic, parliament, period, dates, adopted/rejected. |
| `aw_poll_result` | Result of one poll computed per faction (the API has no aggregate). With `politician_id`: that MP's vote and whether it followed the faction majority; can list dissenters. |
| `aw_compare_votes` | Agreement rate of 2–3 MPs and the polls where they differed. |
| `aw_get_sidejobs` | Declared side jobs with decoded categories and income-level ranges (Bundestag only; other parliaments report `not_available`, not "none"). |
| `aw_committees` | An MP's committee memberships, a committee's members (with politician ids), or browse committees. |
| `aw_find_election_programs` | Party programmes (PDF link) for a party/parliament/period — or the one an MP actually ran on. |
| `aw_read_election_program` | Search (ranked passages with page numbers) or read the programme PDF text. The only tool beyond the JSON API. |
| `aw_lookup` | Names → ids for parliament, period, topic, party, fraction, committee, constituency. |
| `aw_query` *(opt-in)* | Bounded raw access to any entity as a last resort. |

Every result has the same shape: `summary`, `data`, optional `page` (`total/returned/offset/has_more`), `notes` (caveats and
next-step hints), `sources` (abgeordnetenwatch.de URLs to cite) and `meta` (`requests`, `cached`, `data_as_of`).

## Resources and prompts

* `aw://guide` – routing table, recipes, pitfalls · `aw://codebook` – decoders (vote values, income levels 0–10, side-job
  categories, committee roles…) · `aw://coverage`, `aw://parliaments`, `aw://topics`, `aw://info`
* Templates: `aw://parliament/{id}/periods`, `aw://politician/{id}`, `aw://poll/{id}`
* MCP prompts (user-invoked workflows): `compare_votes_with_program`, `mp_overview`, `explain_vote`, `party_positions_on_topic`

## What the API forced into the design

These were verified against the live API (v2.9.0); the rest of the code depends on them.

| Fact | Consequence |
|---|---|
| Documented 30 requests/min per IP, **measured ~15/min sustained with a burst of ~10, and ≥1.2 s between requests** | Pacing limiter (12/min, burst 4 spaced 1.5 s, halves its pace after a 429), caching, per-call request budget, bulk fetch + local join. |
| `votes` rows have no date or topic and cannot be filtered by poll fields (HTTP 500) | A voting record is a join per legislature period: the MP's votes ⋈ the period's polls (filtered by topic/date/title). |
| No aggregate endpoints | Faction tallies are computed server-side from `polls/{id}?related_data=votes`. |
| `candidacies-mandates` hides former mandates unless `current_on=all` | Always sent; `is_current` is derived. |
| Name search is "contains" and the API does not transliterate (`Mueller` ≠ `Müller`) | Ambiguity is the norm → analysis tools take a numeric `politician_id`; spelling variants are tried server-side. |
| Endpoint names are irregular (`election-program` is singular) | Hard-coded endpoint map. |
| Sidejobs are published for the Bundestag only; income is a level range 0–10, an exact amount only for some entries | Decoded; "not reported" ≠ zero; `coverage: not_available` for other parliaments. |
| Election programmes are PDFs | Metadata via API, text extracted locally (host allow-list, size cap, output flagged untrusted). |
| Numeric string keys (`income_level`) compare as text in API filters | Such filters are applied locally. |

## Principles

* Report neutrally and cite the URL; never infer motives. Only roll-call votes exist — a missing vote is not a position.
* A party programme is not an individual's belief, and a vote along the faction line says little about conviction —
  hence `followed_fraction_majority` and the dissenter listing.
* Third-party text (poll descriptions, side-job notes, PDF text) is returned as data, HTML stripped.

## Development

```bash
uv run pytest                 # 168 offline tests (fake API; no network)
uv run pytest -m live         # smoke tests against the real API (~1 min, rate-limited)
```

```
src/server.py            wiring only
src/aw/                  API layer: client (limiter, cache, budget), endpoints+codebook, reference data, mandates, polls/tallies, text
src/tools/               one module per tool family
src/resources/           guide text and MCP resources
src/prompts/             MCP prompts
tests/                   builders.py (response shapes), conftest.py (FakeAPI), unit + tool + live tests
```
