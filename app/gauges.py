"""Macro gauges (Risk page, Zone 1): fetch, score, store, and present.

Three composites — stress, risk appetite, credit stress (US, ex-Turkey) —
each 0–100, from FRED series and FMP quotes. Definitions and breakpoints live
in app/risk_config.py; this module only executes them.

Same architecture as prices: `refresh_gauges` is the ONLY thing that touches
the network; `build_gauges` reads the stored history. A failed input degrades
its gauge visibly (weights renormalize, the miss is listed) — never silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import GaugePoint
from app.providers.fmp import FMPClient
from app.providers.fred import FredClient
from app.risk_config import GAUGE_STALE_DAYS, GAUGES, GaugeComponent

SPARK_POINTS = 15
SPARK_W, SPARK_H = 120, 28


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def interp_score(v: Decimal, p0: Decimal, p50: Decimal, p100: Decimal) -> Decimal:
    """Piecewise-linear 0–100 through (p0, p50, p100), clamped. The triple may
    ascend or descend; a degenerate triple returns 50 rather than dividing by 0."""
    if p0 == p50 or p50 == p100:
        return Decimal("50")
    ascending = p0 < p100
    lo, hi = (p0, p100) if ascending else (p100, p0)
    if v <= lo:
        return Decimal("0") if ascending else Decimal("100")
    if v >= hi:
        return Decimal("100") if ascending else Decimal("0")
    if ascending:
        if v <= p50:
            return 50 * (v - p0) / (p50 - p0)
        return 50 + 50 * (v - p50) / (p100 - p50)
    # descending: p0 > p50 > p100
    if v >= p50:
        return 50 * (p0 - v) / (p0 - p50)
    return 50 + 50 * (p50 - v) / (p50 - p100)


def band_for(gauge_key: str, value: Decimal) -> tuple[str, str]:
    """(label, css_status) from the gauge's bands, evaluated top-down."""
    for min_v, label, css in GAUGES[gauge_key].bands:
        if min_v is None or value >= min_v:
            return label, css
    return "?", "watch"


# ---------------------------------------------------------------------------
# Refresh (network → gauge_points)
# ---------------------------------------------------------------------------

@dataclass
class _Input:
    value: Decimal | None = None
    obs_date: date | None = None
    error: str | None = None


def _collect_inputs() -> dict[tuple, _Input]:
    """Fetch every distinct (kind, params) once across all gauges."""
    settings = get_settings()
    needed: set[tuple] = set()
    for g in GAUGES.values():
        for c in g.components:
            needed.add((c.kind, c.params))

    inputs: dict[tuple, _Input] = {k: _Input() for k in needed}

    fred_keys = [k for k in needed if k[0] == "fred"]
    if fred_keys:
        if not settings.fred_api_key:
            for k in fred_keys:
                inputs[k].error = "FRED_API_KEY not configured"
        else:
            with FredClient(settings.fred_api_key) as fred:
                for k in fred_keys:
                    series = k[1][0]
                    try:
                        fv = fred.latest(series)
                        inputs[k].value, inputs[k].obs_date = fv.value, fv.as_of
                    except Exception as exc:  # noqa: BLE001 — one series must not sink the rest
                        inputs[k].error = f"{series}: {exc}"

    fmp_keys = [k for k in needed if k[0].startswith("fmp")]
    if fmp_keys:
        if not settings.fmp_api_key:
            for k in fmp_keys:
                inputs[k].error = "FMP_API_KEY not configured"
        else:
            client = FMPClient(settings.fmp_api_key, min_interval=0.05)
            quotes: dict[str, object] = {}

            def q(symbol: str):
                if symbol not in quotes:
                    quotes[symbol] = client.get_quote(symbol)
                return quotes[symbol]

            def day_change(symbol: str) -> Decimal:
                quote = q(symbol)
                if quote.prev_close in (None, 0):
                    raise RuntimeError(f"{symbol}: no previous close")
                return quote.price / quote.prev_close - 1

            try:
                for k in fmp_keys:
                    kind, params = k
                    try:
                        if kind == "fmp_level":
                            inputs[k].value = q(params[0]).price
                        elif kind == "fmp_daychg":
                            inputs[k].value = day_change(params[0])
                        elif kind == "fmp_spread":
                            inputs[k].value = day_change(params[0]) - day_change(params[1])
                        else:
                            inputs[k].error = f"unknown kind {kind!r}"
                            continue
                        inputs[k].obs_date = date.today()
                    except Exception as exc:  # noqa: BLE001
                        inputs[k].error = str(exc)
            finally:
                client.close()

    return inputs


