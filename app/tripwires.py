"""Tripwires — the written exit rules (Risk page, Zone 3).

Reads the cache only, never the network, matching signals.py. Three modes:

  auto   — computed here at read time. Currently one metric:
             dilution_yoy — annualized growth of shares outstanding, from the
             fundamentals_snapshot history (kept per refresh precisely so
             questions like this are answerable).
  semi   — the saved reading stands, but the row is flagged "data due" when the
           holding has reported (or reports within 14 days) since the reading
           was last updated — the cue to re-enter it after each print.
  manual — event rules; whatever was last saved stands.

The saved status is the managers' judgment; the computed status (auto rules)
is shown next to it and wins for the page's headline count when it is worse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EarningsHistory, FundamentalsSnapshot, Tripwire

EARNINGS_LOOKAHEAD_DAYS = 14
DILUTION_MIN_SPAN_DAYS = 90       # need at least a quarter of history
DILUTION_MAX_LOOKBACK_DAYS = 550  # oldest comparison point considered

_SEVERITY = {"ok": 0, "watch": 1, "triggered": 2}


@dataclass
class TripwireRow:
    id: int
    ticker: str | None
    rule: str
    action: str
    mode: str
    saved_status: str            # ok | watch | triggered (as last saved)
    latest_value: str | None
    note: str | None
    updated_at: datetime
    data_due: bool = False       # semi: a print landed/looms since last update
    computed_value: str | None = None    # auto: the number, formatted
    computed_status: str | None = None   # auto: ok | triggered | None (no data)
    computed_detail: str | None = None   # auto: how it was derived / why absent

    @property
    def effective_status(self) -> str:
        """Worst of saved and computed — what the headline should count."""
        worst = self.saved_status
        if self.computed_status and _SEVERITY.get(self.computed_status, 0) > _SEVERITY.get(worst, 0):
            worst = self.computed_status
        return worst


@dataclass
class TripwiresView:
    rows: list[TripwireRow] = field(default_factory=list)
    triggered: int = 0
    watch: int = 0
    due: int = 0


def build_tripwires(session: Session) -> TripwiresView:
    wires = session.execute(
        select(Tripwire).where(Tripwire.active.is_(True)).order_by(Tripwire.id)
    ).scalars().all()

    view = TripwiresView()
    today = date.today()
    horizon = today + timedelta(days=EARNINGS_LOOKAHEAD_DAYS)

    # Earnings dates per ticker, for the semi "data due" flag.
    tickers = {w.ticker for w in wires if w.ticker}
    earnings_by_ticker: dict[str, list[date]] = {}
    if tickers:
        for e in session.execute(
            select(EarningsHistory).where(EarningsHistory.ticker.in_(tickers))
        ).scalars().all():
            earnings_by_ticker.setdefault(e.ticker, []).append(e.fiscal_ending)

    for w in wires:
        row = TripwireRow(
            id=w.id, ticker=w.ticker, rule=w.rule, action=w.action, mode=w.mode,
            saved_status=w.status, latest_value=w.latest_value, note=w.note,
            updated_at=w.updated_at,
        )

        if w.mode == "semi" and w.ticker:
            last = w.updated_at.date()
            row.data_due = any(
                last <= fe <= horizon
                for fe in earnings_by_ticker.get(w.ticker, [])
            )

        if w.mode == "auto" and w.metric == "dilution_yoy" and w.ticker:
            _compute_dilution(session, w, row)

        view.rows.append(row)

    view.rows.sort(key=lambda r: (-_SEVERITY.get(r.effective_status, 0), not r.data_due, r.id))
    view.triggered = sum(1 for r in view.rows if r.effective_status == "triggered")
    view.watch = sum(1 for r in view.rows if r.effective_status == "watch")
    view.due = sum(1 for r in view.rows if r.data_due)
    return view


def _compute_dilution(session: Session, w: Tripwire, row: TripwireRow) -> None:
    """Annualized share-count growth from the fundamentals history."""
    rows = session.execute(
        select(FundamentalsSnapshot)
        .where(
            FundamentalsSnapshot.ticker == w.ticker,
            FundamentalsSnapshot.shares_outstanding.isnot(None),
        )
        .order_by(FundamentalsSnapshot.as_of.desc())
    ).scalars().all()
    if not rows:
        row.computed_detail = "no shares-outstanding data cached yet"
        return

    latest = rows[0]
    cutoff = latest.as_of - timedelta(days=DILUTION_MAX_LOOKBACK_DAYS)
    base = None
    for r in reversed(rows):            # oldest first
        if r.as_of >= cutoff:
            base = r
            break
    if base is None or base.as_of == latest.as_of:
        row.computed_detail = "only one fundamentals refresh so far — history builds daily"
        return

    span = (latest.as_of - base.as_of).days
    if span < DILUTION_MIN_SPAN_DAYS:
        row.computed_detail = f"history spans only {span}d (need ≥{DILUTION_MIN_SPAN_DAYS}d)"
        return

    s_new = Decimal(latest.shares_outstanding)
    s_old = Decimal(base.shares_outstanding)
    if s_old <= 0:
        row.computed_detail = "bad base share count"
        return

    growth = s_new / s_old - 1
    annualized = growth * Decimal(365) / Decimal(span)   # linear annualization
    row.computed_value = f"{annualized * 100:+.1f}%/yr"
    row.computed_detail = (
        f"shares {s_old:,.0f} → {s_new:,.0f} over {span}d "
        f"({base.as_of} → {latest.as_of})"
    )
    if w.threshold is not None:
        th = Decimal(w.threshold)
        breached = annualized > th if (w.direction or "above") == "above" else annualized < th
        row.computed_status = "triggered" if breached else "ok"


def update_tripwire(
    session: Session, wire_id: int, *,
    status: str, latest_value: str | None, note: str | None,
) -> bool:
    """Save a manual/semi reading. Returns False if the id is unknown or the
    status is not one of the three allowed values. Caller commits."""
    if status not in ("ok", "watch", "triggered"):
        return False
    w = session.get(Tripwire, wire_id)
    if w is None:
        return False
    w.status = status
    w.latest_value = (latest_value or "").strip() or None
    w.note = (note or "").strip() or None
    from datetime import timezone
    w.updated_at = datetime.now(timezone.utc)
    return True
