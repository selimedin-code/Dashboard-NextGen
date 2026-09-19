"""Single-ticker trades: manual snapshots on top of the custodian file."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import trades as tr
from app.ingest.commit import commit_snapshot
from app.ingest.normalize import CASH_TICKER
from app.ingest.parser import ParsedHolding
from app.models import Change, HoldingSnapshot, ManualTrade, ReviewLog, Security, Snapshot
from app.positions import build_positions

D0 = date(2026, 9, 1)


def _h(ticker, units, cost, cash=False):
    return ParsedHolding(raw_ticker=CASH_TICKER if cash else f"{ticker} US", ticker=ticker,
                         exchange=None if cash else "US", units=Decimal(units),
                         avg_cost=Decimal(cost), is_cash=cash)


def _upload(session, as_of, holdings, overwrite=False):
    return commit_snapshot(holdings=holdings, as_of=as_of, filename="f.xlsx", notes=None,
                           pillar_assignments=None, session=session, overwrite=overwrite)


BASE = [_h("NVDA", 100, 100), _h("AMD", 50, 150), _h(CASH_TICKER, 20000, 1, cash=True)]


def _book(session, as_of):
    snap = session.execute(select(Snapshot).where(Snapshot.as_of == as_of)).scalar_one()
    rows = session.execute(select(HoldingSnapshot).where(HoldingSnapshot.snapshot_id == snap.id)).scalars()
    return {h.ticker: (Decimal(h.units), Decimal(h.avg_cost)) for h in rows}


def _trade(session, side, units, price="200", day=D0 + timedelta(days=1), ticker="NVDA", **kw):
    return tr.record_trade(session, ticker, side=side, units_raw=units, price_raw=price,
                           trade_date=day, **kw)


def test_buy_reblends_cost_and_moves_cash(session):
    _upload(session, D0, BASE)
    r = _trade(session, "BUY", "100", "200")
    assert r.units_before == 100 and r.units_after == 200
    assert r.avg_cost_after == Decimal("150")
    book = _book(session, D0 + timedelta(days=1))
    assert book["NVDA"] == (Decimal(200), Decimal(150))
    assert book[CASH_TICKER][0] == Decimal(0)          # 20,000 - 100 x 200
    assert book["AMD"] == (Decimal(50), Decimal(150))  # untouched
    snap = session.get(Snapshot, r.snapshot.id)
    assert snap.source == "manual"
    ch = session.execute(select(Change).where(Change.ticker == "NVDA")).scalar_one()
    assert ch.change_type == "ADD"


def test_sell_keeps_cost_sell_all_closes(session):
    _upload(session, D0, BASE)
    _trade(session, "SELL", "40", "120")
    assert _book(session, D0 + timedelta(days=1))["NVDA"] == (Decimal(60), Decimal(100))
    # A second trade the same day lands on the same manual snapshot.
    r = _trade(session, "SELL", "all", "130")
    assert r.units_after == 0 and r.avg_cost_after is None
    book = _book(session, D0 + timedelta(days=1))
    assert "NVDA" not in book
    assert book[CASH_TICKER][0] == Decimal(20000 + 40 * 120 + 60 * 130)
    assert session.execute(select(Snapshot)).scalars().all().__len__() == 2
    assert session.get(Security, "NVDA").active is False
    ch = session.execute(select(Change).where(Change.ticker == "NVDA")).scalar_one()
    assert ch.change_type == "CLOSE"


def test_open_new_name_without_cash_adjust(session):
    _upload(session, D0, BASE)
    _trade(session, "BUY", "10", "50", ticker="MU", adjust_cash=False)
    book = _book(session, D0 + timedelta(days=1))
    assert book["MU"] == (Decimal(10), Decimal(50))
    assert book[CASH_TICKER][0] == Decimal(20000)
    assert session.get(Security, "MU").first_seen == D0 + timedelta(days=1)


@pytest.mark.parametrize("kw, msg", [
    (dict(side="SELL", units="500"), "only 100 held"),
    (dict(side="SELL", units="1", ticker="MU"), "only 0 held"),
    (dict(side="BUY", units="all"), "'all'"),
    (dict(side="BUY", units="-5"), "positive"),
    (dict(side="HOLD", units="5"), "Buy or Sell"),
    (dict(side="BUY", units="5", day=D0), "custodian file is dated"),
    (dict(side="BUY", units="5", day=D0 - timedelta(days=1)), "on or after"),
])
def test_rejections_leave_no_trace(session, kw, msg):
    _upload(session, D0, BASE)
    with pytest.raises(tr.TradeError, match=msg):
        _trade(session, **kw)
    session.rollback()
    assert session.execute(select(ManualTrade)).scalars().all() == []
    assert len(session.execute(select(Snapshot)).scalars().all()) == 1


def test_note_creates_review_entry(session):
    _upload(session, D0, BASE)
    _trade(session, "SELL", "100", "210", note="valuation stretched")
    rv = session.execute(select(ReviewLog)).scalar_one()
    assert rv.stance == "EXIT" and rv.price == Decimal("210")
    assert "Sell 100 @ 210.00 — valuation stretched" == rv.note


def test_undo_restores_and_removes_empty_snapshot(session):
    _upload(session, D0, BASE)
    a = _trade(session, "BUY", "100", "200").trade
    b = _trade(session, "SELL", "50", "210").trade
    tr.undo_trade(session, "NVDA", b.id)
    assert _book(session, D0 + timedelta(days=1))["NVDA"] == (Decimal(200), Decimal(150))
    tr.undo_trade(session, "NVDA", a.id)
    assert len(session.execute(select(Snapshot)).scalars().all()) == 1
    assert session.execute(select(Change)).scalars().all() == []


def test_undo_blocked_once_newer_snapshot_exists(session):
    _upload(session, D0, BASE)
    a = _trade(session, "BUY", "100", "200").trade
    _trade(session, "BUY", "1", "200", day=D0 + timedelta(days=2))
    rows = tr.list_trades(session, "NVDA")
    assert [r.undoable for r in rows] == [True, False]
    with pytest.raises(tr.TradeError, match="latest snapshot"):
        tr.undo_trade(session, "NVDA", a.id)


def test_earlier_custodian_upload_flows_into_manual_snapshot(session):
    _upload(session, D0, BASE)
    _trade(session, "BUY", "10", "200", day=D0 + timedelta(days=5))
    # A custodian file dated BEFORE the trade arrives late, with AMD trimmed.
    _upload(session, D0 + timedelta(days=3),
            [_h("NVDA", 100, 100), _h("AMD", 20, 150), _h(CASH_TICKER, 24500, 1, cash=True)])
    book = _book(session, D0 + timedelta(days=5))
    assert book["AMD"][0] == 20                      # picked up from the new base
    assert book["NVDA"][0] == 110
    assert book[CASH_TICKER][0] == Decimal(24500 - 2000)


def test_same_day_upload_replaces_manual_snapshot(session):
    _upload(session, D0, BASE)
    _trade(session, "BUY", "10", "200")
    _upload(session, D0 + timedelta(days=1), BASE, overwrite=True)
    assert session.execute(select(ManualTrade)).scalars().all() == []
    assert session.execute(select(Snapshot.source).order_by(Snapshot.as_of.desc())).scalars().first() == "upload"


def test_staleness_counts_from_custodian_file(session):
    _upload(session, D0, BASE)
    _trade(session, "BUY", "1", "200", day=date.today())
    pv = build_positions(session)
    assert pv.as_of == date.today()
    assert pv.custodian_as_of == D0
    assert pv.staleness_days == (date.today() - D0).days
    assert pv.manual_trades == 1
