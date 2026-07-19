from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.ingest.commit import commit_snapshot
from app.ingest.diff import rebuild_changes
from app.ingest.parser import ParsedHolding
from app.models import Change


def _h(ticker, units, cost, exch="US", isin=None, cash=False):
    return ParsedHolding(
        raw_ticker=f"{ticker} {exch}" if exch else ticker, ticker=ticker, exchange=exch,
        units=Decimal(str(units)), avg_cost=Decimal(str(cost)), isin=isin, is_cash=cash,
    )


def _commit(session, holdings, d):
    return commit_snapshot(holdings=holdings, as_of=d, filename="f", notes=None,
                           pillar_assignments=None, session=session)


def _changes(session, to_id):
    rows = session.execute(select(Change).where(Change.to_snapshot == to_id)).scalars().all()
    return {c.ticker: c for c in rows}


def test_first_snapshot_has_no_changes(session):
    snap = _commit(session, [_h("NVDA", 100, 50, isin="US67066G1040")], date(2026, 1, 1))
    assert session.execute(select(Change)).scalars().all() == []
    assert snap is not None


def test_change_types_and_deltas(session):
    _commit(session, [
        _h("NVDA", 100, 50, isin="US67066G1040"),
        _h("AMD", 200, 130, isin="US0079031078"),
        _h("USD Cash", 1000, 1, exch=None, cash=True),
    ], date(2026, 1, 1))
    s2 = _commit(session, [
        _h("NVDA", 150, 50, isin="US67066G1040"),   # ADD
        _h("PLTR", 300, 20, isin="US69608A1088"),   # OPEN ; AMD -> CLOSE
        _h("USD Cash", 900, 1, exch=None, cash=True),
    ], date(2026, 2, 1))

    ch = _changes(session, s2.id)
    assert ch["NVDA"].change_type == "ADD"
    assert ch["NVDA"].units_delta == Decimal("50")
    assert ch["PLTR"].change_type == "OPEN"
    assert ch["AMD"].change_type == "CLOSE"
    assert ch["AMD"].units_after is None
    assert ch["USD Cash"].change_type == "TRIM"


def test_flow_vs_discretionary(session):
    # base book
    _commit(session, [
        _h("NVDA", 100, 50, isin="US67066G1040"),
        _h("AMD", 100, 50, isin="US0079031078"),
        _h("MSFT", 100, 50, isin="US5949181045"),
        _h("META", 100, 50, isin="US30303M1027"),
    ], date(2026, 1, 1))
    # +10% capital deployed pro-rata across most; NVDA gets a discretionary extra top-up
    s2 = _commit(session, [
        _h("NVDA", 150, 50, isin="US67066G1040"),   # +50% -> discretionary (off the median)
        _h("AMD", 110, 50, isin="US0079031078"),    # +10% -> flow
        _h("MSFT", 110, 50, isin="US5949181045"),   # +10% -> flow
        _h("META", 110, 50, isin="US30303M1027"),   # +10% -> flow
    ], date(2026, 2, 1))

    ch = _changes(session, s2.id)
    assert ch["AMD"].classification == "FLOW_DRIVEN"
    assert ch["MSFT"].classification == "FLOW_DRIVEN"
    assert ch["META"].classification == "FLOW_DRIVEN"
    assert ch["NVDA"].classification == "DISCRETIONARY"


def test_no_flow_event_everything_discretionary(session):
    _commit(session, [
        _h("NVDA", 100, 50, isin="US67066G1040"),
        _h("AMD", 100, 50, isin="US0079031078"),
    ], date(2026, 1, 1))
    # median move ~0 (AMD unchanged) -> NVDA's add is a decision, not flow
    s2 = _commit(session, [
        _h("NVDA", 130, 50, isin="US67066G1040"),
        _h("AMD", 100, 50, isin="US0079031078"),
    ], date(2026, 2, 1))
    ch = _changes(session, s2.id)
    assert ch["NVDA"].classification == "DISCRETIONARY"
    assert ch["AMD"].change_type == "HOLD"
    assert ch["AMD"].classification is None


def test_implied_add_price_identity(session):
    # buy 50 more of NVDA at a price that moves blended cost 50 -> 60
    # (100*50 + 50*P)/150 = 60  ->  P = 80
    _commit(session, [_h("NVDA", 100, 50, isin="US67066G1040")], date(2026, 1, 1))
    s2 = _commit(session, [_h("NVDA", 150, 60, isin="US67066G1040")], date(2026, 2, 1))
    ch = _changes(session, s2.id)
    assert ch["NVDA"].implied_price == Decimal("80")


def test_open_implied_price_is_cost(session):
    _commit(session, [_h("NVDA", 100, 50, isin="US67066G1040")], date(2026, 1, 1))
    s2 = _commit(session, [
        _h("NVDA", 100, 50, isin="US67066G1040"),
        _h("AMD", 10, 42, isin="US0079031078"),     # OPEN
    ], date(2026, 2, 1))
    ch = _changes(session, s2.id)
    assert ch["AMD"].change_type == "OPEN"
    assert ch["AMD"].implied_price == Decimal("42")


def test_overwrite_recomputes_cleanly(session):
    _commit(session, [_h("NVDA", 100, 50, isin="US67066G1040")], date(2026, 1, 1))
    s2 = _commit(session, [_h("NVDA", 150, 50, isin="US67066G1040")], date(2026, 2, 1))
    assert _changes(session, s2.id)["NVDA"].units_delta == Decimal("50")

    # overwrite the second snapshot with different units -> changes must update, not duplicate
    commit_snapshot(holdings=[_h("NVDA", 120, 50, isin="US67066G1040")],
                    as_of=date(2026, 2, 1), filename="f2", notes=None,
                    pillar_assignments=None, session=session, overwrite=True)
    rows = session.execute(select(Change)).scalars().all()
    assert len(rows) == 1
    assert rows[0].units_delta == Decimal("20")


def test_middle_insert_recomputes_both_neighbours(session):
    _commit(session, [_h("NVDA", 100, 50, isin="US67066G1040")], date(2026, 1, 1))
    _commit(session, [_h("NVDA", 300, 50, isin="US67066G1040")], date(2026, 3, 1))
    # insert a snapshot BETWEEN the two existing ones
    _commit(session, [_h("NVDA", 200, 50, isin="US67066G1040")], date(2026, 2, 1))

    # now there should be two pairs: Jan->Feb (+100) and Feb->Mar (+100)
    rows = session.execute(select(Change)).scalars().all()
    deltas = sorted(c.units_delta for c in rows)
    assert deltas == [Decimal("100"), Decimal("100")]


def test_rebuild_changes_idempotent(session):
    _commit(session, [_h("NVDA", 100, 50, isin="US67066G1040")], date(2026, 1, 1))
    _commit(session, [_h("NVDA", 150, 50, isin="US67066G1040")], date(2026, 2, 1))
    before = len(session.execute(select(Change)).scalars().all())
    rebuild_changes(session, commit=True)
    after = len(session.execute(select(Change)).scalars().all())
    assert before == after == 1
