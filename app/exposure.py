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
from app.risk_config import (
    BETS,
    SCENARIO_NAME,
    SCENARIO_NOTE,
    UNMAPPED,
    bet_for,
)
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
class BetExposure:
    """One effective bet: pillars collapsed to their shared macro driver."""
    key: str
    label: str
    in_ai_complex: bool
    count: int
    market_value: Decimal
    fund_weight: Decimal          # % of whole fund
    invested_weight: Decimal      # % of invested equity (the honest factor basis)
    day_pnl: Decimal              # today's $ move of the bet (priced names w/ day %)
    scenario_drawdown: Decimal    # assumption from risk_config
    scenario_loss: Decimal        # market_value * drawdown


@dataclass
class RiskView:
    """Zone 2 of the Risk page: effective bets, the AI-complex aggregate, and
    the capex air-pocket scenario. Derived entirely from priced positions —
    no new data source."""
    bets: list[BetExposure]
    ai_complex_value: Decimal
    ai_complex_invested_weight: Decimal
    ai_complex_fund_weight: Decimal
    effective_bet_count: int          # bets holding >1% of invested equity
    scenario_name: str
    scenario_note: str
    scenario_loss_total: Decimal
    scenario_loss_pct_aum: Decimal    # vs whole fund incl. cash (cash cushions)
    scenario_loss_pct_invested: Decimal


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
    risk: RiskView | None = None


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

    # --- effective bets (Risk page, Zone 2) -------------------------------
    # pillar NAME -> macro bet key, via the pillars table. Positions whose
    # pillar is missing or unmapped land in the visible "unmapped" bucket.
    bet_key_by_pillar_name = {p.name: p.macro_bet for p in pillar_meta.values()}
    bet_agg: dict[str, dict] = {}
    for r in priced:
        meta = bet_for(bet_key_by_pillar_name.get(r.pillar)) if r.pillar else UNMAPPED
        b = bet_agg.setdefault(meta.key, {"meta": meta, "mv": ZERO, "n": 0, "day": ZERO})
        b["mv"] += r.market_value
        b["n"] += 1
        dc = getattr(r, "day_change_pct", None)
        if dc is not None:
            # today's $ move: mv - mv/(1+dc)
            one = Decimal("1")
            b["day"] += r.market_value - (r.market_value / (one + dc))

    bets: list[BetExposure] = []
    for b in bet_agg.values():
        meta, mv = b["meta"], b["mv"]
        bets.append(BetExposure(
            key=meta.key, label=meta.label, in_ai_complex=meta.in_ai_complex,
            count=b["n"], market_value=mv,
            fund_weight=(mv / total) if total else ZERO,
            invested_weight=(mv / invested) if invested else ZERO,
            day_pnl=b["day"],
            scenario_drawdown=meta.scenario_drawdown,
            scenario_loss=mv * meta.scenario_drawdown,
        ))
    bets.sort(key=lambda x: (BETS[x.key].order if x.key in BETS else UNMAPPED.order))

    complex_mv = sum((b.market_value for b in bets if b.in_ai_complex), ZERO)
    scen_loss = sum((b.scenario_loss for b in bets), ZERO)
    risk = RiskView(
        bets=bets,
        ai_complex_value=complex_mv,
        ai_complex_invested_weight=(complex_mv / invested) if invested else ZERO,
        ai_complex_fund_weight=(complex_mv / total) if total else ZERO,
        effective_bet_count=sum(1 for b in bets if b.invested_weight > Decimal("0.01")),
        scenario_name=SCENARIO_NAME,
        scenario_note=SCENARIO_NOTE,
        scenario_loss_total=scen_loss,
        scenario_loss_pct_aum=(scen_loss / total) if total else ZERO,
        scenario_loss_pct_invested=(scen_loss / invested) if invested else ZERO,
    )

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
        risk=risk,
    )
