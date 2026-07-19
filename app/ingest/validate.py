"""Validate parsed holdings and build the change preview against the last snapshot.

Blocking issues stop the commit. Warnings are surfaced but the user may proceed.
The preview diff (opens/closes/adds/trims) is the main defense against uploading
the wrong file or the wrong date — it is shown before anything is written.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest.parser import ParsedHolding
from app.models import HoldingSnapshot, Security, Snapshot

# Thresholds from the roadmap's warn-only list.
_COUNT_DRIFT_PCT = Decimal("0.20")   # ticker count changing by more than ~20%
_UNITS_MULTIPLE = Decimal("5")       # any single position's units changing by more than 5x


@dataclass
class ChangeLine:
    ticker: str
    change_type: str                 # OPEN | CLOSE | ADD | TRIM | HOLD
    units_before: Decimal | None
    units_after: Decimal | None
    units_delta: Decimal | None
    pct_delta: Decimal | None        # fraction, e.g. 0.05 == +5%


@dataclass
class ValidationResult:
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    new_tickers: list[str] = field(default_factory=list)   # not in `securities`
    changes: list[ChangeLine] = field(default_factory=list)
    duplicate_as_of: bool = False
    prior_as_of: date | None = None

    @property
    def ok_to_commit(self) -> bool:
        return not self.blocking

    def summary_counts(self) -> dict[str, int]:
        c = Counter(line.change_type for line in self.changes)
        return {k: c.get(k, 0) for k in ("OPEN", "ADD", "TRIM", "CLOSE", "HOLD")}


def validate_snapshot(
    holdings: list[ParsedHolding],
    as_of: date,
    session: Session,
    *,
    quote_check=None,
) -> ValidationResult:
    """Run every check and assemble the change preview.

    `quote_check`, if provided, is a callable(ticker) -> bool used for the
    live-quote validation. When None (no price source configured yet), that check
    is skipped with a visible warning rather than silently passing.
    """
    result = ValidationResult()

    # --- Blocking: structural integrity of the file itself ---
    ticker_counts = Counter(h.ticker for h in holdings)
    dups = [t for t, n in ticker_counts.items() if n > 1]
    if dups:
        result.blocking.append(f"Duplicate ticker(s) within the file: {', '.join(sorted(dups))}.")

    for h in holdings:
        if h.units <= 0:
            result.blocking.append(f"{h.ticker}: units must be positive (got {h.units}).")
        if h.avg_cost <= 0:
            result.blocking.append(f"{h.ticker}: cost must be positive (got {h.avg_cost}).")

    # --- Blocking: duplicate as_of requires explicit overwrite ---
    existing = session.execute(
        select(Snapshot).where(Snapshot.as_of == as_of)
    ).scalar_one_or_none()
    if existing is not None:
        result.duplicate_as_of = True  # not fatal by itself; the UI asks to confirm overwrite

    # --- Blocking: live quote lookup (hook) ---
    if quote_check is None:
        result.warnings.append(
            "Live quote validation skipped — no price source configured yet "
            "(arrives with the Phase 4 data layer). Tickers were not price-verified."
        )
    else:
        unquotable = [h.ticker for h in holdings if not h.is_cash and not quote_check(h.ticker)]
        if unquotable:
            result.blocking.append(
                f"No live quote for: {', '.join(sorted(unquotable))}. "
                "Check the ticker/exchange suffix."
            )

    # --- Warn: duplicate ISIN across different tickers (known collision exists) ---
    isin_to_tickers: dict[str, set[str]] = {}
    for h in holdings:
        if h.isin:
            isin_to_tickers.setdefault(h.isin, set()).add(h.ticker)
    for isin, tickers in isin_to_tickers.items():
        if len(tickers) > 1:
            result.warnings.append(
                f"ISIN {isin} is shared by {', '.join(sorted(tickers))} "
                "(known: MongoDB and Toast collide). Verify this is expected."
            )

    # --- Prior snapshot: diff, new tickers, drift warnings ---
    prior = session.execute(
        select(Snapshot).where(Snapshot.as_of < as_of).order_by(Snapshot.as_of.desc()).limit(1)
    ).scalar_one_or_none()

    prior_units: dict[str, Decimal] = {}
    if prior is not None:
        result.prior_as_of = prior.as_of
        rows = session.execute(
            select(HoldingSnapshot.ticker, HoldingSnapshot.units).where(
                HoldingSnapshot.snapshot_id == prior.id
            )
        ).all()
        prior_units = {t: u for t, u in rows}

    result.changes = _build_changes(holdings, prior_units)

    if prior is not None:
        before_n, after_n = len(prior_units), len(holdings)
        if before_n:
            drift = abs(Decimal(after_n) - Decimal(before_n)) / Decimal(before_n)
            if drift > _COUNT_DRIFT_PCT:
                result.warnings.append(
                    f"Position count changed from {before_n} to {after_n} "
                    f"({drift:.0%}) vs the {prior.as_of} snapshot — larger than expected."
                )
        for line in result.changes:
            if (
                line.units_before is not None
                and line.units_after is not None
                and line.units_before > 0
                and (line.units_after / line.units_before > _UNITS_MULTIPLE
                     or line.units_before / line.units_after > _UNITS_MULTIPLE)
            ):
                result.warnings.append(
                    f"{line.ticker}: units changed more than 5x "
                    f"({line.units_before} → {line.units_after}). Verify."
                )

    # --- Warn: tickers not yet in `securities` (need pillar assignment) ---
    known = set(
        session.execute(select(Security.ticker)).scalars().all()
    )
    result.new_tickers = sorted(
        {h.ticker for h in holdings if h.ticker not in known and not h.is_cash}
    )

    return result


def _build_changes(
    holdings: list[ParsedHolding], prior_units: dict[str, Decimal]
) -> list[ChangeLine]:
    lines: list[ChangeLine] = []
    current = {h.ticker: h.units for h in holdings}

    for ticker, units_after in current.items():
        units_before = prior_units.get(ticker)
        if units_before is None:
            change_type = "OPEN"
            delta = units_after
            pct = None
        else:
            delta = units_after - units_before
            pct = (delta / units_before) if units_before else None
            if delta == 0:
                change_type = "HOLD"
            elif delta > 0:
                change_type = "ADD"
            else:
                change_type = "TRIM"
        lines.append(
            ChangeLine(ticker, change_type, units_before, units_after, delta, pct)
        )

    # Closes: in the prior snapshot but gone now.
    for ticker, units_before in prior_units.items():
        if ticker not in current:
            lines.append(
                ChangeLine(ticker, "CLOSE", units_before, None, -units_before, Decimal("-1"))
            )

    order = {"OPEN": 0, "CLOSE": 1, "ADD": 2, "TRIM": 3, "HOLD": 4}
    lines.sort(key=lambda l: (order[l.change_type], l.ticker))
    return lines
