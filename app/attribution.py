"""Period attribution (Brinson) against the policy benchmark.

One period = one consecutive snapshot pair A -> B. The portfolio leg is A's
holdings, buy-and-hold at A's and B's closes (cash is its own segment earning
nothing, so cash drag shows up as allocation). The benchmark leg is the policy:
target weight x pillar benchmark (see performance.load_policy). Per segment p:

    allocation  = (w_p - W_p) x (B_p - R_B)    did the wave tilts pay?
    selection   = W_p x (R_p - B_p)            did our names beat their pillar?
    interaction = (w_p - W_p) x (R_p - B_p)    tilting into where the names won

w = actual weight at A (share of the whole fund), W = normalized target,
R_p / B_p = segment / pillar-benchmark return, R_B = policy return. Summed over
segments the three effects equal R_portfolio - R_B exactly.

A segment outside the policy (no target: cash, unassigned names, or a pillar
whose benchmark has no history for the period) is benchmarked against itself,
so its whole relative return is allocation: w x (R_p - R_B). For cash that is
exactly the cash drag. Rows are derived and rebuilt whole; multi-period
views sum effects arithmetically and show the compounding residual separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.ingest.normalize import CASH_TICKER
from app.models import AttributionPeriod, HoldingSnapshot, Security, Snapshot
from app.performance import _price_near, _series, component_return, load_policy

ZERO = Decimal("0")
CASH_SEGMENT = "Cash"
UNASSIGNED_SEGMENT = "— unassigned —"


@dataclass
class Segment:
    name: str
    port_weight: Decimal
    bench_weight: Decimal
    port_return: Decimal | None
    bench_return: Decimal | None
    allocation: Decimal
    selection: Decimal
    interaction: Decimal

    @property
    def total(self) -> Decimal:
        return self.allocation + self.selection + self.interaction


def compute_period(session: Session, a: Snapshot, b: Snapshot, hist) -> tuple[list[Segment], list[str]]:
    """Segments for A -> B, plus tickers left out for lack of prices."""
    policy = load_policy(session, snapshot_id=a.id)
    if not policy.ok:
        return [], []
    a_iso, b_iso = a.as_of.isoformat(), b.as_of.isoformat()

    secs = {s.ticker: s for s in session.execute(select(Security)).scalars().all()}
    holdings = session.execute(
        select(HoldingSnapshot).where(HoldingSnapshot.snapshot_id == a.id)).scalars().all()

    pid_name = {c.pillar_id: c.name for c in policy.components}
    v0: dict[str, Decimal] = {}
    v1: dict[str, Decimal] = {}
    names_by_pid: dict[str, list[str]] = {}
    unpriced: list[str] = []
    for h in holdings:
        if h.ticker == CASH_TICKER:
            v0[CASH_SEGMENT] = v0.get(CASH_SEGMENT, ZERO) + Decimal(h.units)
            v1[CASH_SEGMENT] = v1.get(CASH_SEGMENT, ZERO) + Decimal(h.units)
            continue
        s = hist(h.ticker)
        p0, p1 = _price_near(s, a_iso), _price_near(s, b_iso)
        if p0 is None or p1 is None:
            unpriced.append(h.ticker)
            continue
        sec = secs.get(h.ticker)
        pid = sec.pillar_id if sec else None
        if pid:
            names_by_pid.setdefault(pid, []).append(h.ticker)
        seg = pid_name.get(pid) or (sec.pillar if sec and sec.pillar else UNASSIGNED_SEGMENT)
        v0[seg] = v0.get(seg, ZERO) + Decimal(h.units) * p0
        v1[seg] = v1.get(seg, ZERO) + Decimal(h.units) * p1

    total = sum(v0.values(), ZERO)
    if not total:
        return [], unpriced

    # Pillar benchmark returns; the policy renormalizes over pillars that have one.
    bench_ret: dict[str, Decimal] = {}
    for c in policy.components:
        r = component_return(c, a_iso, b_iso, hist,
                             basket=None if c.etf else (names_by_pid.get(c.pillar_id) or c.basket))
        if r is not None:
            bench_ret[c.name] = r
    covered = sum((c.weight for c in policy.components if c.name in bench_ret), ZERO)
    if not covered:
        return [], unpriced
    W = {c.name: c.weight / covered for c in policy.components if c.name in bench_ret}
    r_b = sum((W[n] * bench_ret[n] for n in W), ZERO)

    segments: list[Segment] = []
    for name in sorted(set(v0) | set(W)):
        w = v0.get(name, ZERO) / total
        Wp = W.get(name, ZERO)
        rp = (v1[name] / v0[name] - 1) if v0.get(name) else None
        bp = bench_ret.get(name) if name in W else None
        if bp is not None:
            b_eff = bp
            r_eff = rp if rp is not None else bp   # a pillar we don't hold earns its benchmark
        else:
            r_eff = rp if rp is not None else r_b
            b_eff = r_eff                          # out of policy: pure allocation
        segments.append(Segment(
            name=name, port_weight=w, bench_weight=Wp, port_return=rp, bench_return=bp,
            allocation=(w - Wp) * (b_eff - r_b),
            selection=Wp * (r_eff - b_eff),
            interaction=(w - Wp) * (r_eff - b_eff),
        ))
    return segments, unpriced


def rebuild_attribution(session: Session, *, commit: bool = False) -> int:
    """Recompute every consecutive-pair period from scratch. Returns period count.
    Same full-rebuild rationale as the diff engine: a handful of snapshots, and
    correct when one lands mid-series or a target or price history changes."""
    session.execute(delete(AttributionPeriod))
    snaps = session.execute(select(Snapshot).order_by(Snapshot.as_of)).scalars().all()
    cache: dict[str, list] = {}

    def hist(sym):
        if sym not in cache:
            cache[sym] = _series(session, sym)
        return cache[sym]

    n = 0
    for a, b in zip(snaps, snaps[1:]):
        segments, _ = compute_period(session, a, b, hist)
        for sg in segments:
            session.add(AttributionPeriod(
                from_snapshot=a.id, to_snapshot=b.id, pillar=sg.name,
                port_weight=sg.port_weight, bench_weight=sg.bench_weight,
                port_return=sg.port_return, bench_return=sg.bench_return,
                allocation=sg.allocation, selection=sg.selection, interaction=sg.interaction,
            ))
        n += bool(segments)
    if commit:
        session.commit()
    return n


def safe_rebuild(session: Session) -> None:
    """Best-effort rebuild inside a caller's transaction: attribution is derived,
    so a failure here must never block the snapshot/trade/target write."""
    try:
        with session.begin_nested():
            rebuild_attribution(session)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


@dataclass
class Period:
    from_id: int
    to_id: int
    from_date: date
    to_date: date
    segments: list[Segment] = field(default_factory=list)

    @property
    def days(self) -> int:
        return (self.to_date - self.from_date).days

    @property
    def port_return(self) -> Decimal:
        return sum((s.port_weight * (s.port_return if s.port_return is not None else
                    (s.bench_return or ZERO)) for s in self.segments), ZERO)

    @property
    def bench_return(self) -> Decimal:
        # R_B = sum W x B over covered pillars; segments without B carry W = 0.
        return sum((s.bench_weight * s.bench_return for s in self.segments
                    if s.bench_return is not None), ZERO)

    @property
    def allocation(self) -> Decimal:
        return sum((s.allocation for s in self.segments), ZERO)

    @property
    def selection(self) -> Decimal:
        return sum((s.selection for s in self.segments), ZERO)

    @property
    def interaction(self) -> Decimal:
        return sum((s.interaction for s in self.segments), ZERO)

    @property
    def excess(self) -> Decimal:
        return self.allocation + self.selection + self.interaction


@dataclass
class Rollup:
    key: str
    label: str
    periods: list[Period]
    by_segment: list[dict] = field(default_factory=list)
    allocation: Decimal = ZERO
    selection: Decimal = ZERO
    interaction: Decimal = ZERO
    port_return: Decimal = ZERO     # compounded
    bench_return: Decimal = ZERO    # compounded

    @property
    def excess_sum(self) -> Decimal:
        return self.allocation + self.selection + self.interaction

    @property
    def linking_residual(self) -> Decimal:
        """Compounded excess minus the arithmetic sum of period effects."""
        return (self.port_return - self.bench_return) - self.excess_sum


def load_periods(session: Session) -> list[Period]:
    snaps = {s.id: s for s in session.execute(select(Snapshot)).scalars().all()}
    rows = session.execute(select(AttributionPeriod).order_by(AttributionPeriod.id)).scalars().all()
    by_pair: dict[tuple[int, int], Period] = {}
    for r in rows:
        a, b = snaps.get(r.from_snapshot), snaps.get(r.to_snapshot)
        if a is None or b is None:
            continue
        p = by_pair.setdefault((a.id, b.id), Period(a.id, b.id, a.as_of, b.as_of))
        p.segments.append(Segment(
            name=r.pillar, port_weight=Decimal(r.port_weight), bench_weight=Decimal(r.bench_weight),
            port_return=Decimal(r.port_return) if r.port_return is not None else None,
            bench_return=Decimal(r.bench_return) if r.bench_return is not None else None,
            allocation=Decimal(r.allocation), selection=Decimal(r.selection),
            interaction=Decimal(r.interaction)))
    periods = sorted(by_pair.values(), key=lambda p: p.to_date)
    for p in periods:
        p.segments.sort(key=lambda s: -abs(s.total))
    return periods


def rollup(key: str, label: str, periods: list[Period]) -> Rollup:
    ru = Rollup(key=key, label=label, periods=periods)
    agg: dict[str, dict] = {}
    gp = gb = Decimal("1")
    for p in periods:
        gp *= 1 + p.port_return
        gb *= 1 + p.bench_return
        for s in p.segments:
            a = agg.setdefault(s.name, {"name": s.name, "allocation": ZERO, "selection": ZERO,
                                        "interaction": ZERO, "weight_sum": ZERO, "target_sum": ZERO})
            a["allocation"] += s.allocation
            a["selection"] += s.selection
            a["interaction"] += s.interaction
            a["weight_sum"] += s.port_weight
            a["target_sum"] += s.bench_weight
    n = len(periods) or 1
    for a in agg.values():
        a["total"] = a["allocation"] + a["selection"] + a["interaction"]
        a["avg_weight"] = a["weight_sum"] / n
        a["avg_target"] = a["target_sum"] / n
    ru.by_segment = sorted(agg.values(), key=lambda a: -abs(a["total"]))
    ru.allocation = sum((p.allocation for p in periods), ZERO)
    ru.selection = sum((p.selection for p in periods), ZERO)
    ru.interaction = sum((p.interaction for p in periods), ZERO)
    ru.port_return, ru.bench_return = gp - 1, gb - 1
    return ru


def rollups(periods: list[Period], today: date | None = None) -> list[Rollup]:
    """Trailing 12 months first, then each calendar quarter (newest first). A
    period belongs to the quarter its END date falls in."""
    today = today or date.today()
    out = [rollup("t12m", "Trailing 12 months",
                  [p for p in periods if p.to_date > today - timedelta(days=365)])]
    quarters: dict[tuple[int, int], list[Period]] = {}
    for p in periods:
        quarters.setdefault((p.to_date.year, (p.to_date.month - 1) // 3 + 1), []).append(p)
    for (y, q) in sorted(quarters, reverse=True):
        out.append(rollup(f"{y}q{q}", f"Q{q} {y}", quarters[(y, q)]))
    return out
