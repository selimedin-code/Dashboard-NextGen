"""Assemble the ticker detail page from cache only (no network).

Position context + change history come from the snapshot tables; company data from
the fundamentals cache; the price chart from the price-history cache with this
position's OPEN/ADD/TRIM/CLOSE moves overlaid as markers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Change,
    EarningsHistory,
    EstimatesSnapshot,
    FundamentalsSnapshot,
    NewsItem,
    Pillar,
    PriceHistoryCache,
    QuoteCache,
    Security,
    Snapshot,
)
from app.positions import build_positions
from app.reviews import STANCES, ReviewRow, list_reviews
from app.trades import TradeRow, list_trades

CHART_W = 720
CHART_H = 200
CHART_PAD = 6


@dataclass
class Marker:
    x: float
    y: float
    kind: str          # OPEN | ADD | TRIM | CLOSE | REVIEW
    label: str
    price: Decimal | None = None


@dataclass
class Gridline:
    """One horizontal price level. `frac` is y/CHART_H so the template can
    position the HTML label overlay (SVG text would distort — the chart is
    stretched with preserveAspectRatio="none")."""
    y: float
    frac: float
    level: Decimal


@dataclass
class Chart:
    points: str = ""
    markers: list[Marker] = field(default_factory=list)
    gridlines: list[Gridline] = field(default_factory=list)
    lo: Decimal | None = None
    hi: Decimal | None = None
    first_date: str | None = None
    last_date: str | None = None
    last_x: float | None = None
    last_y: float | None = None
    last_close: Decimal | None = None
    last_frac: float | None = None
    w: int = CHART_W
    h: int = CHART_H


def _grid_levels(lo: Decimal, hi: Decimal, target: int = 4) -> list[Decimal]:
    """Round price levels inside [lo, hi]: a 1/2/2.5/5 step sized for ~`target`
    lines, so labels read as 30, 35, 40 rather than 31.07, 36.44."""
    span = hi - lo
    if span <= 0:
        return []
    raw = span / target
    mag = Decimal(10) ** raw.adjusted()
    step = next(m * mag for m in (Decimal(1), Decimal(2), Decimal("2.5"),
                                  Decimal(5), Decimal(10)) if m * mag >= raw)
    level = (lo / step).to_integral_value(rounding=ROUND_CEILING) * step
    levels = []
    while level <= hi:
        levels.append(level)
        level += step
    return levels


@dataclass
class TickerDetail:
    ticker: str
    security: Security | None
    pillar_name: str | None
    crossref_name: str | None
    held: bool
    position: object | None            # PositionRow for this ticker, if held
    changes: list[dict] = field(default_factory=list)
    fundamentals: FundamentalsSnapshot | None = None
    fundamentals_age_days: int | None = None
    estimates: list[EstimatesSnapshot] = field(default_factory=list)
    earnings: list[EarningsHistory] = field(default_factory=list)
    news: list[NewsItem] = field(default_factory=list)
    chart: Chart | None = None
    derived: dict = field(default_factory=dict)
    reviews: list[ReviewRow] = field(default_factory=list)
    current_price: Decimal | None = None
    stances: tuple[str, ...] = STANCES
    trades: list[TradeRow] = field(default_factory=list)
    has_snapshot: bool = False


def get_ticker_detail(session: Session, ticker: str) -> TickerDetail:
    sec = session.get(Security, ticker)
    pillars = {p.id: p.name for p in session.execute(select(Pillar)).scalars().all()}

    # Position context (reuse the position engine for weight/P&L consistency).
    pv = build_positions(session)
    position = None
    if pv is not None:
        position = next((r for r in pv.rows if r.ticker == ticker), None)

    detail = TickerDetail(
        ticker=ticker,
        security=sec,
        pillar_name=(pillars.get(sec.pillar_id) if sec and sec.pillar_id else (sec.pillar if sec else None)),
        crossref_name=(pillars.get(sec.crossref_pillar_id) if sec and sec.crossref_pillar_id else None),
        held=position is not None,
        position=position,
    )

    detail.changes = _change_history(session, ticker)
    detail.fundamentals = session.execute(
        select(FundamentalsSnapshot).where(FundamentalsSnapshot.ticker == ticker)
        .order_by(FundamentalsSnapshot.as_of.desc()).limit(1)
    ).scalar_one_or_none()
    if detail.fundamentals is not None:
        age = datetime.now(timezone.utc) - detail.fundamentals.fetched_at
        detail.fundamentals_age_days = age.days

    if detail.fundamentals is not None:
        f_as_of = detail.fundamentals.as_of
        detail.estimates = list(session.execute(
            select(EstimatesSnapshot).where(
                EstimatesSnapshot.ticker == ticker, EstimatesSnapshot.as_of == f_as_of,
                EstimatesSnapshot.period_type == "annual",
            ).order_by(EstimatesSnapshot.period_end)
        ).scalars().all())

    detail.earnings = list(session.execute(
        select(EarningsHistory).where(EarningsHistory.ticker == ticker)
        .order_by(EarningsHistory.fiscal_ending.desc()).limit(8)
    ).scalars().all())
    detail.news = list(session.execute(
        select(NewsItem).where(NewsItem.ticker == ticker)
        .order_by(NewsItem.published_at.desc().nulls_last()).limit(15)
    ).scalars().all())

    if position is not None and getattr(position, "price", None):
        detail.current_price = position.price
    else:
        q = session.get(QuoteCache, ticker)
        if q is not None and q.ok and q.price is not None:
            detail.current_price = Decimal(q.price)
    detail.reviews = list_reviews(session, ticker, detail.current_price)
    detail.trades = list_trades(session, ticker)
    detail.has_snapshot = pv is not None

    detail.chart = _build_chart(session, ticker, detail.changes, detail.reviews)
    detail.derived = _derived(detail, position)
    return detail


def _change_history(session: Session, ticker: str) -> list[dict]:
    rows = session.execute(
        select(Change, Snapshot.as_of)
        .join(Snapshot, Snapshot.id == Change.to_snapshot)
        .where(Change.ticker == ticker, Change.change_type != "HOLD")
        .order_by(Snapshot.as_of.desc())
    ).all()
    return [{"as_of": as_of, "change": c} for c, as_of in rows]


def _build_chart(session: Session, ticker: str, changes: list[dict],
                 reviews: list[ReviewRow] = ()) -> Chart | None:
    cache = session.get(PriceHistoryCache, ticker)
    if cache is None or not cache.series:
        return None
    series = [(d, Decimal(c)) for d, c in cache.series]
    closes = [c for _, c in series]
    lo, hi = min(closes), max(closes)
    span = (hi - lo) or Decimal("1")
    n = len(series)

    def x_at(i: int) -> float:
        return CHART_PAD + (CHART_W - 2 * CHART_PAD) * (i / max(1, n - 1))

    def y_at(price: Decimal) -> float:
        frac = (price - lo) / span
        return CHART_PAD + (CHART_H - 2 * CHART_PAD) * (1 - float(frac))

    # Sample to at most ~240 points for a light polyline.
    step = max(1, n // 240)
    pts = [f"{x_at(i):.1f},{y_at(series[i][1]):.1f}" for i in range(0, n, step)]
    if (n - 1) % step:
        pts.append(f"{x_at(n-1):.1f},{y_at(series[-1][1]):.1f}")

    date_to_i = {d: i for i, d in enumerate(d for d, _ in series)}
    def index_on_or_before(d: str) -> int | None:
        i = date_to_i.get(d)
        if i is None:  # nearest earlier date
            earlier = [j for j, (dd, _) in enumerate(series) if dd <= d]
            i = earlier[-1] if earlier else None
        return i

    markers: list[Marker] = []
    for ch in changes:
        d = ch["as_of"].isoformat()
        i = index_on_or_before(d)
        if i is not None:
            markers.append(Marker(x=x_at(i), y=y_at(series[i][1]),
                                  kind=ch["change"].change_type, label=d,
                                  price=series[i][1]))
    for row in reviews:
        r = row.review
        i = index_on_or_before(r.review_date.isoformat())
        if i is not None:
            markers.append(Marker(x=x_at(i), y=y_at(series[i][1]), kind="REVIEW",
                                  label=f"{r.review_date}{' · ' + r.stance if r.stance else ''}",
                                  price=Decimal(r.price) if r.price is not None else series[i][1]))

    gridlines = [Gridline(y=y_at(lv), frac=y_at(lv) / CHART_H, level=lv)
                 for lv in _grid_levels(lo, hi)]
    last_y = y_at(series[-1][1])
    return Chart(
        points=" ".join(pts), markers=markers, gridlines=gridlines, lo=lo, hi=hi,
        first_date=series[0][0], last_date=series[-1][0],
        last_x=x_at(n - 1), last_y=last_y,
        last_close=series[-1][1], last_frac=last_y / CHART_H,
    )


def _derived(detail: TickerDetail, position) -> dict:
    # Every key the template reads must exist even when the input is missing:
    # `d.derived.x is not none` passes for Undefined and then crashes on compare.
    d: dict = {
        "implied_upside": None,
        "range52_pos": None,
        "dist_dma50": None,
        "dist_dma200": None,
        "total_grades": 0,
    }
    f = detail.fundamentals
    price = getattr(position, "price", None) if position else None
    if f is None:
        return d
    if f.target_consensus and price:
        d["implied_upside"] = (Decimal(f.target_consensus) / price) - 1
    if f.week52_high and f.week52_low and price:
        rng = Decimal(f.week52_high) - Decimal(f.week52_low)
        if rng:
            d["range52_pos"] = (price - Decimal(f.week52_low)) / rng
    if f.dma_50 and price:
        d["dist_dma50"] = (price / Decimal(f.dma_50)) - 1
    if f.dma_200 and price:
        d["dist_dma200"] = (price / Decimal(f.dma_200)) - 1
    total_grades = sum(x or 0 for x in (
        f.grades_strong_buy, f.grades_buy, f.grades_hold, f.grades_sell, f.grades_strong_sell))
    d["total_grades"] = total_grades
    return d
