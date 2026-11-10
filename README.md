# crm-report-pipeline

![tests](https://github.com/ryansomers/crm-report-pipeline/actions/workflows/test.yml/badge.svg)

> Demo rebuild of a pattern I've deployed for clients, on synthetic/public data. No client code or data.

I built this pipeline to pull contacts, deals and activities out of a CRM's paginated REST API,
validate them, and upsert them into PostgreSQL without creating duplicates. Each week it computes
sales metrics in SQL (new deals, won/lost, win rate, pipeline by stage, top owners, week-over-week
changes) and has an LLM write a short summary of those numbers. The LLM sees only the computed
metrics, and every figure it writes is checked against them before the report is saved. The CRM
here is a mock FastAPI service seeded with Faker. It returns rate limits, server errors and
malformed records on purpose, so the retry and reject paths get exercised. The full flow runs on
PostgreSQL through Docker Compose, or on SQLite with no Docker and no API key.

## Architecture

```mermaid
flowchart LR
    subgraph CRM["mock_crm (FastAPI)"]
        API["/contacts /deals /activities<br/>cursor paging, updated_since<br/>429 + Retry-After, 503, bad records"]
    end
    subgraph P["pipeline"]
        X["extract.py<br/>httpx, retry/backoff+jitter"] --> V["models.py<br/>pydantic validation"]
        V -->|valid| L["load.py<br/>INSERT ... ON CONFLICT DO UPDATE"]
        V -->|invalid| R[(rejects)]
    end
    subgraph DB["PostgreSQL 16 / SQLite"]
        T[(contacts, deals,<br/>activities)]
        M[(pipeline_runs,<br/>watermarks)]
    end
    subgraph RP["report"]
        S["metrics.py + sql/*.sql<br/>aggregates, window functions"] --> J{{metrics JSON}}
        J --> LLM["summarize.py<br/>Claude via Anthropic SDK<br/>or template"]
        LLM --> C["verify.py<br/>every figure traceable?"]
        C -->|pass| O["reports/weekly-YYYY-MM-DD<br/>.md .html .metrics.json"]
        C -->|fail: retry once,<br/>then template| LLM
    end
    API --> X
    L --> T
    L --> M
    T --> S
```

### Schema

```mermaid
erDiagram
    contacts ||--o{ deals : "contact_id (logical)"
    deals ||--o{ activities : "deal_id (logical)"
    contacts ||--o{ activities : "contact_id (logical)"
    pipeline_runs ||--o{ rejects : run_id
    pipeline_runs ||--o{ watermarks : run_id

    contacts {
        varchar id PK "CRM source id"
        varchar first_name
        varchar last_name
        varchar email
        varchar company
        varchar title
        timestamp created_at
        timestamp updated_at
        int loaded_run_id
        timestamp loaded_at
    }
    deals {
        varchar id PK "CRM source id"
        varchar contact_id
        varchar name
        varchar owner
        varchar stage "prospecting..closed_lost"
        numeric amount "NUMERIC(14,2)"
        varchar currency
        timestamp created_at
        date close_date
        timestamp updated_at
        int loaded_run_id
        timestamp loaded_at
    }
    activities {
        varchar id PK "CRM source id"
        varchar deal_id
        varchar contact_id
        varchar type "call|email|meeting"
        text subject
        timestamp occurred_at
        timestamp updated_at
        int loaded_run_id
        timestamp loaded_at
    }
    pipeline_runs {
        int id PK
        timestamp started_at
        timestamp finished_at
        varchar status "running|success|failed"
        varchar mode "full|incremental"
        int contacts_loaded
        int deals_loaded
        int activities_loaded
        int rejects
        text error
    }
    watermarks {
        varchar resource PK
        timestamp high_watermark "max updated_at loaded"
        int run_id
    }
    rejects {
        int id PK
        int run_id
        varchar resource
        varchar source_id
        text reason
        text payload "original JSON"
        timestamp rejected_at
    }
```

All timestamps are stored as UTC without a time zone.

## How to run

From a fresh clone (Python 3.11+ and [uv](https://docs.astral.sh/uv/)):

```bash
uv venv .venv && uv pip install -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/pytest                      # 46 tests, no network/Docker/API key
```

**Quickest demo (SQLite, no Docker, no API key):**

```bash
.venv/bin/crm-pipeline run --in-process-mock --db-url sqlite:///crm.db
.venv/bin/crm-pipeline report --db-url sqlite:///crm.db --week 2026-11-02 --no-llm
# -> reports/weekly-2026-11-02.md, .html, .metrics.json
```

`--in-process-mock` serves the mock CRM inside the same process, with 429s, 503s and about 2%
corrupt records switched on.

**Full stack with PostgreSQL 16 (Docker Compose):**

```bash
make demo        # = docker compose up -d --build db mock-crm
                 #   docker compose run --rm pipeline run
                 #   docker compose run --rm pipeline report --week 2026-11-02 --no-llm
make test-pg     # PostgreSQL integration tests against the compose database
make down
```

**Running the parts separately:**

```bash
crm-pipeline serve-mock --port 8000 --rate-limit-every 7 --fail-every 11 --corrupt-rate 0.02
crm-pipeline run --db-url postgresql+psycopg://crm:crm@localhost:5432/crm --base-url http://localhost:8000
curl -X POST 'localhost:8000/_admin/simulate-changes?n=10'   # edit some deals
crm-pipeline run --db-url ...          # incremental: only rows changed since the watermark
crm-pipeline report --db-url ... --week 2026-11-02          # LLM summary (needs ANTHROPIC_API_KEY)
```

Environment variables are listed in `.env.example`: `DATABASE_URL`, `CRM_BASE_URL`,
`ANTHROPIC_API_KEY`, `LLM_MODEL` (default `claude-sonnet-5-5`) and `TEST_PG_URL`.

## Design decisions

**Keyset (cursor) pagination.** The mock API sorts by `(updated_at, id)`. Its cursor is an opaque
encoding of the last pair the client has seen. With `?page=N` offsets, a record that changes while
the client is paging moves every later row, so rows get skipped or repeated. A keyset cursor resumes
right after the last row seen. A record edited mid-scan can still show up twice. The extractor
keeps the newest copy, because PostgreSQL rejects an `ON CONFLICT DO UPDATE` that touches the same
key twice in one statement.

**Retries are hand-rolled, not tenacity.** The rules are short enough to read in one function
(`CRMClient.get_json`), and tests can check them with a fake `sleep`:
- On a 429, wait exactly `Retry-After` (in seconds or as an HTTP date, capped at 120 s).
- On a 5xx or a network error, use exponential backoff with full jitter:
  `uniform(0, min(cap, base * 2**attempt))`.
- Don't retry other 4xx errors.

**Validation creates rejects instead of failures.** Each record is parsed by a pydantic model
(stage enum, non-negative `Decimal` amount, a timezone-aware timestamp, and a `close_date` on
closed deals). A bad record is written to `rejects` with a readable reason and its original JSON,
and the rest of the batch still loads. A timestamp without a timezone is rejected rather than
guessed, because a wrong guess would quietly shift data by hours.

**Idempotent upserts.** CRM ids are the primary keys, and every load is
`INSERT ... ON CONFLICT (id) DO UPDATE ... WHERE stored.updated_at <= incoming.updated_at`.
Re-running a load changes nothing. The `WHERE` guard means an older copy of a record can never
overwrite a newer one. SQLAlchemy exposes this through dialect-specific `insert()` constructs, so
`load._dialect_insert` picks the PostgreSQL or SQLite one. The row count comes from `RETURNING`,
because psycopg reports `rowcount = -1` for these statements.

**Watermarks are committed with the data.** Each resource loads in a single transaction together
with its new high watermark, so a crash can never advance the watermark past data that wasn't
loaded. Incremental runs request `updated_since >= watermark` (inclusive). Rows sharing the
boundary timestamp get read again, and the upsert absorbs them.

**No enforced foreign keys between CRM tables.** The source system doesn't guarantee referential
integrity. A deal can point to a contact that was rejected or hasn't synced yet, and failing a
whole batch over that would also block good data. The relationships are logical (dashed in the
ER diagram).

**SQL lives in `.sql` files.** `report/sql/*.sql` is portable between PostgreSQL and SQLite: it
uses aggregate `FILTER`, `RANK()` and `NULLIF` for division by zero. The one query that needs
dialect features has two versions with the same output columns:

| Query | PostgreSQL | SQLite |
|---|---|---|
| `weekly_trend.sql` | `generate_series()` week spine, `date_trunc('week', ...)` | recursive CTE spine, `date(ts, '-N days')` from `strftime('%w')` |

Both versions compute week-over-week changes with `LAG() OVER (ORDER BY week_start)`. The week
spine matters because `LAG` looks at the previous *row*. If a week with no deals were missing,
"last week" would silently become two weeks ago. A test runs the same hand-computed fixture
against both dialects and expects identical output.

**Reliable LLM summaries.** I don't rely on the prompt alone:
1. The model only gets the metrics JSON, never raw rows, so it has nothing to compute its own
   aggregates from.
2. The prompt forbids new figures and arithmetic. Deltas are already in the JSON.
3. Structured output (`client.beta.messages.parse(output_format=WeeklySummary)`) returns a
   validated pydantic object rather than free text.
4. `report/verify.py` pulls every number and date out of the summary. A figure passes only if it
   is a correct rounding of some metric at the precision it was written: `$3,501` and `$3.5k`
   both match 3500.5, `$3.6k` doesn't. If a figure fails, the model gets one retry with the
   offending figures listed. If it fails again, the report uses the deterministic template
   summary and records why. An API error or a refusal also falls back to the template.

The LLM sits behind a two-method `Summarizer` protocol, so tests inject a fake client.
One test drives the real Anthropic SDK against a mock HTTP transport to check the request format
without any network call.

## Results

- **Tests:** 46 pass by default (SQLite plus in-process FastAPI; no network, Docker or key).
  3 PostgreSQL integration tests (`pytest -m integration`) also pass against a local
  PostgreSQL 16.
- **Demo run** (`run --in-process-mock`, default seed 42): 1,450 source records, 1,420 loaded,
  30 rejected (3 contacts, 7 deals, 20 activities), 18 HTTP requests for 15 pages (3 were retries after
  injected 429/503 responses). A second run against unchanged data writes 3 boundary rows and inserts no
  duplicates.
- The same demo data loaded into PostgreSQL 16 and SQLite produces identical metrics JSON.

### Sample report

Generated with `--no-llm` on the seeded demo data (full files: [docs/sample-report.md](docs/sample-report.md),
[docs/sample-report.html](docs/sample-report.html), [docs/sample-metrics.json](docs/sample-metrics.json)):

```markdown
# Weekly Sales Report: 2026-11-02 to 2026-11-08

_Summary source: template. Figure check: passed (23 figures checked)._

## Summary

**Week of 2026-11-02: 21 deals won for $435,747.83 at a 43.8% win rate.**

- 21 deals won for $435,747.83, up $84,884.58 (24.2%) week over week.
- 27 deals lost ($461,026.84); win rate 43.8%.
- 35 new deals worth $572,570.96, up 4 (12.9%) week over week.
- Open pipeline: 120 deals worth $2,453,525.80.
- Top owner by won value: Connie Lawrence with $191,433.12 from 9 deals.

**Watch:**

- Losses up 15 (125.0%) week over week.
- Largest open stage is negotiation: 42 deals, $892,517.12.

## Key metrics

| Metric | This week | Last week | Change |
|---|---|---|---|
| New deals | 35 | 31 | +4 (+12.9%) |
| New deal value | $572,570.96 | $704,037.64 | -$131,466.68 (-18.7%) |
| Deals won | 21 | 16 | +5 (+31.2%) |
| Won value | $435,747.83 | $350,863.25 | +$84,884.58 (+24.2%) |
| Deals lost | 27 | 12 | +15 (+125.0%) |
| Win rate | 43.8% | n/a | n/a |

## Open pipeline by stage

| Stage | Deals | Value |
|---|---|---|
| prospecting | 36 | $780,990.58 |
| qualification | 24 | $378,332.48 |
| proposal | 18 | $401,685.62 |
| negotiation | 42 | $892,517.12 |
| **Total** | 120 | $2,453,525.80 |

## Top owners by won value

| Rank | Owner | Won deals | Won value |
|---|---|---|---|
| 1 | Connie Lawrence | 9 | $191,433.12 |
| 2 | Cristian Santos | 3 | $103,346.57 |
| 3 | Daniel Wagner | 5 | $81,386.91 |

## Trend (last 6 weeks)

| Week | New deals | Won | Won value | Lost |
|---|---|---|---|---|
| 2026-09-28 | 38 | 8 | $143,232.68 | 7 |
| 2026-10-05 | 27 | 13 | $219,066.53 | 12 |
| 2026-10-12 | 34 | 4 | $39,122.78 | 21 |
| 2026-10-19 | 26 | 11 | $190,296.47 | 14 |
| 2026-10-26 | 31 | 16 | $350,863.25 | 12 |
| 2026-11-02 | 35 | 21 | $435,747.83 | 27 |
```

## Limitations

- The figure check proves every number can be traced to the metrics. It doesn't prove the number
  is attached to the right label: "won 3 deals" passes if *any* metric is 3. Closing that gap
  would take per-claim citations (metric path plus value) in the structured output.
- I haven't run the LLM path against the live API here, because that costs money. It is covered
  by a fake client and by a wire-format test of the SDK request. Run `make demo-llm` with a key
  to try it.
- "Open pipeline by stage" uses each deal's *current* stage. There is no stage history table, so
  past weeks' pipeline can't be reconstructed.
- The mock injects failures by counter (every Nth request), not at random. That keeps tests
  deterministic, but it is gentler than a real API.
- Single currency (USD). There is no FX conversion.
- Deletes in the source aren't propagated. That would need tombstones or a periodic full
  reconciliation.
- I validated the Docker Compose file and the Dockerfile but didn't build or run them in this
  environment. The PostgreSQL paths were tested against a locally installed PostgreSQL 16.
