# NextGen Dashboard — Position Detail Data Layer

Brief for Claude Code. Covers the cached-fundamentals schema and the per-ticker
fetch/refresh function that backs the position detail page.

## Context

- FastAPI app on Render, Postgres (Render Postgres — **not** SQLite, the filesystem
  does not survive deploys).
- Transaction ledger is the source of truth for positions; this module is separate
  and read-only with respect to the ledger.
- Data sources verified working as of 2026-07-18:
  - **Alpha Vantage `COMPANY_OVERVIEW`** — valuation, margins, growth, ownership,
    float, beta, 52w range, 50/200 DMA. One call covers most of the fundamentals block.
  - **FMP `analyst/price-target-consensus`**, **`analyst/grades-summary`**,
    **`analyst/financial-estimates`** — all confirmed working on the current plan.
  - **Alpha Vantage `NEWS_SENTIMENT`** — works, but must be relevance-filtered.
- Do **not** use Alpha Vantage `EARNINGS_ESTIMATES` — returns an empty array for every
  symbol tested (ANET, IBM). Use FMP `financial-estimates` instead.
- Do **not** use FMP `tipranks` (separate paid add-on) or `form13F` (Ultimate plan).

## Design principles

1. **Cache everything except price and news.** Fundamentals change quarterly. One
   refresh per ticker per day, written by the same cron that writes the NAV snapshot.
2. **Never let the page trigger a fetch synchronously.** The page reads from Postgres.
   If the cache is stale, show the stale data with an "as of" timestamp rather than
   blocking on an API call.
3. **Store raw JSON alongside parsed columns.** Providers change field names; keeping
   the raw payload means a parser bug is recoverable without re-fetching history.
4. **Partial failure is normal.** If FMP is down but Alpha Vantage is up, write what
   you got. Track per-source fetch status.
5. **Nothing numeric ever comes from news text.** News summaries from aggregators were
   observed quoting prices off by ~80%. News is for awareness only.

## Schema

```sql
-- One row per ticker per refresh date. Keep history; it is cheap and makes
-- "what did the market think 3 months ago" answerable later.
CREATE TABLE fundamentals_snapshot (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT        NOT NULL,
    as_of           DATE        NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- identity
    company_name    TEXT,
    sector          TEXT,
    industry        TEXT,
    exchange        TEXT,

    -- valuation
    market_cap          NUMERIC,
    pe_trailing         NUMERIC,
    pe_forward          NUMERIC,
    peg                 NUMERIC,
    price_to_sales      NUMERIC,
    price_to_book       NUMERIC,
    ev_to_revenue       NUMERIC,
    ev_to_ebitda        NUMERIC,

    -- quality / growth
    profit_margin           NUMERIC,
    operating_margin        NUMERIC,
    roa                     NUMERIC,
    roe                     NUMERIC,
    revenue_ttm             NUMERIC,
    gross_profit_ttm        NUMERIC,
    eps_ttm                 NUMERIC,
    rev_growth_yoy          NUMERIC,
    earnings_growth_yoy     NUMERIC,

    -- market structure
    beta                NUMERIC,
    week52_high         NUMERIC,
    week52_low          NUMERIC,
    dma_50              NUMERIC,
    dma_200             NUMERIC,
    shares_outstanding  NUMERIC,
    shares_float        NUMERIC,
    pct_insiders        NUMERIC,
    pct_institutions    NUMERIC,

    -- analyst view (FMP primary, AV as cross-check)
    target_high         NUMERIC,
    target_low          NUMERIC,
    target_consensus    NUMERIC,
    target_median       NUMERIC,
    target_av           NUMERIC,      -- Alpha Vantage's own number, for divergence checks
    grades_strong_buy   INTEGER,
    grades_buy          INTEGER,
    grades_hold         INTEGER,
    grades_sell         INTEGER,
    grades_strong_sell  INTEGER,
    grades_consensus    TEXT,

    latest_quarter      DATE,

    -- provenance
    raw_av_overview     JSONB,
    raw_fmp_targets     JSONB,
    raw_fmp_grades      JSONB,
    source_status       JSONB,        -- {"av_overview":"ok","fmp_targets":"error: 402", ...}

    UNIQUE (ticker, as_of)
);

CREATE INDEX idx_fund_ticker_asof ON fundamentals_snapshot (ticker, as_of DESC);


-- Forward estimates. One row per ticker per fiscal period per refresh.
CREATE TABLE estimates_snapshot (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL,
    as_of               DATE NOT NULL,
    period_end          DATE NOT NULL,
    period_type         TEXT NOT NULL,          -- 'annual' | 'quarter'
    revenue_avg         NUMERIC,
    revenue_low         NUMERIC,
    revenue_high        NUMERIC,
    ebitda_avg          NUMERIC,
    net_income_avg      NUMERIC,
    eps_avg             NUMERIC,
    eps_low             NUMERIC,
    eps_high            NUMERIC,
    num_analysts_rev    INTEGER,
    num_analysts_eps    INTEGER,
    UNIQUE (ticker, as_of, period_end, period_type)
);

CREATE INDEX idx_est_ticker ON estimates_snapshot (ticker, period_end);


-- Earnings history: actual vs estimate, for the surprise table.
CREATE TABLE earnings_history (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL,
    fiscal_ending   DATE NOT NULL,
    reported_date   DATE,
    eps_actual      NUMERIC,
    eps_estimate    NUMERIC,
    surprise_pct    NUMERIC,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (ticker, fiscal_ending)
);


-- News. Short retention; prune anything older than ~90 days.
CREATE TABLE news_item (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL,
    url                 TEXT NOT NULL,
    title               TEXT,
    source              TEXT,
    published_at        TIMESTAMPTZ,
    summary             TEXT,
    relevance_score     NUMERIC,
    sentiment_score     NUMERIC,
    sentiment_label     TEXT,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (ticker, url)
);

CREATE INDEX idx_news_ticker_pub ON news_item (ticker, published_at DESC);
```

