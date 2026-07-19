"""Position metrics and price handling. No live API — the cache is seeded directly."""

from datetime import date, datetime, timezone
from decimal import Decimal

from app.ingest.commit import commit_snapshot
from app.ingest.parser import ParsedHolding
from app.models import QuoteCache
from app.positions import build_positions
from app.prices import resolve_fmp_symbol


def _h(ticker, units, cost, exch="US", isin=None, cash=False):
    return ParsedHolding(
        raw_ticker=f"{ticker} {exch}" if exch else ticker, ticker=ticker, exchange=exch,
        units=Decimal(str(units)), avg_cost=Decimal(str(cost)), isin=isin, is_cash=cash,
    )


def _seed_quote(session, ticker, price, prev, ok=True, err=None, day=None):
    session.add(QuoteCache(
        ticker=ticker, fmp_symbol=ticker,
        price=Decimal(str(price)) if price is not None else None,
        prev_close=Decimal(str(prev)) if prev is not None else None,
        day_change_pct=Decimal(str(day)) if day is not None else None,
        currency="USD", name=ticker, ok=ok, error=err,
        fetched_at=datetime.now(timezone.utc),
    ))


def test_symbol_mapping():
    assert resolve_fmp_symbol("NVDA", "US") == "NVDA"
    assert resolve_fmp_symbol("PLTR", None) == "PLTR"
    assert resolve_fmp_symbol("IFX", "GR") == "IFNNY"      # ADR override
    assert resolve_fmp_symbol("XYZ", "GR") is None          # non-US, no override


def test_position_metrics(session):
    commit_snapshot(holdings=[
        _h("NVDA", 100, 50, isin="US67066G1040"),
        _h("AMD", 200, 100, isin="US0079031078"),
        _h("USD Cash", 1000, 1, exch=None, cash=True),
    ], as_of=date(2026, 7, 1), filename="f", notes=None, pillar_assignments=None, session=session)

    _seed_quote(session, "NVDA", price=60, prev=55, day=9.09)   # +10 vs cost, +5 on day
    _seed_quote(session, "AMD", price=90, prev=95, day=-5.26)   # -10 vs cost, -5 on day
    session.commit()

    view = build_positions(session)
    rows = {r.ticker: r for r in view.rows}

    # NVDA: mv 6000, unrl (60-50)*100=1000, day (60-55)*100=500
    assert rows["NVDA"].market_value == Decimal("6000")
    assert rows["NVDA"].unrl_pl == Decimal("1000")
    assert rows["NVDA"].day_pl == Decimal("500")
    # AMD: mv 18000, unrl (90-100)*200=-2000, day (90-95)*200=-1000
    assert rows["AMD"].market_value == Decimal("18000")
    assert rows["AMD"].unrl_pl == Decimal("-2000")
    assert rows["AMD"].day_pl == Decimal("-1000")
    # cash carried at 1
    assert rows["USD Cash"].market_value == Decimal("1000")

    h = view.header
    # total = 6000 + 18000 + 1000 = 25000
    assert h["total_value"] == Decimal("25000")
    assert h["cash_value"] == Decimal("1000")
    # day pl = 500 - 1000 = -500
    assert h["day_pl"] == Decimal("-500")
    # unrealized = 1000 - 2000 = -1000
    assert h["unrl_pl"] == Decimal("-1000")
    assert h["unpriced_count"] == 0

    # weights sum to 1 across priced rows (incl cash)
    total_w = sum(r.weight for r in view.rows if r.weight is not None)
    assert abs(total_w - Decimal("1")) < Decimal("0.0001")


def test_unpriced_is_flagged_not_zero(session):
    commit_snapshot(holdings=[
        _h("NVDA", 100, 50, isin="US67066G1040"),
        _h("IFX", 10, 30, exch="GR", isin="DE0006231004"),
    ], as_of=date(2026, 7, 1), filename="f", notes=None, pillar_assignments=None, session=session)

    _seed_quote(session, "NVDA", price=60, prev=60)
    _seed_quote(session, "IFX", price=None, prev=None, ok=False, err="no live price: GR listing")
    session.commit()

    view = build_positions(session)
    rows = {r.ticker: r for r in view.rows}
    assert rows["IFX"].priced is False
    assert rows["IFX"].market_value is None            # not silently zero
    assert "IFX" in view.unpriced
    assert view.header["unpriced_count"] == 1
    # totals exclude the unpriced name: only NVDA (6000) counts
    assert view.header["total_value"] == Decimal("6000")


def test_no_snapshot_returns_none(session):
    assert build_positions(session) is None
