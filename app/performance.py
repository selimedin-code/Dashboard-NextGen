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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.fundamentals import _store_history
from app.models import NavPoint, Pillar, PriceHistoryCache
from app.positions import build_positions
from app.providers.fmp import FMPClient

ZERO = Decimal("0")

BENCH = {"QQQ": "#3167D6", "BLEND": "#B8912F", "SPY": "#8C8C8C"}   # display colors
BENCH_SYMBOLS = ["QQQ", "SMH", "SPY"]
FUND_COLOR = "#141414"

# Windows for the pillar-vs-ETF comparison. Stock histories hold ~20 months
# (fundamentals.HISTORY_KEEP), so nothing longer than 1Y is reliable.
PILLAR_PERIODS = {"ytd": "YTD", "3m": "3M", "1y": "1Y"}

CHART_W = 900
CHART_H = 380
PAD_L, PAD_R, PAD_T, PAD_B = 42, 10, 12, 24   # room for axis labels


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
        if nav is None:  # tolerate variants like "Price_USD" / "NAV per share"
            nav = next((v for k, v in norm.items() if "nav" in k or "price" in k), None)
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
    date_i = nav_i = price_i = None
    out: list[tuple[date, Decimal]] = []
    for row in ws.iter_rows(values_only=True):
        if date_i is None or (nav_i is None and price_i is None):
            for i, c in enumerate(row):
                s = str(c or "").strip().lower()
                if s == "date":
                    date_i = i
                elif "nav" in s and nav_i is None:
                    nav_i = i
                elif "price" in s and price_i is None:  # e.g. "Price_USD"
                    price_i = i
            continue
        if nav_i is None:
            nav_i = price_i  # a NAV-named column wins when both exist
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
    symbols = BENCH_SYMBOLS + [s for s in pillar_etf_symbols(session) if s not in BENCH_SYMBOLS]
    try:
        for sym in symbols:
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


def _pillar_etf(p: Pillar | None) -> tuple[str | None, bool]:
    """(benchmark symbol, is_alt) — the primary ETF, else the alternate (P03 has
    no primary; SKYY stands in, with its caveat shown)."""
    if p is None:
        return None, False
    if p.primary_etf:
        return p.primary_etf.strip().upper(), False
    if p.alt_etf:
        return p.alt_etf.strip().upper(), True
    return None, False


def pillar_etf_symbols(session: Session) -> list[str]:
    syms = {_pillar_etf(p)[0] for p in session.execute(select(Pillar)).scalars().all()}
    return sorted(s for s in syms if s)


def period_start(key: str, today: date) -> date:
    """Start of a comparison window; returns are measured from the last close on
    or before this date (so YTD starts from the prior year's final close)."""
    if key == "3m":
        return today - timedelta(days=91)
    if key == "1y":
        return today - timedelta(days=365)
    return date(today.year - 1, 12, 31)


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
    kpis: dict = field(default_factory=dict)
    pillar_period: str = "ytd"
    pillar_period_start: date | None = None
    pillar_period_end: str | None = None


def build_performance(session: Session, pillar_period: str = "ytd",
                      today: date | None = None) -> PerformanceView:
    navs = list_nav_points(session)
    qqq, smh, spy = _series(session, "QQQ"), _series(session, "SMH"), _series(session, "SPY")
    blend = _blend_series(qqq, smh)
    benchmarks_cached = bool(qqq and spy)

    view = PerformanceView(
        nav_points=navs, has_fund_return=len(navs) >= 2,
        base_date=navs[0].as_of if navs else None, window_end=None,
        fund_return=None, benchmarks_cached=benchmarks_cached,
    )
    if pillar_period not in PILLAR_PERIODS:
        pillar_period = "ytd"
    view.pillar_period = pillar_period

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

        # Graph: fund vs QQQ and SPY (QQQ/SMH blend intentionally left off the chart).
        view.chart = _build_chart(navs, {"QQQ": qqq, "SPY": spy}, base_iso)
        view.kpis = compute_nav_kpis(navs, ref=qqq, ref_label="QQQ")

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
        members: dict[str, list] = {}
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
            members.setdefault(key, []).append(r)
        contribs.sort(key=lambda c: c.contribution, reverse=True)
        view.contributors = contribs
        view.by_pillar = sorted(
            ({"pillar": k, "contribution": v[0],
              "return_vs_cost": (v[1] / v[2] - 1) if v[2] else None} for k, v in pillar_agg.items()),
            key=lambda d: d["contribution"], reverse=True,
        )
        _add_pillar_benchmarks(session, view, members, today or date.today())

    return view


