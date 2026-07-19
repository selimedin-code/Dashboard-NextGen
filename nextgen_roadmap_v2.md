# NextGen Fund Dashboard — Roadmap (Snapshot Architecture)

Supersedes the earlier ledger-based roadmap. Read this first; each phase gets its own
detailed brief.

## What this is

Internal monitoring tool for a ~55-position US equity fund concentrated in AI and
compute infrastructure, formed 2023, organized across an 11-pillar thematic framework.
Two users: the fund manager and one other person reviewing alongside him.

A secondary but real purpose is coaching — the tool should make it easy to see where
returns actually came from and whether reasoning at entry held up, not just what the
portfolio is worth today.

**Stack:** FastAPI + Postgres + Jinja2, GitHub → Render. Server-rendered; no SPA
needed for two users.

## Architecture

The fund's custodian produces an authoritative holdings file. The dashboard ingests
it rather than maintaining its own transaction ledger — re-keying custodian data can
only introduce errors.

**Uploaded per snapshot (the only inputs that matter):**

| Field | Notes |
|---|---|
| `ticker` | Bloomberg-style, e.g. `NVDA US`, `IFX GR` |
| `units` | Fractional; use Decimal |
| `avg_cost` | Blended, includes pro-rata top-ups |
| `as_of` | **Required at upload time.** Not in the file; capture explicitly. |

Cash appears as a row with ticker `USD Cash`. Handle as a position with
`avg_cost = 1.0`, or as a dedicated field — either is fine, but be consistent.

Everything else — current price, market value, weight, sector, P&L, performance,
sector allocation — is **derived by the dashboard**. Ignore those columns if present
in the file; recomputing them is both simpler and more trustworthy than reconciling
someone else's arithmetic.

**Transaction history is inferred by diffing consecutive snapshots**, not entered.

## Sequencing principle

Snapshots are the source of truth. The diff engine turns them into change history.
Everything else reads from those two.

Analysis that needs only the current snapshot comes early. Analysis that needs
accumulated snapshot history comes later — but unlike the ledger design, nothing is
permanently lost by starting late, because each upload is a complete point-in-time
picture.

---

## Phase 1 — Upload, parse, validate

Excel upload endpoint. Parse to `holdings_snapshot` rows. This phase is small but
everything depends on it being strict.

**Schema:**

```sql
CREATE TABLE snapshots (
    id          BIGSERIAL PRIMARY KEY,
    as_of       DATE NOT NULL UNIQUE,   -- user-supplied at upload
    filename    TEXT,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes       TEXT
);

CREATE TABLE holdings_snapshot (
    id          BIGSERIAL PRIMARY KEY,
    snapshot_id BIGINT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    ticker      TEXT NOT NULL,          -- normalized, no country suffix
    raw_ticker  TEXT NOT NULL,          -- as it appeared in the file
    units       NUMERIC(20,6) NOT NULL,
    avg_cost    NUMERIC(20,6) NOT NULL,
    UNIQUE (snapshot_id, ticker)
);

-- Persistent per-ticker metadata. Survives across snapshots.
CREATE TABLE securities (
    ticker       TEXT PRIMARY KEY,
    name         TEXT,
    exchange     TEXT,                  -- from the suffix: US, GR, ...
    pillar       TEXT,                  -- the 11-pillar framework
    isin         TEXT,
    thesis_note  TEXT,                  -- position-level, editable
    stop_price   NUMERIC(20,6),
    first_seen   DATE,
    active       BOOLEAN NOT NULL DEFAULT TRUE
);
```

**Ticker normalization:** strip the country suffix for API calls but store it.
`IFX GR` is the Frankfurt listing — quoting it against a US endpoint returns the wrong
price or nothing.

**Validation, blocking:**
- Duplicate `as_of` — require explicit overwrite confirmation
- Duplicate ticker within one file
- Non-positive units or cost
- Any ticker that fails a live quote lookup

**Validation, warn only:**
- New tickers not in `securities` — prompt for pillar assignment inline
- Ticker count changing by more than ~20% from the prior snapshot
- Units changing by more than 5x for any single position

**Preview before commit.** Show what will change versus the last snapshot — opens,
closes, adds, trims — and let the user confirm. This is the main defense against
uploading the wrong file or the wrong date.

**Done when:** the real file uploads cleanly and the parsed rows match it exactly.

---

## Phase 2 — Diff engine

Given two consecutive snapshots, produce a `changes` table:

```sql
CREATE TABLE changes (
    id              BIGSERIAL PRIMARY KEY,
    from_snapshot   BIGINT REFERENCES snapshots(id),
    to_snapshot     BIGINT REFERENCES snapshots(id),
    ticker          TEXT NOT NULL,
    change_type     TEXT NOT NULL,   -- OPEN | CLOSE | ADD | TRIM | HOLD
    units_before    NUMERIC(20,6),
    units_after     NUMERIC(20,6),
    units_delta     NUMERIC(20,6),
    pct_delta       NUMERIC(10,4),
    cost_before     NUMERIC(20,6),
    cost_after      NUMERIC(20,6),
    implied_price   NUMERIC(20,6),   -- derived; see below
    classification  TEXT,            -- FLOW_DRIVEN | DISCRETIONARY | AMBIGUOUS
    UNIQUE (from_snapshot, to_snapshot, ticker)
);
```

