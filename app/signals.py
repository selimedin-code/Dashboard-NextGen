"""Phase 7 — signals and flags.

Deliberately the last and most restrained view: only flags backed by cached data,
each with the number behind it, so nothing here is noise. Everything reads the
cache (positions, fundamentals, exposure, earnings, news) — no network.

Signal families:
  moves         — outsized single-day price moves
  trend         — price below the 50/200-day moving average
  stops         — price at/near a user-set stop
  earnings      — a holding reports within the next two weeks
  concentration — single-name / top-10 / pillar weight breaches
  news          — genuinely recent headlines (awareness only)
  budget        — risk-budget limits (AI-complex cap, monthly-loss trigger)
  journal       — reviews past their horizon, waiting for a verdict
  cadence       — the custodian file is due / past the monthly minimum
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exposure import build_exposure
from app.models import EarningsHistory, FundamentalsSnapshot, NewsItem, Security
from app.positions import build_positions

MOVE_WARN = Decimal("0.05")
MOVE_HIGH = Decimal("0.08")
STOP_NEAR = Decimal("0.03")          # within 3% of stop
TOP10_BREACH = Decimal("0.45")
PILLAR_BREACH = Decimal("0.35")
EARNINGS_DAYS = 14
NEWS_DAYS = 3


@dataclass
class Signal:
    category: str
    ticker: str
    severity: str        # high | warn | info
    headline: str
    detail: str = ""


@dataclass
class SignalsView:
    as_of: date | None
    moves: list[Signal] = field(default_factory=list)
    trend: list[Signal] = field(default_factory=list)
    stops: list[Signal] = field(default_factory=list)
    earnings: list[Signal] = field(default_factory=list)
    concentration: list[Signal] = field(default_factory=list)
    news: list[Signal] = field(default_factory=list)
    budget: list[Signal] = field(default_factory=list)
    journal: list[Signal] = field(default_factory=list)
    cadence: list[Signal] = field(default_factory=list)
    fundamentals_coverage: int = 0     # held tickers with fundamentals cached
    held_count: int = 0
    total: int = 0

    def sections(self):
        return [
            ("Risk budget", "budget", self.budget),
            ("Snapshot cadence", "cadence", self.cadence),
            ("Outsized moves", "moves", self.moves),
            ("Trend (moving averages)", "trend", self.trend),
            ("Stops", "stops", self.stops),
            ("Earnings ahead", "earnings", self.earnings),
            ("Concentration", "concentration", self.concentration),
            ("Recent news", "news", self.news),
            ("Journal — verdicts due", "journal", self.journal),
        ]


def build_signals(session: Session) -> SignalsView:
    pv = build_positions(session)
    if pv is None:
        return SignalsView(as_of=None)

    view = SignalsView(as_of=pv.as_of)
    held = {r.ticker for r in pv.rows if not r.is_cash}
    view.held_count = len(held)

    priced = {r.ticker: r for r in pv.rows if not r.is_cash and r.priced}
    secs = {s.ticker: s for s in session.execute(select(Security)).scalars().all()}
    # Latest fundamentals row per ticker (desc order + setdefault keeps the newest).
    funds: dict[str, FundamentalsSnapshot] = {}
    for f in session.execute(
        select(FundamentalsSnapshot).order_by(FundamentalsSnapshot.as_of.desc())
    ).scalars().all():
        funds.setdefault(f.ticker, f)
    view.fundamentals_coverage = len(held & funds.keys())

    # --- moves ---
    for t, r in priced.items():
        dc = r.day_change_pct
        if dc is None or abs(dc) < MOVE_WARN:
            continue
        sev = "high" if abs(dc) >= MOVE_HIGH else "warn"
        view.moves.append(Signal("moves", t, sev,
            f"{'+' if dc > 0 else ''}{dc*100:.1f}% today",
            f"weight {r.weight*100:.1f}%" if r.weight else ""))
    view.moves.sort(key=lambda s: s.severity != "high")

    # --- trend (needs fundamentals) ---
    for t in sorted(held & funds.keys()):
        r = priced.get(t)
        f = funds[t]
        if r is None or r.price is None:
            continue
        price = r.price
        if f.dma_200 and price < Decimal(f.dma_200):
            pct = (price / Decimal(f.dma_200) - 1) * 100
            view.trend.append(Signal("trend", t, "warn",
                f"below 200-day ({pct:.1f}%)", "long-term trend broken"))
        elif f.dma_50 and price < Decimal(f.dma_50):
            pct = (price / Decimal(f.dma_50) - 1) * 100
            view.trend.append(Signal("trend", t, "info", f"below 50-day ({pct:.1f}%)"))

    # --- stops ---
    for t, sec in secs.items():
        if sec.stop_price is None or t not in priced or priced[t].price is None:
            continue
        price, stop = priced[t].price, Decimal(sec.stop_price)
        if price <= stop:
            view.stops.append(Signal("stops", t, "high",
                f"breached stop {stop:,.2f}", f"price {price:,.2f}"))
        elif price <= stop * (1 + STOP_NEAR):
            view.stops.append(Signal("stops", t, "warn",
                f"near stop {stop:,.2f}", f"price {price:,.2f}"))

    # --- earnings ahead ---
    horizon = date.today() + timedelta(days=EARNINGS_DAYS)
    upcoming = session.execute(
        select(EarningsHistory).where(
            EarningsHistory.eps_actual.is_(None),
            EarningsHistory.fiscal_ending >= date.today(),
            EarningsHistory.fiscal_ending <= horizon,
        ).order_by(EarningsHistory.fiscal_ending)
    ).scalars().all()
    for e in upcoming:
        if e.ticker not in held:
            continue
        days = (e.fiscal_ending - date.today()).days
        view.earnings.append(Signal("earnings", e.ticker,
            "warn" if days <= 5 else "info",
            f"reports {e.fiscal_ending}", f"in {days}d"))

    # --- concentration ---
    ev = build_exposure(session)
    if ev is not None:
        for h in ev.flags:
            view.concentration.append(Signal("concentration", h.ticker,
                "high" if h.flag == "high" else "warn",
                f"{h.fund_weight*100:.1f}% of fund", h.pillar or ""))
        if ev.top10 > TOP10_BREACH:
            view.concentration.append(Signal("concentration", "TOP 10", "warn",
                f"top 10 = {ev.top10*100:.1f}% of fund", f"threshold {TOP10_BREACH*100:.0f}%"))
        for p in ev.pillars:
            if p.fund_weight > PILLAR_BREACH:
                view.concentration.append(Signal("concentration", p.name, "warn",
                    f"{p.fund_weight*100:.1f}% of fund", f"{p.count} names"))

    # --- news (recent only) ---
    cutoff = datetime.now(timezone.utc) - timedelta(days=NEWS_DAYS)
    recent = session.execute(
        select(NewsItem).where(NewsItem.published_at >= cutoff)
        .order_by(NewsItem.published_at.desc()).limit(25)
    ).scalars().all()
    for n in recent:
        if n.ticker not in held:
            continue
        view.news.append(Signal("news", n.ticker, "info", n.title or "",
            (n.source or "") + (f" · {n.published_at:%m-%d}" if n.published_at else "")))

    # --- risk budget ---
    from app.risk_budget import build_risk_budget
    for rule in build_risk_budget(session, exposure=ev).rules:
        if rule.status not in ("breach", "warn"):
            continue
        lim = f"limit {rule.limit * 100:.0f}%"
        view.budget.append(Signal("budget", "AI COMPLEX" if rule.key == "ai_complex" else "FUND NAV",
            "high" if rule.status == "breach" else "warn",
            f"{rule.label}: {rule.value * 100:.1f}% ({lim})"
            + (" — BREACHED" if rule.status == "breach" else " — near limit"),
            rule.action if rule.status == "breach" else rule.detail))

    # --- snapshot cadence (measured from the last custodian file) ---
    from app.risk_config import SNAPSHOT_DUE_DAYS, SNAPSHOT_STALE_DAYS
    age = pv.staleness_days
    if age > SNAPSHOT_DUE_DAYS:
        view.cadence.append(Signal("cadence", "CUSTODIAN FILE",
            "high" if age > SNAPSHOT_STALE_DAYS else "warn",
            f"last custodian file {pv.custodian_as_of or pv.as_of} is {age}d old",
            f"monthly minimum — {'past' if age > SNAPSHOT_STALE_DAYS else 'due within'} "
            f"{SNAPSHOT_STALE_DAYS}d; wider gaps blind the trade classifier"))

    # --- journal: horizons passed, verdict not yet forced ---
    from app.reviews import build_journal
    for row in build_journal(session).due:
        r = row.review
        view.journal.append(Signal("journal", r.ticker, "warn" if row.days_overdue > 7 else "info",
            f"review {r.review_date} — horizon {r.horizon_date} passed, score it",
            (r.expected_outcome or "")[:90]))

    view.total = sum(len(s) for _, _, s in view.sections())
    return view