def _add_pillar_benchmarks(session: Session, view: PerformanceView,
                           members: dict[str, list], today: date) -> None:
    """Per pillar, over the same window: the pillar's CURRENT holdings (buy-and-
    hold of today's units) vs its benchmark ETF. Return-vs-cost has no fixed
    window, so this like-for-like pair is what the "vs ETF" column compares.

    Both legs end at the ETF's last cached close so the dates line up; a name is
    left out (and counted in `covered`) if its history doesn't span the window."""
    start = period_start(view.pillar_period, today)
    start_iso = start.isoformat()
    view.pillar_period_start = start
    meta = {p.name: p for p in session.execute(select(Pillar)).scalars().all()}
    hist_cache: dict[str, list] = {}

    def hist(sym: str) -> list:
        if sym not in hist_cache:
            hist_cache[sym] = _series(session, sym)
        return hist_cache[sym]

    ends = []
    for row in view.by_pillar:
        p = meta.get(row["pillar"])
        etf, is_alt = _pillar_etf(p)
        row.update(etf=etf, etf_is_alt=is_alt, caveat=(p.caveat if p else None),
                   etf_return=None, pillar_return=None, excess=None,
                   covered=0, total=len(members.get(row["pillar"], [])))
        etf_series = hist(etf) if etf else []
        end_iso = etf_series[-1][0] if etf_series else today.isoformat()
        if etf_series:
            e0 = _value_at(etf_series, start_iso, mode="before")
            e1 = etf_series[-1][1]
            if e0:
                row["etf_return"] = e1 / e0 - 1
                ends.append(end_iso)

        v0 = v1 = ZERO
        for r in members.get(row["pillar"], []):
            s = hist(r.ticker)
            c0 = _value_at(s, start_iso, mode="before")
            c1 = _value_at(s, end_iso, mode="before")
            if c0 and c1:
                v0 += r.units * c0
                v1 += r.units * c1
                row["covered"] += 1
        if v0:
            row["pillar_return"] = v1 / v0 - 1
        if row["pillar_return"] is not None and row["etf_return"] is not None:
            row["excess"] = row["pillar_return"] - row["etf_return"]
    view.pillar_period_end = max(ends) if ends else None


def compute_nav_kpis(navs: list[NavPoint], ref: list | None = None,
                     ref_label: str = "QQQ") -> dict:
    """Period returns from the daily NAV series: YTD, QTD, MTD, plus the current
    year broken out by month and by quarter. Each period return is
    end-of-period NAV / end-of-prior-period NAV − 1 (the current period uses the
    latest NAV, so its by-month/by-quarter cell equals MTD/QTD).

    `ref` (a benchmark daily series, e.g. QQQ) is compared over the SAME calendar
    windows — its value on each NAV period-boundary date — so the reference is
    like-for-like. Each period returns {"fund": r, "ref": r}."""
    import calendar

    if len(navs) < 2:
        return {}

    by_month: dict[tuple[int, int], Decimal] = {}
    by_quarter: dict[tuple[int, int], Decimal] = {}
    by_year: dict[int, Decimal] = {}
    dmon: dict[tuple[int, int], str] = {}       # period-end DATES (iso) for the ref
    dqtr: dict[tuple[int, int], str] = {}
    dyr: dict[int, str] = {}
    for p in navs:                      # ascending → last write per period wins
        y, m = p.as_of.year, p.as_of.month
        q = (m - 1) // 3 + 1
        nav = Decimal(p.nav_per_share)
        iso = p.as_of.isoformat()
        by_month[(y, m)] = nav; dmon[(y, m)] = iso
        by_quarter[(y, q)] = nav; dqtr[(y, q)] = iso
        by_year[y] = nav; dyr[y] = iso

    latest = Decimal(navs[-1].nav_per_share)
    latest_iso = navs[-1].as_of.isoformat()
    Y, M = navs[-1].as_of.year, navs[-1].as_of.month
    Q = (M - 1) // 3 + 1

    def fund_ret(base):
        return (latest / base - 1) if base else None

    def ref_ret(start_iso, end_iso):
        if not ref or not start_iso or not end_iso:
            return None
        v0 = _value_at(ref, start_iso, mode="before")
        v1 = _value_at(ref, end_iso, mode="before")
        return (v1 / v0 - 1) if (v0 and v1) else None

    def prev_month(y, m):
        return (y, m - 1) if m > 1 else (y - 1, 12)

    def prev_quarter(y, q):
        return (y, q - 1) if q > 1 else (y - 1, 4)

    pm, pq = prev_month(Y, M), prev_quarter(Y, Q)
    kpis = {
        "year": Y, "ref_label": ref_label,
        "ytd": {"fund": fund_ret(by_year.get(Y - 1)), "ref": ref_ret(dyr.get(Y - 1), latest_iso)},
        "qtd": {"fund": fund_ret(by_quarter.get(pq)), "ref": ref_ret(dqtr.get(pq), latest_iso)},
        "mtd": {"fund": fund_ret(by_month.get(pm)), "ref": ref_ret(dmon.get(pm), latest_iso)},
        "by_month": [], "by_quarter": [], "by_year": [],
    }
    for m in sorted(mm for (yy, mm) in by_month if yy == Y):
        pmm = prev_month(Y, m)
        base = by_month.get(pmm)
        kpis["by_month"].append((
            calendar.month_abbr[m],
            (by_month[(Y, m)] / base - 1) if base else None,
            ref_ret(dmon.get(pmm), dmon[(Y, m)]),
        ))
    for q in sorted(qq for (yy, qq) in by_quarter if yy == Y):
        pqq = prev_quarter(Y, q)
        base = by_quarter.get(pqq)
        kpis["by_quarter"].append((
            f"Q{q}",
            (by_quarter[(Y, q)] / base - 1) if base else None,
            ref_ret(dqtr.get(pqq), dqtr[(Y, q)]),
        ))

    # Prior full calendar years. The inception year is measured from the first NAV
    # (the fund did not exist for the whole calendar year).
    first_nav = Decimal(navs[0].nav_per_share)
    first_iso = navs[0].as_of.isoformat()
    for y in sorted(yy for yy in by_year if yy < Y):
        if (y - 1) in by_year:
            base, base_iso, incep = by_year[y - 1], dyr[y - 1], False
        else:
            base, base_iso, incep = first_nav, first_iso, True
        kpis["by_year"].append((
            str(y),
            (by_year[y] / base - 1) if base else None,
            ref_ret(base_iso, dyr[y]),
            incep,
        ))
    return kpis


