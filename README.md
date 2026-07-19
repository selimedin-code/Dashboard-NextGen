# NextGen Fund Dashboard

Internal monitoring tool for a ~55-position US equity fund concentrated in AI and
compute infrastructure, organized across an 11-pillar thematic framework. Two
users: the fund manager and one reviewer. A secondary purpose is coaching — making
it easy to see where returns came from and whether the entry reasoning held up.

See [`nextgen_roadmap_v2.md`](nextgen_roadmap_v2.md) for the full architecture and
phase plan, and [`nextgen_fundamentals_brief.md`](nextgen_fundamentals_brief.md)
for the cached-fundamentals data layer.

## Architecture

Snapshot-based, not a transaction ledger. The custodian's authoritative holdings
file is uploaded per period; everything else — prices, market value, weights, P&L,
and the transaction history itself — is **derived** by the dashboard (transaction
history by diffing consecutive snapshots).

**Stack:** FastAPI + PostgreSQL + Jinja2 (server-rendered), Alembic migrations,
deployed GitHub → Render. HTTP Basic Auth in front of everything.

## Status

**Phase 1 complete.** On top of the scaffold (app skeleton, schema, migrations,
config, auth), the snapshot ingestion flow is built and tested:

- **Upload** a custodian file at `/upload` — Excel (`.xlsx`, header-mapped, the
  preferred format) or PDF (the rendered custodian report, parsed by x-position
  column bucketing so kerning-split numbers still read correctly).
- **Validate** — blocking checks (duplicate ticker, non-positive units/cost,
  duplicate `as_of`, live-quote hook) and warnings (new tickers, >20% count drift,
  >5x unit swings, duplicate ISIN). The live-quote check is a hook that currently
  warns-and-skips until the Phase 4 price layer lands.
- **Preview** — a full diff against the last snapshot (opens/closes/adds/trims),
  inline pillar assignment for new tickers, and the complete parsed holdings table,
  shown *before* anything is written.
- **Commit** — transactional write of the snapshot, holdings, and securities
  metadata. Re-committing an existing `as_of` requires an explicit overwrite
  confirmation.

Verified end-to-end against the real `CurrentPortfolio.pdf` (55 positions + cash,
exact units/cost match). Next: **Phase 2** (diff engine → `changes` table) and
**Phase 3** (position table with live prices).

## Local development

Prerequisites: Python 3.14, Homebrew PostgreSQL 16.

```bash
# 1. Postgres (once)
brew install postgresql@16
brew services start postgresql@16
createdb nextgen

# 2. App
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # or requirements.lock.txt for the exact pinned set
cp .env.example .env                          # adjust if needed

# 3. Migrate + run
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Then open http://127.0.0.1:8000 (user `nextgen`, pass from `.env`).
`make help` lists convenience targets. Health check: `GET /healthz` (unauthenticated).

### Tests

```bash
createdb nextgen_test          # once — DB tests need real Postgres (JSONB/NUMERIC)
make test                      # or: .venv/bin/python -m pytest tests/ -q
```

## Layout

```
app/
  main.py           FastAPI app: home page, health check, Jinja filters
  routes_upload.py  Phase 1 upload → preview → commit routes
  config.py         env-driven settings (pydantic-settings)
  db.py             SQLAlchemy engine, session, declarative Base
  models.py         ORM models — domain tables + upload staging
  auth.py           HTTP Basic Auth dependency
  ingest/           snapshot ingestion
    normalize.py    ticker + decimal normalization
    parser.py       Excel (header-mapped) + PDF (column-bucketed) parsers
    validate.py     blocking/warning checks + change preview
    commit.py       transactional snapshot commit
  templates/        Jinja2 (base, index, upload, upload_preview)
  static/           app.css, logo.svg
alembic/            migration environment + versions/
tests/              pytest suite (parser, normalize, ingest DB flow)
render.yaml         Render Blueprint (web service + Postgres)
```

## Conventions

- **Decimal end to end.** NUMERIC in the DB, Decimal in Python; float never
  touches a monetary value. Round at display only.
- **`ticker` is the join key, not ISIN.** There is a known ISIN collision
  (MongoDB and Toast both report `US8887871080`). ISIN is a validated attribute.
- **Fail visibly.** A stale price cache or an old snapshot is surfaced in the UI,
  never silently rendered as current.
- **Secrets in the environment**, never in the repo. `.env` is git-ignored.

## Data model

| Table | Purpose |
|---|---|
| `snapshots` | One custodian holdings file, as of a user-supplied date |
| `holdings_snapshot` | One position within one snapshot (units, avg_cost) |
| `securities` | Persistent per-ticker metadata: pillar, thesis note, stop |
| `changes` | Position deltas between consecutive snapshots (diff engine) |
| `nav_points` | Per-share NAV series for honest fund-level return |
| `fundamentals_snapshot` | Cached valuation/quality/analyst data per ticker |
| `estimates_snapshot` | Forward revenue/EPS estimates |
| `earnings_history` | Actual vs estimate surprises |
| `news_item` | Relevance-filtered news (awareness only) |
```
