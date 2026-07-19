from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.ingest.commit import CommitError, commit_snapshot
from app.ingest.parser import ParsedHolding, parse_pdf
from app.ingest.validate import validate_snapshot
from app.models import HoldingSnapshot, Security, Snapshot


def _h(ticker, units, cost, exch="US", isin=None, name=None, cash=False):
    return ParsedHolding(
        raw_ticker=f"{ticker} {exch}" if exch else ticker, ticker=ticker,
        exchange=exch, units=Decimal(str(units)), avg_cost=Decimal(str(cost)),
        isin=isin, name=name, is_cash=cash,
    )


def test_first_commit_persists_everything(session, portfolio_pdf_bytes):
    holdings = parse_pdf(portfolio_pdf_bytes).holdings
    v = validate_snapshot(holdings, date(2026, 7, 18), session, quote_check=None)
    assert v.ok_to_commit
    # first snapshot: everything opens, all non-cash tickers are new
    assert all(c.change_type == "OPEN" for c in v.changes)
    assert len(v.new_tickers) == 55

    commit_snapshot(
        holdings=holdings, as_of=date(2026, 7, 18), filename="CurrentPortfolio.pdf",
        notes=None, pillar_assignments={"NVDA": "AI Compute"}, session=session,
    )

    assert session.execute(select(func.count()).select_from(Snapshot)).scalar_one() == 1
    assert session.execute(select(func.count()).select_from(HoldingSnapshot)).scalar_one() == 56
    nvda = session.get(Security, "NVDA")
    assert nvda.pillar == "AI Compute"
    assert nvda.first_seen == date(2026, 7, 18)
    assert nvda.exchange == "US"


def test_diff_detects_open_close_add_trim(session):
    base = [
        _h("NVDA", 100, 50, isin="US67066G1040"),
        _h("AMD", 200, 130, isin="US0079031078"),
        _h("USD Cash", 1000, 1, exch=None, cash=True),
    ]
    commit_snapshot(holdings=base, as_of=date(2026, 6, 1), filename="a", notes=None,
                    pillar_assignments=None, session=session)

    nxt = [
        _h("NVDA", 150, 50, isin="US67066G1040"),   # ADD
        _h("PLTR", 300, 20, isin="US69608A1088"),   # OPEN (AMD dropped -> CLOSE)
        _h("USD Cash", 900, 1, exch=None, cash=True),  # TRIM
    ]
    v = validate_snapshot(nxt, date(2026, 7, 1), session, quote_check=None)
    kinds = {c.ticker: c.change_type for c in v.changes}
    assert kinds["NVDA"] == "ADD"
    assert kinds["PLTR"] == "OPEN"
    assert kinds["AMD"] == "CLOSE"
    assert kinds["USD Cash"] == "TRIM"
    assert v.prior_as_of == date(2026, 6, 1)


def test_duplicate_as_of_blocks_without_overwrite(session):
    h = [_h("NVDA", 100, 50, isin="US67066G1040")]
    commit_snapshot(holdings=h, as_of=date(2026, 6, 1), filename="a", notes=None,
                    pillar_assignments=None, session=session)

    v = validate_snapshot(h, date(2026, 6, 1), session, quote_check=None)
    assert v.duplicate_as_of is True

    with pytest.raises(CommitError):
        commit_snapshot(holdings=h, as_of=date(2026, 6, 1), filename="b", notes=None,
                        pillar_assignments=None, session=session, overwrite=False)

    # overwrite replaces cleanly, no duplicate rows
    commit_snapshot(holdings=[_h("NVDA", 111, 50, isin="US67066G1040")],
                    as_of=date(2026, 6, 1), filename="b", notes=None,
                    pillar_assignments=None, session=session, overwrite=True)
    assert session.execute(select(func.count()).select_from(Snapshot)).scalar_one() == 1
    row = session.execute(select(HoldingSnapshot).where(HoldingSnapshot.ticker == "NVDA")).scalar_one()
    assert row.units == Decimal("111")


def test_non_positive_and_dup_ticker_block(session):
    v = validate_snapshot([_h("NVDA", 0, 50), _h("NVDA", 10, 50)], date(2026, 6, 1),
                          session, quote_check=None)
    assert not v.ok_to_commit
    assert any("Duplicate ticker" in b for b in v.blocking)
    assert any("units must be positive" in b for b in v.blocking)


def test_quote_check_blocks_unquotable(session):
    v = validate_snapshot(
        [_h("NVDA", 10, 50), _h("FAKE", 10, 5)], date(2026, 6, 1), session,
        quote_check=lambda t: t != "FAKE",
    )
    assert any("No live quote" in b and "FAKE" in b for b in v.blocking)


def test_duplicate_isin_warns(session):
    v = validate_snapshot(
        [_h("MDB", 10, 300, isin="US8887871080"), _h("TOST", 10, 40, isin="US8887871080")],
        date(2026, 6, 1), session, quote_check=None,
    )
    assert any("US8887871080" in w for w in v.warnings)
