"""Phase 5 — exposure and concentration.

For a book this concentrated this is where the real risk lives, and it needs only
the current snapshot. Everything is derived from the priced positions; unpriced
names are excluded from the maths and reported separately so the numbers are not
quietly wrong.

Two weight bases, both labelled in the UI:
  - fund weight       = market value / total fund (incl. cash) — the intuitive
                        "this name is X% of the fund", used for top-N and flags
  - invested weight   = market value / invested equity (excl. cash) — the honest
                        basis for concentration (HHI, effective N)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Pillar
from app.positions import build_positions
from sqlalchemy import select

ZERO = Decimal("0")
SINGLE_NAME_WARN = Decimal("0.05")   # 5% of the fund
SINGLE_NAME_HIGH = Decimal("0.08")   # 8% of the fund


@dataclass
class Holding:
    rank: int
    ticker: str
    pillar: str | None
    market_value: Decimal
    fund_weight: Decimal
    cumulative: Decimal          # running sum of fund weight, top-down
    flag: str | None = None      # "high" | "warn" | None


@dataclass
class PillarExposure:
    name: str
    etf: str | None
    alt_etf: str | None
    caveat: str | None
    count: int
    market_value: Decimal
    fund_weight: Decimal


@dataclass
class ExposureView:
    as_of: date
    staleness_days: int
    total_value: Decimal
    invested_value: Decimal
    cash_value: Decimal
    cash_pct: Decimal
    name_count: int              # priced non-cash names
    unpriced: list[str]
    hhi: Decimal                 # on invested weights
    effective_n: Decimal
    top5: Decimal
    top10: Decimal
    top20: Decimal
    holdings: list[Holding] = field(default_factory=list)
    pillars: list[PillarExposure] = field(default_factory=list)
    flags: list[Holding] = field(default_factory=list)


def build_exposure(session: Session) -> ExposureView | None:
    pv = build_positions(session)
    if pv is None:
        return None

    priced = [r for r in pv.rows if not r.is_cash and r.priced and r.market_value is not None]
    total = pv.header["total_value"] or ZERO
    cash = pv.header["cash_value"] or ZERO
    invested = sum((r.market_value for r in priced), ZERO)

    # Top holdings by market value, with fund weight and cumulative.
    priced.sort(key=lambda r: r.market_value, reverse=True)
    holdings: list[Holding] = []
    running = ZERO
    for i, r in enumerate(priced, start=1):
        fw = (r.market_value / total) if total else ZERO
        running += fw
        flag = "high" if fw >= SINGLE_NAME_HIGH else ("warn" if fw >= SINGLE_NAME_WARN else None)
        holdings.append(Holding(
            rank=i, ticker=r.ticker, pillar=r.pillar, market_value=r.market_value,
            fund_weight=fw, cumulative=running, flag=flag,
        ))

    def top_n(n: int) -> Decimal:
        return sum((h.fund_weight for h in holdings[:n]), ZERO)

    # Concentration on invested (equity-only) weights.
    hhi = sum(((r.market_value / invested) ** 2 for r in priced), ZERO) if invested else ZERO
    effective_n = (Decimal("1") / hhi) if hhi else ZERO

    # By pillar, with benchmark ETF from the taxonomy.
    pillar_meta = {p.name: p for p in session.execute(select(Pillar)).scalars().all()}
    agg: dict[str, list] = {}
    for r in priced:
        key = r.pillar or "— unassigned —"
        bucket = agg.setdefault(key, [ZERO, 0])
        bucket[0] += r.market_value
        bucket[1] += 1
    pillars: list[PillarExposure] = []
    for name, (mv, cnt) in agg.items():
        meta = pillar_meta.get(name)
        pillars.append(PillarExposure(
            name=name,
            etf=meta.primary_etf if meta else None,
            alt_etf=meta.alt_etf if meta else None,
            caveat=meta.caveat if meta else None,
            count=cnt, market_value=mv,
            fund_weight=(mv / total) if total else ZERO,
        ))
    pillars.sort(key=lambda p: p.market_value, reverse=True)

    return ExposureView(
        as_of=pv.as_of,
        staleness_days=pv.staleness_days,
        total_value=total,
        invested_value=invested,
        cash_value=cash,
        cash_pct=(cash / total) if total else ZERO,
        name_count=len(priced),
        unpriced=pv.unpriced,
        hhi=hhi,
        effective_n=effective_n,
        top5=top_n(5),
        top10=top_n(10),
        top20=top_n(20),
        holdings=holdings,
        pillars=pillars,
        flags=[h for h in holdings if h.flag],
    )
