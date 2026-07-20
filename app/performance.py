"""Phase 6 — performance.

Two measures, deliberately kept separate (the roadmap is firm on this):

  Fund level — from the per-share NAV series only. Total market value moves with
  subscriptions and redemptions, so it is NOT a performance measure; NAV per share
  is. Benchmarks: QQQ and a 50/50 QQQ/SMH blend (the honest beta comparison for an
  AI book) and SPY (the generalist-allocator comparison).

  Position level — contribution to the fund's return-vs-blended-cost, by position
  and by pillar. Contributions sum to the fund's unrealized return. Blended cost
  is economic return, not a verdict on the entry.

Everything reads the cache; nothing here hits the network except refresh_benchmarks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.fundamentals import _store_history
from app.models import NavPoint, PriceHistoryCache
from app.positions import build_positions
from app.providers.fmp import FMPClient

ZERO = Decimal("0")

BENCH = {"QQQ": "#3167D6", "BLEND": "#B8912F", "SPY": "#8C8C8C"}   # display colors
BENCH_SYMBOLS = ["QQQ", "SMH", "SPY"]
FUND_COLOR = "#141414"

CHART_W = 720
CHART_H = 240
PAD = 8


# ---------------------------------------------------------------------------
# NAV series management
# ---------------------------------------------------------------------------


def add_nav_point(session: Session, as_of: date, nav_per_share: Decimal,
                  total_nav: Decimal | None = None, shares: Decimal | None = None) -> NavPoint:
    row = session.execute(select(NavPoint).where(NavPoint.as_of == as_of)).scalar_one_or_none()
    if row is None:
        row = NavPoint(as_of=as_of)
        session.add(row)
    row.nav_per_share = nav_per_share
    row.total_nav = total_nav
    row.shares_outstanding = shares
    row.uploaded_at = datetime.now(timezone.utc)
    session.commit()
    return row


def delete_nav_point(session: Session, as_of: date) -> None:
    session.execute(delete(NavPoint).where(NavPoint.as_of == as_of))
    session.commit()


def list_nav_points(session: Session) -> list[NavPoint]:
    return list(session.execute(select(NavPoint).order_by(NavPoint.as_of)).scalars().all())


def parse_nav_file(filename: str, data: bytes) -> list[tuple[date, Decimal]]:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        return _parse_nav_xlsx(data)
    return parse_nav_csv(data)


def parse_nav_csv(data: bytes) -> list[tuple[date, Decimal]]:
    import csv
    import io

    text = data.decode("utf-8-sig", errors="replace")
    out: list[tuple[date, Decimal]] = []
    for row in csv.DictReader(io.StringIO(text)):
        norm = {k.strip().lower(): v for k, v in row.items() if k}
        d = norm.get("date") or norm.get("as_of")
        nav = norm.get("nav_per_share") or norm.get("nav") or norm.get("price") or norm.get("nav (usd)")
        if not d or not nav:
            continue
        try:
            out.append((date.fromisoformat(str(d)[:10]), Decimal(str(nav).replace(",", "").replace("'", ""))))
        except Exception:
            continue
    return out


def _parse_nav_xlsx(data: bytes) -> list[tuple[date, Decimal]]:
    """Read a daily NAV sheet (Date + a NAV column). Prefers a 'Daily NAV' tab."""
    import io

    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb["Daily NAV"] if "Daily NAV" in wb.sheetnames else wb.active
    date_i = nav_i = None
    out: list[tuple[date, Decimal]] = []
    for row in ws.iter_rows(values_only=True):
        if date_i is None or nav_i is None:
            for i, c in enumerate(row):
                s = str(c or "").strip().lower()
                if s == "date":
                    date_i = i
                elif "nav" in s and nav_i is None:
                    nav_i = i
            continue
        d, nav = row[date_i], row[nav_i]
        if d is None or nav is None:
            continue
        try:
            dd = d.date() if hasattr(d, "date") else date.fromisoformat(str(d)[:10])
            out.append((dd, Decimal(str(nav).replace(",", "").replace("'", ""))))
        except Exception:
            continue
    wb.close()
    return out


def bulk_add_nav_points(session: Session, points: list[tuple[date, Decimal]]) -> int:
    """Upsert many NAV points in one transaction."""
    existing = {p.as_of: p for p in session.execute(select(NavPoint)).scalars().all()}
    now = datetime.now(timezone.utc)
    for d, nav in points:
        row = existing.get(d)
        if row is None:
            row = NavPoint(as_of=d)
            session.add(row)
            existing[d] = row
        row.nav_per_share = nav
        row.uploaded_at = now
    session.commit()
    return len(points)


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def refresh_benchmarks(session: Session, *, client: FMPClient | None = None) -> dict:
    owns = client is None
    if owns:
        key = get_settings().fmp_api_key
        if not key:
            raise RuntimeError("FMP_API_KEY is not set.")
        client = FMPClient(key, min_interval=0.05)
    status: dict[str, str] = {}
    try:
        for sym in BENCH_SYMBOLS:
            try:
                hist = client.request("historical-price-eod/light", symbol=sym)
                # Keep enough history to span the NAV series back to inception.
                _store_history(session, sym, hist or [], keep=1200)
                status[sym] = "ok"
            except Exception as exc:  # noqa: BLE001
                status[sym] = f"error: {exc}"
    finally:
        if owns:
            client.close()
    session.commit()
    return status


def _series(session: Session, ticker: str) -> list[tuple[str, Decimal]]:
    row = session.get(PriceHistoryCache, ticker)
    if row is None:
        return []
    return [(d, Decimal(c)) for d, c in row.series]


def _value_at(series: list[tuple[str, Decimal]], iso: str, *, mode: str) -> Decimal | None:
    """Value on the nearest date; mode 'after' picks the first >= iso, 'before' the last <= iso."""
    if not series:
        return None
    if mode == "after":
        for d, c in series:
            if d >= iso:
                return c
        return None
    prev = None
    for d, c in series:
        if d <= iso:
            prev = c
        else:
            break
    return prev


def _blend_series(qqq: list, smh: list) -> list[tuple[str, Decimal]]:
    """50/50 QQQ/SMH, rebased daily on the intersection of dates."""
    smh_map = dict(smh)
    q0 = qqq[0][1] if qqq else None
    s0 = smh[0][1] if smh else None
    if not q0 or not s0:
        return []
    out = []
    for d, qc in qqq:
        sc = smh_map.get(d)
        if sc is None:
            continue
        idx = (qc / q0) * Decimal("0.5") + (sc / s0) * Decimal("0.5")
        out.append((d, idx))
    return out


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


@dataclass
class Series:
    name: str
    color: str
    points: str
    total_return: Decimal | None
    dashed: bool = False


@dataclass
class Contributor:
    ticker: str
    pillar: str | None
    weight: Decimal
    return_vs_cost: Decimal | None
    contribution: Decimal


@dataclass
class PerformanceView:
    nav_points: list[NavPoint]
    has_fund_return: bool
    base_date: date | None
    window_end: date | None
    fund_return: Decimal | None
    returns: dict = field(default_factory=dict)      # name -> return over window
    chart: dict | None = None
    contributors: list[Contributor] = field(default_factory=list)
    by_pillar: list[dict] = field(default_factory=list)
    fund_unrl_return: Decimal | None = None
    total_cost: Decimal | None = None
    benchmarks_cached: bool = False


def build_performance(session: Session) -> PerformanceView:
    navs = list_nav_points(session)
    qqq, smh, spy = _series(session, "QQQ"), _series(session, "SMH"), _series(session, "SPY")
    blend = _blend_series(qqq, smh)
    benchmarks_cached = bool(qqq and spy)

    view = PerformanceView(
        nav_points=navs, has_fund_return=len(navs) >= 2,
        base_date=navs[0].as_of if navs else None, window_end=None,
        fund_return=None, benchmarks_cached=benchmarks_cached,
    )

    # --- fund level ---
    if navs:
        base = navs[0].as_of
        end = navs[-1].as_of if len(navs) >= 2 else _latest_date(qqq, spy) or base
        view.window_end = end
        base_iso, end_iso = base.isoformat(), end.isoformat()

        if len(navs) >= 2:
            view.fund_return = (Decimal(navs[-1].nav_per_share) / Decimal(navs[0].nav_per_share)) - 1

        named = {"QQQ": qqq, "BLEND": blend, "SPY": spy}
        for key, series in named.items():
            v0 = _value_at(series, base_iso, mode="after")
            v1 = _value_at(series, end_iso, mode="before")
            view.returns[key] = ((v1 / v0) - 1) if (v0 and v1) else None

        view.chart = _build_chart(navs, {"QQQ": qqq, "BLEND": blend, "SPY": spy}, base_iso)

    # --- position level (works with the current snapshot alone) ---
    pv = build_positions(session)
    if pv is not None:
        priced = [r for r in pv.rows if not r.is_cash and r.priced and r.market_value is not None]
        total_cost = sum((r.units * r.avg_cost for r in priced), ZERO)
        total_mv = sum((r.market_value for r in priced), ZERO)
        view.total_cost = total_cost
        view.fund_unrl_return = (total_mv / total_cost - 1) if total_cost else None

        contribs: list[Contributor] = []
        pillar_agg: dict[str, list] = {}
        for r in priced:
            unrl = (r.market_value - r.units * r.avg_cost)
            contribution = (unrl / total_cost) if total_cost else ZERO
            contribs.append(Contributor(
                ticker=r.ticker, pillar=r.pillar, weight=(r.weight or ZERO),
                return_vs_cost=r.unrl_pct, contribution=contribution,
            ))
            key = r.pillar or "— unassigned —"
            b = pillar_agg.setdefault(key, [ZERO, ZERO, ZERO])   # contribution, mv, cost
            b[0] += contribution
            b[1] += r.market_value
            b[2] += r.units * r.avg_cost
        contribs.sort(key=lambda c: c.contribution, reverse=True)
        view.contributors = contribs
        view.by_pillar = sorted(
            ({"pillar": k, "contribution": v[0],
              "return_vs_cost": (v[1] / v[2] - 1) if v[2] else None} for k, v in pillar_agg.items()),
            key=lambda d: d["contribution"], reverse=True,
        )

    return view


def _latest_date(*serieses) -> date | None:
    last = None
    for s in serieses:
        if s:
            d = date.fromisoformat(s[-1][0])
            last = d if last is None or d > last else last
    return last


def _build_chart(navs: list[NavPoint], benches: dict, base_iso: str) -> dict:
    # Time axis in ordinal days from base to the latest date across all series.
    base_ord = date.fromisoformat(base_iso).toordinal()
    ends = [date.fromisoformat(s[-1][0]).toordinal() for s in benches.values() if s]
    ends.append(navs[-1].as_of.toordinal())
    max_ord = max(ends)
    span_days = max(1, max_ord - base_ord)

    def x_of(iso_or_date) -> float:
        o = (iso_or_date if isinstance(iso_or_date, int)
             else (date.fromisoformat(iso_or_date) if isinstance(iso_or_date, str) else iso_or_date).toordinal())
        return PAD + (CHART_W - 2 * PAD) * ((o - base_ord) / span_days)

    # Rebased return series (value/base - 1), collect for y-range.
    plotted: dict[str, list[tuple[float, Decimal]]] = {}
    for key, series in benches.items():
        v0 = _value_at(series, base_iso, mode="after")
        if not v0:
            continue
        pts = [(x_of(d), (c / v0) - 1) for d, c in series if d >= base_iso]
        if pts:
            plotted[key] = pts
    nav0 = Decimal(navs[0].nav_per_share)
    nav_pts = [(x_of(p.as_of.isoformat()), (Decimal(p.nav_per_share) / nav0) - 1) for p in navs]
    plotted["FUND"] = nav_pts

    all_rets = [r for pts in plotted.values() for _, r in pts] or [ZERO]
    lo, hi = min(all_rets), max(all_rets)
    span = (hi - lo) or Decimal("1")

    def y_of(ret: Decimal) -> float:
        return PAD + (CHART_H - 2 * PAD) * (1 - float((ret - lo) / span))

    lines = []
    colors = {"FUND": FUND_COLOR, **BENCH}
    labels = {"FUND": "Fund NAV", "QQQ": "QQQ", "BLEND": "QQQ/SMH", "SPY": "SPY"}
    for key in ("QQQ", "BLEND", "SPY", "FUND"):
        pts = plotted.get(key)
        if not pts:
            continue
        poly = " ".join(f"{x:.1f},{y_of(r):.1f}" for x, r in pts)
        lines.append({
            "name": labels[key], "color": colors[key], "points": poly,
            "is_fund": key == "FUND", "markers": [(x, y_of(r)) for x, r in pts] if key == "FUND" else [],
        })
    # zero line
    zero_y = y_of(ZERO)
    return {"w": CHART_W, "h": CHART_H, "lines": lines, "zero_y": zero_y,
            "lo": lo, "hi": hi}
