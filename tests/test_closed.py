"""Closed positions: derived from snapshot history at read time."""

from datetime import date
from decimal import Decimal

from app import trades as tr
from app.closed import build_closed_positions
from app.ingest.commit import commit_snapshot
from app.ingest.normalize import CASH_TICKER
from app.ingest.parser import ParsedHolding
from app.models import PriceHistoryCache

D0 = date(2026, 9, 1)
D1 = date(2026, 10, 1)


def _h(ticker, units, cost, cash=False):
    return ParsedHolding(raw_ticker=CASH_TICKER if cash else f"{ticker} US", ticker=ticker,
                         exchange=None if cash else "US", units=Decimal(units),
                         avg_cost=Decimal(cost), is_cash=cash)


def _upload(session, as_of, holdings):
    return commit_snapshot(holdings=holdings, as_of=as_of, filename="f.xlsx", notes=None,
                           pillar_assignments=None, session=session)


def test_no_closed_with_single_snapshot(session):
    _upload(session, D0, [_h("NVDA", 100, 100)])
    assert build_closed_positions(session) == []


def test_close_via_custodian_diff_uses_history_estimate(session):
    _upload(session, D0, [_h("NVDA", 100, 100), _h("SOUN", 500, 8)])
    session.add(PriceHistoryCache(ticker="SOUN",
                                  series=[["2026-09-28", 4.5], ["2026-10-01", 4.0]]))
    _upload(session, D1, [_h("NVDA", 100, 100)])
    closed = build_closed_positions(session)
    assert [c.ticker for c in closed] == ["SOUN"]
    c = closed[0]
    assert c.first_seen == D0 and c.closed_on == D1
    assert c.last_units == Decimal(500) and c.avg_cost == Decimal(8)
    assert c.exit_price == Decimal("4.0") and c.exit_source == "close est."
    assert c.realized_pl == Decimal("-2000")   # (4 - 8) * 500
    assert c.realized_pct == Decimal("-0.5")


def test_close_via_manual_trade_uses_trade_price(session):
    _upload(session, D0, [_h("SOUN", "5526.67", 8), _h(CASH_TICKER, 1000, 1, cash=True)])
    tr.record_trade(session, "SOUN", side="SELL", units_raw="all", price_raw="4.0208",
                    trade_date=date(2026, 11, 6))
    closed = build_closed_positions(session)
    assert [c.ticker for c in closed] == ["SOUN"]
    c = closed[0]
    assert c.closed_on == date(2026, 11, 6)
    assert c.exit_price == Decimal("4.0208") and c.exit_source == "trade"
    assert c.realized_pct == Decimal("4.0208") / 8 - 1


def test_missing_exit_price_degrades_gracefully(session):
    _upload(session, D0, [_h("XXXX", 10, 50)])
    _upload(session, D1, [_h("NVDA", 1, 1)])
    c = build_closed_positions(session)[0]
    assert c.ticker == "XXXX"
    assert c.exit_price is None and c.realized_pl is None


def test_cash_and_still_held_names_excluded(session):
    _upload(session, D0, [_h("NVDA", 100, 100), _h(CASH_TICKER, 1000, 1, cash=True)])
    _upload(session, D1, [_h("NVDA", 100, 100)])   # cash gone, NVDA still held
    assert build_closed_positions(session) == []
