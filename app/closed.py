"""Closed positions: every ticker ever held that is absent from the latest
snapshot, with the economics of how it ended.

Pure read-time derivation over tables that already exist (snapshots, manual
trades, price history) — no new state, so it works retroactively for positions
closed before this view existed.

"Realized result" is final-leg economics: (exit − blended cost) × final units.
Profits already taken on earlier trims are not accumulated here — the ticker
page's trade and change history carries that story.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest.normalize import CASH_TICKER
from app.models import (
    HoldingSnapshot,
    ManualTrade,
    PriceHistoryCache,
    Security,
    Snapshot,
)


@dataclass
class ClosedPosition:
    ticker: str
    name: str | None
    pillar: str | None
    first_seen: date            # first snapshot that held it
    closed_on: date             # snapshot where it disappeared
    last_units: Decimal         # position in the last snapshot that held it
    avg_cost: Decimal
    cost_basis: Decimal         # last_units * avg_cost
    exit_price: Decimal | None
    exit_source: str | None     # "trade" | "close est."
    realized_pl: Decimal | None     # (exit - avg_cost) * last_units
    realized_pct: Decimal | None


def _history_close_at(session: Session, ticker: str, on: date) -> Decimal | None:
    """Last cached daily close on or before `on`."""
    cache = session.get(PriceHistoryCache, ticker)
    if cache is None or not cache.series:
        return None
    iso = on.isoformat()
    best = None
    for d, close in cache.series:          # stored oldest-first or not — scan all
        if d <= iso and (best is None or d > best[0]):
            best = (d, close)
    return Decimal(str(best[1])) if best else None


def build_closed_positions(session: Session) -> list[ClosedPosition]:
    snaps = session.execute(select(Snapshot).order_by(Snapshot.as_of)).scalars().all()
    if len(snaps) < 2:
        return []

    holdings: dict[int, dict[str, HoldingSnapshot]] = {s.id: {} for s in snaps}
    for h in session.execute(select(HoldingSnapshot)).scalars().all():
        if h.ticker != CASH_TICKER and h.snapshot_id in holdings:
            holdings[h.snapshot_id][h.ticker] = h

    latest = snaps[-1]
    current = set(holdings[latest.id])
    secs = {s.ticker: s for s in session.execute(select(Security)).scalars().all()}

    out: list[ClosedPosition] = []
    # A ticker is closed when it was held in some snapshot but is absent from
    # the latest. For re-opened-then-reclosed names, the LAST close is shown.
    for ticker in {t for sid in holdings for t in holdings[sid]} - current:
        held_in = [s for s in snaps if ticker in holdings[s.id]]
        last_snap = held_in[-1]
        after = next(s for s in snaps if s.as_of > last_snap.as_of)
        h = holdings[last_snap.id][ticker]
        units, avg_cost = Decimal(h.units), Decimal(h.avg_cost)

        # Exit price: the selling trade on the closing snapshot, else the cached
        # daily close on the close date (an estimate).
        exit_price = exit_source = None
        sells = session.execute(
            select(ManualTrade).where(
                ManualTrade.snapshot_id == after.id, ManualTrade.ticker == ticker,
                ManualTrade.side == "SELL",
            ).order_by(ManualTrade.id.desc())
        ).scalars().first()
        if sells is not None:
            exit_price, exit_source = Decimal(sells.price), "trade"
        else:
            est = _history_close_at(session, ticker, after.as_of)
            if est is not None:
                exit_price, exit_source = est, "close est."

        sec = secs.get(ticker)
        cost_basis = units * avg_cost
        realized = (exit_price - avg_cost) * units if exit_price is not None else None
        out.append(ClosedPosition(
            ticker=ticker, name=(sec.name if sec else None),
            pillar=(sec.pillar if sec else None),
            first_seen=(sec.first_seen if sec and sec.first_seen else held_in[0].as_of),
            closed_on=after.as_of,
            last_units=units, avg_cost=avg_cost, cost_basis=cost_basis,
            exit_price=exit_price, exit_source=exit_source,
            realized_pl=realized,
            realized_pct=((exit_price / avg_cost - 1) if exit_price is not None and avg_cost else None),
        ))

    out.sort(key=lambda c: (c.closed_on, c.ticker), reverse=True)
    return out