**Implied trade price** on an add, from the blended-cost identity:
`price = (units_after * cost_after - units_before * cost_before) / units_delta`
Sanity-check it against that period's actual price range and mark it `AMBIGUOUS` if
it falls outside — that means more than one trade happened in the interval.

**Flow classification.** When new capital is distributed pro-rata, every position's
units rise by roughly the same percentage. Compute the median `pct_delta` across all
positions; anything close to the median is `FLOW_DRIVEN`, anything materially
different is `DISCRETIONARY`. This is what lets the tool separate decisions from
mechanics without any manual flagging.

**Known limitation:** round trips inside one interval are invisible. Accept it. More
frequent uploads narrow the window.

**Done when:** two real consecutive files produce a change list the manager agrees
matches what actually happened.

---

## Phase 3 — Position table

The main screen. Per holding: ticker, pillar, units, average cost, live price, market
value, weight %, unrealized P&L in dollars and percent, day change, contribution to
the fund today. Sortable, filterable by pillar.

Header strip: total value, cash, day change, position count, snapshot date with a
staleness badge.

**Done when:** it replaces whatever spreadsheet is in use now.

---

## Phase 4 — Ticker detail page

**Detailed brief:** `nextgen_fundamentals_brief.md` (data layer — still valid as
written)

Click a row, get the whole story. Position context: units, blended cost, weight,
first seen, the editable thesis note, and this position's change history from the
`changes` table plotted as markers on the price chart.

Then company data: valuation and quality metrics, forward estimates, analyst targets
and rating distribution, earnings history with surprises, filtered news. Cached daily
in Postgres from Alpha Vantage `COMPANY_OVERVIEW` plus FMP `analyst/*`.

**Label blended cost as blended.** It includes pro-rata top-ups, so "performance vs
cost" here is economic return, not a verdict on the entry decision. The distinction
matters for the coaching purpose.

**Verify first:** the Alpha Vantage plan tier. Free tier is ~25 calls/day, which does
not cover 55 positions.

---

## Phase 5 — Exposure and concentration

Weight by pillar, top 10 holdings and combined weight, top-5 and top-10 percentages,
Herfindahl index, effective number of positions, cash percentage, single-name flags.

For a book this concentrated this is where the real risk sits, and it needs only the
current snapshot. Once several snapshots exist, plot the drift — a rising top-10
weight is the signal, not the level.

---

## Phase 6 — Performance

Needs snapshot history. Two distinct measures, kept separate:

**Position level.** Return versus blended cost per holding; contribution to fund
return by position and by pillar. Filter attribution to `DISCRETIONARY` changes so
sell discipline is measured on actual decisions.

**Fund level.** This is the one place the minimal upload is not sufficient. Total
value moves with subscriptions and redemptions, so it is not a performance measure.

The clean fix is the per-share NAV series the custodian already produces. Add a
separate, one-time upload for it — date and NAV per share — and compute all
fund-level returns from that. Without it, fund-level performance can only be
approximated by chaining snapshot-to-snapshot returns with flows backed out from the
diff classification, which is workable but lossy.

Benchmark: use QQQ or a QQQ/SMH blend as the honest beta comparison and SPY as the
generalist-allocator comparison. Against SPY alone a heavily-AI book looks heroic in
an up-tape and disastrous in a down-tape, and neither says whether the manager added
value.

---

## Phase 7 — Signals and flags

Earnings this week among holdings, positions breaching a stop or key moving average,
outsized single-day moves, concentration breaches, notable news.

Last because it is the most tempting and least load-bearing. A dashboard full of
alerts nobody trusts is worse than no alerts.

---

## Cross-cutting

- **Decimal end to end.** No float touches a monetary value. Round at display only.
- **Ticker is the primary key, not ISIN.** The source file has at least one ISIN
  collision (MongoDB and Toast share `US8887871080`). Store ISIN as a validated
  attribute and flag changes or duplicates.
- **Daily price cache.** One EOD fetch per held ticker, cached in Postgres. Only live
  price and news hit the API on page load.
- **HTTP basic auth** in front of everything.
- **Alembic migrations.**
- **Secrets in Render environment variables**, never in the repo.
- **Fail visibly.** A stale price cache or a snapshot that is weeks old must be
  obvious in the UI, not silently rendered as current.
- **Paid Render starter tier.** Free spins down on inactivity.

## Out of scope

Manual transaction entry. Per-trade thesis notes (position-level instead). Lot-level
or FIFO cost basis. Multi-user roles. Tax reporting. Anything LP-facing.
