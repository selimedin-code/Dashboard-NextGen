"""ORM models for the NextGen dashboard.

Mirrors the schema in nextgen_roadmap_v2.md (snapshot + diff core) and
nextgen_fundamentals_brief.md (cached fundamentals for the detail page).

Conventions:
  - Monetary / quantity columns are NUMERIC (never float).
  - `ticker` is the normalized symbol (no country suffix) and is the join key
    everywhere. ISIN is a validated attribute, NOT a key (there is a known ISIN
    collision: MongoDB and Toast both report US8887871080).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# A generous precision for money and share quantities. Fractional units are real
# (see the portfolio file), so keep 6 decimal places.
Money = Numeric(20, 6)


# ---------------------------------------------------------------------------
# Core: snapshots, holdings, securities  (Roadmap Phase 1)
# ---------------------------------------------------------------------------


class Snapshot(Base):
    """One custodian holdings file, as of a user-supplied date.

    `as_of` is captured explicitly at upload time — it is NOT reliably present
    in the file, and it is the axis the diff engine walks.
    """

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    filename: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    notes: Mapped[str | None] = mapped_column(Text)

    holdings: Mapped[list[HoldingSnapshot]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class HoldingSnapshot(Base):
    """One position within one snapshot. The atomic unit of truth.

    Everything derived (price, market value, weight, P&L) is computed at read
    time, never stored here.
    """

    __tablename__ = "holdings_snapshot"
    __table_args__ = (UniqueConstraint("snapshot_id", "ticker", name="uq_holding_snapshot_ticker"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=False
    )
    ticker: Mapped[str] = mapped_column(Text, nullable=False)  # normalized, no suffix
    raw_ticker: Mapped[str] = mapped_column(Text, nullable=False)  # as it appeared
    units: Mapped[float] = mapped_column(Money, nullable=False)
    avg_cost: Mapped[float] = mapped_column(Money, nullable=False)

    snapshot: Mapped[Snapshot] = relationship(back_populates="holdings")


class Pillar(Base):
    """The thematic framework: one row per pillar (P01..P13), with the benchmark
    ETF(s) and any honest caveat about ETF fit. Reference data, seeded from file."""

    __tablename__ = "pillars"

    id: Mapped[str] = mapped_column(Text, primary_key=True)      # e.g. "P01"
    name: Mapped[str] = mapped_column(Text, nullable=False)
    primary_etf: Mapped[str | None] = mapped_column(Text)
    alt_etf: Mapped[str | None] = mapped_column(Text)
    caveat: Mapped[str | None] = mapped_column(Text)
    # Effective-bet key for the Risk page (see app/risk_config.py). Groups the
    # 13 pillars into ~9 macro bets; backfilled by migration a7c31e90d4f2.
    macro_bet: Mapped[str | None] = mapped_column(Text)


class Security(Base):
    """Persistent per-ticker metadata that survives across snapshots.

    This is where the pillar framework assignment and editable thesis notes live.
    `ticker` is the primary key by design. `pillar` holds the denormalized pillar
    NAME (what the UI shows); `pillar_id`/`crossref_pillar_id` link to `pillars`.
    """

    __tablename__ = "securities"

    ticker: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    exchange: Mapped[str | None] = mapped_column(Text)  # from the suffix: US, GR, ...
    pillar: Mapped[str | None] = mapped_column(Text)  # denormalized pillar name
    pillar_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("pillars.id", ondelete="SET NULL")
    )
    crossref_pillar_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("pillars.id", ondelete="SET NULL")
    )
    isin: Mapped[str | None] = mapped_column(Text)  # validated attribute, not a key
    thesis_note: Mapped[str | None] = mapped_column(Text)
    stop_price: Mapped[float | None] = mapped_column(Money)
    first_seen: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


# ---------------------------------------------------------------------------
# Diff engine  (Roadmap Phase 2)
# ---------------------------------------------------------------------------


class Change(Base):
    """A single position's delta between two consecutive snapshots."""

    __tablename__ = "changes"
    __table_args__ = (
        UniqueConstraint("from_snapshot", "to_snapshot", "ticker", name="uq_change_pair_ticker"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    from_snapshot: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("snapshots.id", ondelete="CASCADE")
    )
    to_snapshot: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("snapshots.id", ondelete="CASCADE")
    )
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    change_type: Mapped[str] = mapped_column(Text, nullable=False)  # OPEN|CLOSE|ADD|TRIM|HOLD
    units_before: Mapped[float | None] = mapped_column(Money)
    units_after: Mapped[float | None] = mapped_column(Money)
    units_delta: Mapped[float | None] = mapped_column(Money)
    pct_delta: Mapped[float | None] = mapped_column(Numeric(10, 4))
    cost_before: Mapped[float | None] = mapped_column(Money)
    cost_after: Mapped[float | None] = mapped_column(Money)
    implied_price: Mapped[float | None] = mapped_column(Money)
    classification: Mapped[str | None] = mapped_column(Text)  # FLOW_DRIVEN|DISCRETIONARY|AMBIGUOUS


