"""Risk budget — two limits evaluated alongside the tripwires.

  ai_complex    AI-complex market value (the in_ai_complex bets) / NAV vs
                risk_config.AI_COMPLEX_CAP. The stress scenario already builds
                the aggregate; the cap is one comparison.
  monthly_loss  NAV-per-share return month-to-date and over the trailing 30
                days vs risk_config.MONTHLY_LOSS_TRIGGER. A breach means the
                de-risking step is due.

Status per rule: ok | warn (inside the warn band) | breach | unknown (no data —
shown, never silently passed). Reads cache only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app import risk_config as rc
from app.performance import list_nav_points

ZERO = Decimal("0")


@dataclass
class BudgetRule:
    key: str
    label: str
    value: Decimal | None        # the measured figure (fraction)
    limit: Decimal               # the cap / trigger (fraction)
    status: str                  # ok | warn | breach | unknown
    action: str
    detail: str = ""

    @property
    def headroom(self) -> Decimal | None:
        """Distance to the limit in the safe direction (positive = room left)."""
        if self.value is None:
            return None
        return (self.limit - self.value) if self.key == "ai_complex" else (self.value - self.limit)


@dataclass
class RiskBudget:
    rules: list[BudgetRule] = field(default_factory=list)

    @property
    def breaches(self) -> list[BudgetRule]:
        return [r for r in self.rules if r.status == "breach"]

    @property
    def warnings(self) -> list[BudgetRule]:
        return [r for r in self.rules if r.status == "warn"]


def ai_complex_rule(exposure) -> BudgetRule:
    cap = rc.AI_COMPLEX_CAP
    rule = BudgetRule("ai_complex", "AI-complex cap (% of NAV)", None, cap, "unknown",
                      rc.AI_COMPLEX_ACTION)
    risk = getattr(exposure, "risk", None) if exposure is not None else None
    if risk is None or not exposure.total_value:
        rule.detail = "no priced snapshot"
        return rule
    v = risk.ai_complex_fund_weight
    rule.value = v
    rule.status = "breach" if v > cap else ("warn" if v > cap - rc.AI_COMPLEX_WARN_BAND else "ok")
    rule.detail = f"{risk.ai_complex_invested_weight * 100:.1f}% of invested equity"
    return rule


def monthly_loss_rule(navs, today: date | None = None) -> BudgetRule:
    trig = rc.MONTHLY_LOSS_TRIGGER
    rule = BudgetRule("monthly_loss", "Monthly-loss trigger (NAV)", None, trig, "unknown",
                      rc.DERISK_ACTION)
    if len(navs) < 2:
        rule.detail = "needs 2+ fund prices"
        return rule
    today = today or date.today()
    last = navs[-1]
    nav1 = Decimal(last.nav_per_share)

    def nav_on_or_before(d: date):
        prev = None
        for p in navs:
            if p.as_of <= d:
                prev = p
            else:
                break
        return prev

    month_base = nav_on_or_before(date(last.as_of.year, last.as_of.month, 1) - timedelta(days=1))
    roll_base = nav_on_or_before(last.as_of - timedelta(days=30))
    mtd = (nav1 / Decimal(month_base.nav_per_share) - 1) if month_base else None
    r30 = (nav1 / Decimal(roll_base.nav_per_share) - 1) if roll_base else None
    vals = [v for v in (mtd, r30) if v is not None]
    if not vals:
        rule.detail = "no fund price a month back"
        return rule
    worst = min(vals)
    rule.value = worst
    rule.status = ("breach" if worst <= trig else
                   "warn" if worst <= trig + rc.MONTHLY_LOSS_WARN_BAND else "ok")
    parts = []
    if mtd is not None:
        parts.append(f"MTD {mtd * 100:+.1f}%")
    if r30 is not None:
        parts.append(f"30d {r30 * 100:+.1f}%")
    parts.append(f"fund price {last.as_of}")
    age = (today - last.as_of).days
    if age > rc.NAV_STALE_DAYS:
        parts.append(f"{age}d old — add a fresh fund price")
        if rule.status == "ok":
            rule.status = "unknown"      # a stale price can't vouch for this month
    rule.detail = " · ".join(parts)
    return rule


def build_risk_budget(session: Session, exposure=None, today: date | None = None) -> RiskBudget:
    if exposure is None:
        from app.exposure import build_exposure
        exposure = build_exposure(session)
    return RiskBudget(rules=[
        ai_complex_rule(exposure),
        monthly_loss_rule(list_nav_points(session), today),
    ])
