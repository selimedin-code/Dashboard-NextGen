"""Phase 2 — the diff engine.

Turns consecutive snapshots into the persistent `changes` table. This is what
separates *decisions* from *mechanics*: when new capital is deployed pro-rata,
every position's units rise by roughly the same percentage, so a position moving
with the crowd is FLOW_DRIVEN and one moving against it is DISCRETIONARY.

Nothing here needs prices. The implied trade price is derived from the
blended-cost identity; a proper against-the-tape sanity check waits for the
Phase 4 price layer (see IMPLIED_PRICE note).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Change, HoldingSnapshot, Snapshot

# A "flow event" is a pro-rata capital move. If the median position barely moved,
# there was no flow and every change is a decision.
FLOW_MIN = Decimal("0.005")            # 0.5% median move to count as a flow event
_BAND_FLOOR = Decimal("0.01")          # ±1 percentage point
_BAND_REL = Decimal("0.30")            # or ±30% of the median, whichever is larger
_COST_EPS = Decimal("0.01")            # 1% cost drift tolerance on a trim


@dataclass
class HoldingRow:
    units: Decimal
    avg_cost: Decimal


def _load_holdings(session: Session, snapshot_id: int) -> dict[str, HoldingRow]:
    rows = session.execute(
        select(HoldingSnapshot.ticker, HoldingSnapshot.units, HoldingSnapshot.avg_cost).where(
            HoldingSnapshot.snapshot_id == snapshot_id
        )
    ).all()
    return {t: HoldingRow(u, c) for t, u, c in rows}


def compute_changes(
    session: Session, from_snapshot: Snapshot, to_snapshot: Snapshot
) -> list[Change]:
    """Compute and persist the change rows for one snapshot pair.

    Idempotent: existing rows for this (from, to) pair are replaced. Returns the
    persisted Change objects (not yet committed — caller owns the transaction).
    """
    before = _load_holdings(session, from_snapshot.id)
    after = _load_holdings(session, to_snapshot.id)

    # Median percentage move across positions held in BOTH snapshots (a defined
    # pct_delta). Cash is excluded — subscriptions/redemptions move it on its own.
    deltas: list[Decimal] = []
    for ticker, a in after.items():
        b = before.get(ticker)
        if b is None or b.units == 0 or ticker == "USD Cash":
            continue
        deltas.append((a.units - b.units) / b.units)
    median = Decimal(str(statistics.median(deltas))) if deltas else Decimal("0")
    flow_event = abs(median) >= FLOW_MIN

    # Clear any prior rows for this pair so a re-run is clean.
    session.execute(
        delete(Change).where(
            Change.from_snapshot == from_snapshot.id,
            Change.to_snapshot == to_snapshot.id,
        )
    )

    changes: list[Change] = []
    for ticker in sorted(set(before) | set(after)):
        b = before.get(ticker)
        a = after.get(ticker)
        change = _build_change(ticker, b, a, median, flow_event)
        change.from_snapshot = from_snapshot.id
        change.to_snapshot = to_snapshot.id
        session.add(change)
        changes.append(change)

    return changes


def _build_change(
    ticker: str,
    b: HoldingRow | None,
    a: HoldingRow | None,
    median: Decimal,
    flow_event: bool,
) -> Change:
    units_before = b.units if b else None
    units_after = a.units if a else None
    cost_before = b.avg_cost if b else None
    cost_after = a.avg_cost if a else None

    if b is None:                       # OPEN
        change_type = "OPEN"
        delta = units_after
        pct = None
        implied = cost_after            # first lot: blended cost ≈ the trade price
        classification = "DISCRETIONARY"
    elif a is None:                     # CLOSE
        change_type = "CLOSE"
        delta = -units_before
        pct = Decimal("-1")
        implied = None                  # sale price is not recoverable from avg cost
        classification = "DISCRETIONARY"
    else:
        delta = units_after - units_before
        pct = (delta / units_before) if units_before else None
        if delta == 0:
            change_type, implied, classification = "HOLD", None, None
        elif delta > 0:                 # ADD
            change_type = "ADD"
            implied = _implied_add_price(units_before, cost_before, units_after, cost_after, delta)
            classification = _classify(pct, median, flow_event, implied)
        else:                           # TRIM
            change_type = "TRIM"
            implied = None              # a sale does not move the blended average
            # A trim that moved the blended cost is not a clean partial sale.
            if _cost_moved(cost_before, cost_after):
                classification = "AMBIGUOUS"
            else:
                classification = _classify(pct, median, flow_event, implied)

    return Change(
        ticker=ticker,
        change_type=change_type,
        units_before=units_before,
        units_after=units_after,
        units_delta=delta,
        pct_delta=pct,
        cost_before=cost_before,
        cost_after=cost_after,
        implied_price=implied,
        classification=classification,
    )


def _implied_add_price(
    units_before: Decimal, cost_before: Decimal,
    units_after: Decimal, cost_after: Decimal, delta: Decimal,
) -> Decimal | None:
    """Blended-cost identity: P = (u1*c1 - u0*c0) / (u1 - u0).

    IMPLIED_PRICE note: with no price history we cannot yet check this against the
    period's actual range to flag a round trip. That sanity check lands with the
    Phase 4 price layer; for now a non-positive result marks the row AMBIGUOUS.
    """
    if delta == 0:
        return None
    return (units_after * cost_after - units_before * cost_before) / delta


def _cost_moved(before: Decimal | None, after: Decimal | None) -> bool:
    if before is None or after is None or before == 0:
        return False
    return abs(after - before) / before > _COST_EPS


def _classify(
    pct: Decimal | None, median: Decimal, flow_event: bool, implied: Decimal | None
) -> str:
    if implied is not None and implied <= 0:
        return "AMBIGUOUS"          # nonsensical price → more than one trade happened
    if pct is None:
        return "DISCRETIONARY"
    if flow_event and abs(pct - median) <= _band(median):
        return "FLOW_DRIVEN"
    return "DISCRETIONARY"


def _band(median: Decimal) -> Decimal:
    return max(_BAND_FLOOR, _BAND_REL * abs(median))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def rebuild_changes(session: Session, *, commit: bool = False) -> int:
    """Recompute every consecutive-pair diff from scratch. Returns pair count.

    A full rebuild is deliberate: it is trivially cheap for a handful of snapshots
    and stays correct when a snapshot is inserted or overwritten in the MIDDLE of
    the series (which affects both of its neighbouring pairs). Called on every
    commit; caller decides whether to commit the transaction.
    """
    session.execute(delete(Change))
    snaps = session.execute(select(Snapshot).order_by(Snapshot.as_of)).scalars().all()
    pairs = 0
    for prev, curr in zip(snaps, snaps[1:]):
        compute_changes(session, prev, curr)
        pairs += 1
    if commit:
        session.commit()
    return pairs