# ---------------------------------------------------------------------------
# Fund-level NAV series  (Roadmap Phase 6)
# ---------------------------------------------------------------------------


class NavPoint(Base):
    """Per-share NAV from the custodian — the honest basis for fund-level return.

    Uploaded separately from holdings because total market value moves with
    subscriptions and redemptions and is therefore not a performance measure.
    """

    __tablename__ = "nav_points"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    nav_per_share: Mapped[float] = mapped_column(Money, nullable=False)
    total_nav: Mapped[float | None] = mapped_column(Money)
    shares_outstanding: Mapped[float | None] = mapped_column(Money)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Cached fundamentals  (Fundamentals brief — Roadmap Phase 4)
# ---------------------------------------------------------------------------


class FundamentalsSnapshot(Base):
    """One row per ticker per refresh date. History is kept; it is cheap and lets
    'what did the market think 3 months ago' be answered later."""

    __tablename__ = "fundamentals_snapshot"
    __table_args__ = (UniqueConstraint("ticker", "as_of", name="uq_fundamentals_ticker_asof"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # identity
    company_name: Mapped[str | None] = mapped_column(Text)
    sector: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(Text)
    exchange: Mapped[str | None] = mapped_column(Text)

    # valuation
    market_cap: Mapped[float | None] = mapped_column(Numeric)
    pe_trailing: Mapped[float | None] = mapped_column(Numeric)
    pe_forward: Mapped[float | None] = mapped_column(Numeric)
    peg: Mapped[float | None] = mapped_column(Numeric)
    price_to_sales: Mapped[float | None] = mapped_column(Numeric)
    price_to_book: Mapped[float | None] = mapped_column(Numeric)
    ev_to_revenue: Mapped[float | None] = mapped_column(Numeric)
    ev_to_ebitda: Mapped[float | None] = mapped_column(Numeric)

    # quality / growth
    profit_margin: Mapped[float | None] = mapped_column(Numeric)
    operating_margin: Mapped[float | None] = mapped_column(Numeric)
    roa: Mapped[float | None] = mapped_column(Numeric)
    roe: Mapped[float | None] = mapped_column(Numeric)
    revenue_ttm: Mapped[float | None] = mapped_column(Numeric)
    gross_profit_ttm: Mapped[float | None] = mapped_column(Numeric)
    eps_ttm: Mapped[float | None] = mapped_column(Numeric)
    rev_growth_yoy: Mapped[float | None] = mapped_column(Numeric)
    earnings_growth_yoy: Mapped[float | None] = mapped_column(Numeric)

    # market structure
    beta: Mapped[float | None] = mapped_column(Numeric)
    week52_high: Mapped[float | None] = mapped_column(Numeric)
    week52_low: Mapped[float | None] = mapped_column(Numeric)
    dma_50: Mapped[float | None] = mapped_column(Numeric)
    dma_200: Mapped[float | None] = mapped_column(Numeric)
    shares_outstanding: Mapped[float | None] = mapped_column(Numeric)
    shares_float: Mapped[float | None] = mapped_column(Numeric)
    pct_insiders: Mapped[float | None] = mapped_column(Numeric)
    pct_institutions: Mapped[float | None] = mapped_column(Numeric)

    # analyst view (FMP primary, AV as cross-check)
    target_high: Mapped[float | None] = mapped_column(Numeric)
    target_low: Mapped[float | None] = mapped_column(Numeric)
    target_consensus: Mapped[float | None] = mapped_column(Numeric)
    target_median: Mapped[float | None] = mapped_column(Numeric)
    target_av: Mapped[float | None] = mapped_column(Numeric)
    grades_strong_buy: Mapped[int | None] = mapped_column(Integer)
    grades_buy: Mapped[int | None] = mapped_column(Integer)
    grades_hold: Mapped[int | None] = mapped_column(Integer)
    grades_sell: Mapped[int | None] = mapped_column(Integer)
    grades_strong_sell: Mapped[int | None] = mapped_column(Integer)
    grades_consensus: Mapped[str | None] = mapped_column(Text)

    latest_quarter: Mapped[date | None] = mapped_column(Date)

    # provenance — raw payloads kept so a parser bug is recoverable without re-fetch
    raw_av_overview: Mapped[dict | None] = mapped_column(JSONB)
    raw_fmp_targets: Mapped[dict | None] = mapped_column(JSONB)
    raw_fmp_grades: Mapped[dict | None] = mapped_column(JSONB)
    source_status: Mapped[dict | None] = mapped_column(JSONB)


class EstimatesSnapshot(Base):
    """Forward estimates. One row per ticker per fiscal period per refresh."""

    __tablename__ = "estimates_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "as_of", "period_end", "period_type", name="uq_estimates_period"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    period_type: Mapped[str] = mapped_column(Text, nullable=False)  # 'annual' | 'quarter'
    revenue_avg: Mapped[float | None] = mapped_column(Numeric)
    revenue_low: Mapped[float | None] = mapped_column(Numeric)
    revenue_high: Mapped[float | None] = mapped_column(Numeric)
    ebitda_avg: Mapped[float | None] = mapped_column(Numeric)
    net_income_avg: Mapped[float | None] = mapped_column(Numeric)
    eps_avg: Mapped[float | None] = mapped_column(Numeric)
    eps_low: Mapped[float | None] = mapped_column(Numeric)
    eps_high: Mapped[float | None] = mapped_column(Numeric)
    num_analysts_rev: Mapped[int | None] = mapped_column(Integer)
    num_analysts_eps: Mapped[int | None] = mapped_column(Integer)


class EarningsHistory(Base):
    """Actual vs estimate, for the surprise table."""

    __tablename__ = "earnings_history"
    __table_args__ = (UniqueConstraint("ticker", "fiscal_ending", name="uq_earnings_ticker_fiscal"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    fiscal_ending: Mapped[date] = mapped_column(Date, nullable=False)
    reported_date: Mapped[date | None] = mapped_column(Date)
    eps_actual: Mapped[float | None] = mapped_column(Numeric)
    eps_estimate: Mapped[float | None] = mapped_column(Numeric)
    surprise_pct: Mapped[float | None] = mapped_column(Numeric)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NewsItem(Base):
    """Awareness only — nothing numeric is ever trusted from news text.
    Short retention; prune anything older than ~90 days."""

    __tablename__ = "news_item"
    __table_args__ = (UniqueConstraint("ticker", "url", name="uq_news_ticker_url"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str | None] = mapped_column(Text)
    relevance_score: Mapped[float | None] = mapped_column(Numeric)
    sentiment_score: Mapped[float | None] = mapped_column(Numeric)
    sentiment_label: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Upload staging  (Phase 1 — carries a parsed file between preview and commit)
# ---------------------------------------------------------------------------


class UploadStaging(Base):
    """A parsed-but-not-committed upload, held between the preview and commit steps.

    Stored in the DB (not in-process memory or on disk) so the two-step flow
    survives a worker restart and works regardless of which worker handles each
    request. Parsed holdings live in `parsed` as JSON with numbers as strings, to
    preserve Decimal exactly. Rows are short-lived; prune on commit or by age.
    """

    __tablename__ = "upload_staging"

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    filename: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    source_format: Mapped[str | None] = mapped_column(Text)
    parsed: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Price cache  (Phase 3 — the position table reads this, never the live API)
# ---------------------------------------------------------------------------


class QuoteCache(Base):
    """Latest live quote per ticker. Written by the refresh action / daily cron;
    the position page reads only from here so a slow API never blocks a render.

    `ok=False` with an `error` is a first-class state — a ticker we could not
    price (e.g. a non-US listing not on the plan) is shown badged, never as a
    silent zero."""

    __tablename__ = "quote_cache"

    ticker: Mapped[str] = mapped_column(Text, primary_key=True)
    fmp_symbol: Mapped[str | None] = mapped_column(Text)
    price: Mapped[float | None] = mapped_column(Numeric(20, 6))
    prev_close: Mapped[float | None] = mapped_column(Numeric(20, 6))
    day_change_pct: Mapped[float | None] = mapped_column(Numeric(12, 6))
    currency: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PriceHistoryCache(Base):
    """Daily close series per ticker, held as one JSON blob (list of [date, close]).
    Backs the detail-page price chart and the 50/200-day moving averages. One row
    per ticker; refreshed with the fundamentals fetch."""

    __tablename__ = "price_history_cache"

    ticker: Mapped[str] = mapped_column(Text, primary_key=True)
    series: Mapped[list] = mapped_column(JSONB, nullable=False)   # [["2026-07-18", 202.81], ...]
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