def refresh_gauges(session: Session) -> dict:
    """Fetch inputs, score each gauge, upsert today's gauge_points rows.
    Caller commits. Returns a summary for the redirect flash."""
    inputs = _collect_inputs()
    today = date.today()
    ok = degraded = failed = 0

    for g in GAUGES.values():
        comps: list[dict] = []
        weighted = Decimal("0")
        total_w = Decimal("0")
        misses: list[str] = []

        for c in g.components:
            inp = inputs[(c.kind, c.params)]
            if inp.value is None:
                misses.append(f"{c.label}: {inp.error or 'no data'}")
                comps.append({"key": c.key, "label": c.label, "error": inp.error or "no data"})
                continue
            s = interp_score(inp.value, c.p0, c.p50, c.p100)
            weighted += s * c.weight
            total_w += c.weight
            comps.append({
                "key": c.key, "label": c.label,
                "raw": c.fmt.format(inp.value),
                "score": float(round(s, 1)),
                "weight": float(c.weight),
                "obs_date": inp.obs_date.isoformat() if inp.obs_date else None,
            })

        row = session.get(GaugePoint, (g.key, today))
        if row is None:
            row = GaugePoint(gauge=g.key, as_of=today, status="no data")
            session.add(row)

        if total_w == 0:
            row.value = None
            row.status = "no data"
            row.error = "; ".join(misses) or "no inputs available"
            failed += 1
        else:
            value = weighted / total_w        # renormalized over what fetched
            label, _css = band_for(g.key, value)
            row.value = value
            row.status = label
            row.error = ("degraded — missing " + "; ".join(misses)) if misses else None
            ok += 1
            if misses:
                degraded += 1
        row.components = comps

    return {"ok": ok, "degraded": degraded, "failed": failed}


# ---------------------------------------------------------------------------
# Presentation (gauge_points → cards)
# ---------------------------------------------------------------------------

@dataclass
class GaugeCard:
    key: str
    label: str
    higher_means: str
    value: Decimal | None
    status_label: str            # calm / elevated / risk-on / ...
    css: str                     # ok | watch | alert
    as_of: date | None
    stale: bool
    error: str | None
    components: list[dict] = field(default_factory=list)
    spark: str = ""              # SVG polyline points over recent history


@dataclass
class GaugesView:
    cards: list[GaugeCard] = field(default_factory=list)
    any_data: bool = False


def _sparkline(values: list[Decimal]) -> str:
    if len(values) < 2:
        return ""
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = SPARK_W * i / (n - 1)
        v = min(max(v, Decimal("0")), Decimal("100"))
        y = SPARK_H - 2 - (SPARK_H - 4) * v / 100
        pts.append(f"{x:.0f},{y:.1f}")
    return " ".join(pts)


def build_gauges(session: Session) -> GaugesView:
    view = GaugesView()
    today = date.today()

    for g in GAUGES.values():
        history = session.execute(
            select(GaugePoint)
            .where(GaugePoint.gauge == g.key)
            .order_by(GaugePoint.as_of.desc())
            .limit(SPARK_POINTS)
        ).scalars().all()

        if not history:
            view.cards.append(GaugeCard(
                key=g.key, label=g.label, higher_means=g.higher_means,
                value=None, status_label="not yet fetched", css="watch",
                as_of=None, stale=False, error=None,
            ))
            continue

        latest = history[0]
        view.any_data = True
        value = Decimal(latest.value) if latest.value is not None else None
        if value is not None:
            status_label, css = band_for(g.key, value)
        else:
            status_label, css = "no data", "alert"

        comps = latest.components or []
        if isinstance(comps, str):        # defensive: some drivers hand back text
            comps = json.loads(comps)

        series = [Decimal(p.value) for p in reversed(history) if p.value is not None]
        view.cards.append(GaugeCard(
            key=g.key, label=g.label, higher_means=g.higher_means,
            value=value, status_label=status_label, css=css,
            as_of=latest.as_of,
            stale=(today - latest.as_of).days > GAUGE_STALE_DAYS,
            error=latest.error,
            components=comps,
            spark=_sparkline(series),
        ))

    return view
