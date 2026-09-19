"""Assemble the position table from the latest snapshot + the price cache.

Everything here is derived at read time and nothing hits the network — the page
reads the cache written by the refresh action. All money is Decimal; a position
we could not price is carried through with priced=False so the UI can badge it
instead of pretending its value is zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest.normalize import CASH_TICKER
from app.models import HoldingSnapshot, QuoteCache, Security, Snapshot
from app.prices import cache_age_seconds, load_quote_cache

ZERO = Decimal("0")


@dataclass
class PositionRow:
    ticker: str
    name: str | None
    pillar: str | None
    units: Decimal
    avg_cost: Decimal
    is_cash: bool
    priced: bool
    price: Decimal | None = None
    currency: str | None = None
    market_value: Decimal | None = None
    weight: Decimal | None = None
    unrl_pl: Decimal | None = None
    unrl_pct: Decimal | None = None
    day_change_pct: Decimal | None = None      # fraction
    day_pl: Decimal | None = None
    contribution: Decimal | None = None         # share of today's fund move
    error: str | None = None


@dataclass
class PositionsView:
    as_of: date
    staleness_days: int
    rows: list[PositionRow]
    pillars: list[str]
    cache_age_seconds: float | None
    header: dict = field(default_factory=dict)
    unpriced: list[str] = field(default_factory=list)
    # Staleness is measured from the last custodian file; manual trades on top
    # (app/trades.py) are counted, never allowed to make the book look fresh.
    custodian_as_of: date | None = None
    manual_trades: int = 0


def build_positions(session: Session) -> PositionsView | None:
    latest = session.execute(
        select(Snapshot).order_by(Snapshot.as_of.desc()).limit(1)
    ).scalar_one_or_none()
    if latest is None:
        return None

    holdings = session.execute(
        select(HoldingSnapshot).where(HoldingSnapshot.snapshot_id == latest.id)
    ).scalars().all()
    secs = {s.ticker: s for s in session.execute(select(Security)).scalars().all()}
    quotes = load_quote_cache(session)

    rows: list[PositionRow] = []
    total_mv = ZERO           # priced market value (incl. cash)
    total_prev = ZERO         # priced value at prev close (incl. cash)
    total_cost = ZERO         # cost basis of priced non-cash positions
    cash_value = ZERO

    for h in holdings:
        is_cash = h.ticker == CASH_TICKER
        sec = secs.get(h.ticker)
        row = PositionRow(
            ticker=h.ticker,
            name=(sec.name if sec else None) or (quotes[h.ticker].name if h.ticker in quotes else None),
            pillar=sec.pillar if sec else None,
            units=h.units,
            avg_cost=h.avg_cost,
            is_cash=is_cash,
            priced=True,
        )

        if is_cash:
            row.price = Decimal("1")
            row.market_value = h.units
            cash_value = h.units
            total_mv += h.units
            total_prev += h.units
            rows.append(row)
            continue

        q = quotes.get(h.ticker)
        if q is None or not q.ok or q.price is None:
            row.priced = False
            row.error = (q.error if q else "no live price yet — refresh prices")
            rows.append(row)
            continue

        price = Decimal(q.price)
        mv = h.units * price
        prev = h.units * (Decimal(q.prev_close) if q.prev_close is not None else price)
        row.price = price
        row.currency = q.currency
        row.market_value = mv
        row.unrl_pl = (price - h.avg_cost) * h.units
        row.unrl_pct = (price / h.avg_cost - 1) if h.avg_cost else None
        row.day_change_pct = (Decimal(q.day_change_pct) / 100) if q.day_change_pct is not None else None
        row.day_pl = mv - prev
        total_mv += mv
        total_prev += prev
        total_cost += h.units * h.avg_cost
        rows.append(row)

    # Second pass: weight and contribution now that totals are known.
    for row in rows:
        if row.market_value is not None and total_mv:
            row.weight = row.market_value / total_mv
        if row.day_pl is not None and total_prev:
            row.contribution = row.day_pl / total_prev

    priced_non_cash = [r for r in rows if r.priced and not r.is_cash]
    unpriced = [r.ticker for r in rows if not r.priced]
    day_pl_total = sum((r.day_pl for r in priced_non_cash if r.day_pl is not None), ZERO)
    unrl_total = total_mv - cash_value - total_cost

    header = {
        "total_value": total_mv,
        "cash_value": cash_value,
        "cash_pct": (cash_value / total_mv) if total_mv else None,
        "position_count": len(holdings),
        "day_pl": day_pl_total,
        "day_pct": (day_pl_total / total_prev) if total_prev else None,
        "unrl_pl": unrl_total,
        "unrl_pct": (unrl_total / total_cost) if total_cost else None,
        "unpriced_count": len(unpriced),
    }

    # Default order: biggest position first.
    rows.sort(key=lambda r: (r.market_value is None, -(r.market_value or ZERO)))
    pillars = sorted({r.pillar for r in rows if r.pillar})

    from app.trades import provenance
    prov = provenance(session)
    fresh_as_of = prov.custodian_as_of or latest.as_of

    return PositionsView(
        as_of=latest.as_of,
        staleness_days=(date.today() - fresh_as_of).days,
        custodian_as_of=prov.custodian_as_of,
        manual_trades=prov.manual_trades,
        rows=rows,
        pillars=pillars,
        cache_age_seconds=cache_age_seconds(session),
        header=header,
        unpriced=unpriced,
    )