## Fetch module

Create `app/data/fundamentals.py`.

### Requirements

- `httpx` with an explicit timeout (10s) and 2 retries with exponential backoff on
  5xx and timeouts only — never retry a 402/403, that is a plan limit and will not
  resolve.
- API keys from environment: `ALPHAVANTAGE_API_KEY`, `FMP_API_KEY`. Never hardcoded.
- Rate limiting: Alpha Vantage free tier is ~5 req/min, ~25/day — verify the actual
  tier before sizing. Space calls and make the batch job tolerant of a hard stop
  partway through (resume from where it left off next run).
- Every network call wrapped so that one failing source never aborts the whole refresh.

### Functions to implement

```python
def fetch_av_overview(ticker: str) -> dict | None:
    """GET https://www.alphavantage.co/query?function=OVERVIEW&symbol=...

    Alpha Vantage returns HTTP 200 with an empty dict or a {"Note": ...} /
    {"Information": ...} body when rate limited. Treat any response lacking a
    "Symbol" key as a failure, not as empty data — this is the single most common
    silent-corruption bug with this provider.

    All numeric fields arrive as STRINGS, and missing values arrive as the literal
    string "None". Coerce through a helper that maps "None"/""/"-" -> None.
    """


def fetch_fmp_targets(ticker: str) -> dict | None:
    """GET /stable/price-target-consensus?symbol=...
    Returns a list; take element 0. Fields: targetHigh, targetLow,
    targetConsensus, targetMedian.
    """


def fetch_fmp_grades(ticker: str) -> dict | None:
    """GET /stable/grades-summary?symbol=...
    Returns a list; take element 0. Fields: strongBuy, buy, hold, sell,
    strongSell, consensus.
    """


def fetch_fmp_estimates(ticker: str, period: str = "annual", limit: int = 5) -> list[dict]:
    """GET /stable/analyst-estimates?symbol=...&period=...&limit=...
    Returns forward periods with revenue/ebitda/netIncome/eps low-avg-high plus
    numAnalystsRevenue and numAnalystsEps. Call twice per ticker: annual and quarter.
    """


def fetch_news(ticker: str, min_relevance: float = 0.9, limit: int = 40) -> list[dict]:
    """GET https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers=...

    Response shape: {"feed": [{... "ticker_sentiment": [{"ticker","relevance_score",
    "ticker_sentiment_score","ticker_sentiment_label"}]}]}

    For each article, find the entry in ticker_sentiment matching OUR ticker and use
    ITS relevance and sentiment — not the article-level overall_sentiment_score, which
    describes the whole article and is frequently about a different company.

    Drop anything below min_relevance. This matters: an ANET query returned articles
    primarily about Motorola, Lumentum, F5 and an unrelated SD-WAN vendor, all tagged
    at ~0.6 relevance. Without the filter the news panel is mostly noise.

    Also apply SOURCE_BLOCKLIST for 13F-filing spam — MarketBeat "instant-alerts"
    URLs are high volume and near-zero information.
    """


def refresh_ticker(ticker: str, conn) -> dict:
    """Fetch all sources for one ticker, upsert into all four tables, return a
    per-source status dict.

    - Compute as_of as the current US Eastern trading date.
    - Upsert on the UNIQUE constraints so a same-day re-run is idempotent.
    - Write source_status even for the sources that failed, so the UI can show
      "analyst data unavailable" rather than silently rendering zeros.
    - Never raise on a single source failure; only raise if ALL sources failed.
    """


def refresh_all(conn, tickers: list[str] | None = None) -> dict:
    """Batch refresh. Defaults to the distinct tickers currently held per the
    position view, plus any ticker held at any point in the last 90 days (so recently
    closed positions still render).

    Respect rate limits between calls. Log a summary: n succeeded, n partial,
    n failed, with tickers named.
    """
```

### Read path

```python
def get_position_detail(ticker: str, conn) -> dict:
    """Assemble the page payload from cache only — no network calls.

    Returns:
      - latest fundamentals_snapshot row + its as_of, so the UI can badge staleness
      - estimates for the next 3 annual and next 4 quarterly periods
      - last 8 rows of earnings_history
      - top 15 news_item rows by published_at
      - a `data_age_days` field; the UI should visibly warn above 3
    """
```

## Derived fields for the UI

Compute at render time from the cached row plus the live price — do not store:

- Implied upside: `(target_consensus / last_price) - 1`
- 52-week range position: `(last_price - week52_low) / (week52_high - week52_low)`
- Distance from 50 DMA and 200 DMA, as percentages
- Divergence flag when FMP and Alpha Vantage consensus targets differ by more than
  15% — usually means one source is stale, and it is worth surfacing rather than
  silently picking one

## Cron

Add to the existing daily job, after the NAV snapshot:

1. NAV snapshot (must not be blocked by fundamentals failures — run it first)
2. `refresh_all()`
3. Prune `news_item` older than 90 days

Schedule ~16:15 ET / 23:15 IST.

## Testing

- Unit test the Alpha Vantage numeric coercion against a fixture containing
  `"None"`, `""`, and a valid number.
- Unit test the news relevance filter against a fixture with a mixed-ticker article.
- Integration test `refresh_ticker` against one live ticker, asserting that a
  simulated FMP 402 still results in a written row with AV data present and
  `source_status` recording the failure.