def _latest_date(*serieses) -> date | None:
    last = None
    for s in serieses:
        if s:
            d = date.fromisoformat(s[-1][0])
            last = d if last is None or d > last else last
    return last


def _build_chart(navs: list[NavPoint], benches: dict, base_iso: str) -> dict:
    import math

    base_date = date.fromisoformat(base_iso)
    base_ord = base_date.toordinal()
    ends = [date.fromisoformat(s[-1][0]).toordinal() for s in benches.values() if s]
    ends.append(navs[-1].as_of.toordinal())
    max_ord = max(ends)
    end_date = date.fromordinal(max_ord)
    span_days = max(1, max_ord - base_ord)

    plot_w = CHART_W - PAD_L - PAD_R
    plot_h = CHART_H - PAD_T - PAD_B

    def x_of(o) -> float:
        if not isinstance(o, int):
            o = o.toordinal()
        return PAD_L + plot_w * ((o - base_ord) / span_days)

    # Rebased return series (value/base − 1), collect the y-range first.
    plotted: dict[str, list[tuple[float, Decimal]]] = {}
    for key, series in benches.items():
        v0 = _value_at(series, base_iso, mode="after")
        if not v0:
            continue
        pts = [(x_of(date.fromisoformat(d)), (c / v0) - 1) for d, c in series if d >= base_iso]
        if pts:
            plotted[key] = pts
    nav0 = Decimal(navs[0].nav_per_share)
    nav_pts = [(x_of(p.as_of), (Decimal(p.nav_per_share) / nav0) - 1) for p in navs]
    plotted["FUND"] = nav_pts

    all_rets = [r for pts in plotted.values() for _, r in pts] or [ZERO]
    lo, hi = min(all_rets), max(all_rets)
    padding = (hi - lo) * Decimal("0.06") or Decimal("0.02")
    lo_p, hi_p = lo - padding, hi + padding
    span = (hi_p - lo_p) or Decimal("1")

    def y_of(ret: Decimal) -> float:
        return PAD_T + plot_h * (1 - float((ret - lo_p) / span))

    # Horizontal % gridlines at a readable step.
    rng = float(hi - lo)
    step = 0.20 if rng > 0.6 else (0.10 if rng > 0.25 else 0.05)
    ygrid = []
    lvl = math.ceil(float(lo_p) / step) * step
    while lvl <= float(hi_p) + 1e-9:
        ygrid.append({"y": round(y_of(Decimal(str(round(lvl, 6)))), 1),
                      "label": f"{lvl * 100:+.0f}%", "zero": abs(lvl) < 1e-9})
        lvl += step

    # Vertical gridlines at quarter/year boundaries (year = major, labelled).
    xgrid = []
    for year in range(base_date.year, end_date.year + 1):
        for q in range(1, 5):
            qd = date(year, (q - 1) * 3 + 1, 1)
            if base_date <= qd <= end_date:
                xgrid.append({"x": round(x_of(qd), 1),
                              "label": str(year) if qd.month == 1 else f"Q{q}",
                              "major": qd.month == 1})

    lines = []
    colors = {"FUND": FUND_COLOR, **BENCH}
    labels = {"FUND": "Fund NAV", "QQQ": "QQQ", "BLEND": "QQQ/SMH", "SPY": "SPY"}
    sparse_nav = len(nav_pts) <= 24
    for key in ("QQQ", "BLEND", "SPY", "FUND"):
        pts = plotted.get(key)
        if not pts:
            continue
        poly = " ".join(f"{x:.1f},{y_of(r):.1f}" for x, r in pts)
        lines.append({
            "name": labels[key], "color": colors[key], "points": poly,
            "is_fund": key == "FUND",
            "markers": [(round(x, 1), y_of(r)) for x, r in pts] if (key == "FUND" and sparse_nav) else [],
        })

    return {"w": CHART_W, "h": CHART_H,
            "pad": {"l": PAD_L, "r": PAD_R, "t": PAD_T, "b": PAD_B},
            "lines": lines, "ygrid": ygrid, "xgrid": xgrid, "lo": lo, "hi": hi}
