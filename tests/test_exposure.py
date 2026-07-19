"""Concentration maths, with prices seeded directly (no live API)."""

from datetime import date, datetime, timezone
from decimal import Decimal

from app.exposure import build_exposure
from app.ingest.commit import commit_snapshot
from app.ingest.parser import ParsedHolding
from app.models import Pillar, QuoteCache


def _h(ticker, units, cost, exch="US", isin=None, cash=False):
    return ParsedHolding(
        raw_ticker=f"{ticker} {exch}" if exch else ticker, ticker=ticker, exchange=exch,
        units=Decimal(str(units)), avg_cost=Decimal(str(cost)), isin=isin, is_cash=cash,
    )


def _seed_quote(session, ticker, price, ok=True, err=None):
    session.add(QuoteCache(
        ticker=ticker, fmp_symbol=ticker,
        price=Decimal(str(price)) if price is not None else None,
        prev_close=Decimal(str(price)) if price is not None else None,
        ok=ok, error=err, fetched_at=datetime.now(timezone.utc),
    ))


def _four_equal(session):
    # 4 names at $100 each + $100 cash -> invested 400, fund 500
    commit_snapshot(holdings=[
        _h("AAA", 1, 100, isin="US0000000001"),
        _h("BBB", 1, 100, isin="US0000000002"),
        _h("CCC", 1, 100, isin="US0000000003"),
        _h("DDD", 1, 100, isin="US0000000004"),
        _h("USD Cash", 100, 1, exch=None, cash=True),
    ], as_of=date(2026, 7, 1), filename="f", notes=None, pillar_assignments=None, session=session)
    for t in ("AAA", "BBB", "CCC", "DDD"):
        _seed_quote(session, t, 100)
    session.commit()


def test_effective_n_equal_weight(session):
    _four_equal(session)
    v = build_exposure(session)
    # 4 equal invested weights -> HHI = 4*(0.25^2)=0.25, effective N = 4
    assert v.effective_n == Decimal("4")
    assert round(v.hhi, 4) == Decimal("0.2500")
    assert v.name_count == 4
    # cash 100/500 = 20%
    assert v.cash_pct == Decimal("0.2")
    assert v.invested_value == Decimal("400")
    assert v.total_value == Decimal("500")


def test_top_n_and_cumulative(session):
    _four_equal(session)
    v = build_exposure(session)
    # each name is 100/500 = 20% of the fund; top5 (only 4 exist) = 80%
    assert v.top5 == Decimal("0.8")
    assert v.holdings[0].fund_weight == Decimal("0.2")
    assert v.holdings[3].cumulative == Decimal("0.8")


def test_single_name_flags(session):
    commit_snapshot(holdings=[
        _h("BIG", 90, 1, isin="US0000000001"),      # 900 -> 90% of fund
        _h("SM1", 5, 1, isin="US0000000002"),        # 50 -> 5%
        _h("SM2", 5, 1, isin="US0000000003"),        # 50 -> 5%
    ], as_of=date(2026, 7, 1), filename="f", notes=None, pillar_assignments=None, session=session)
    for t, p in (("BIG", 10), ("SM1", 10), ("SM2", 10)):
        _seed_quote(session, t, p)
    session.commit()
    v = build_exposure(session)
    flags = {f.ticker: f.flag for f in v.flags}
    assert flags["BIG"] == "high"     # >= 8%
    # 500/10000 = 5% exactly -> warn
    assert flags.get("SM1") == "warn"


def test_pillar_aggregation_with_etf(session):
    session.add(Pillar(id="P01", name="Semis", primary_etf="SMH"))
    _four_equal(session)
    # assign two names to the Semis pillar
    from app.models import Security
    session.get(Security, "AAA").pillar = "Semis"
    session.get(Security, "AAA").pillar_id = "P01"
    session.get(Security, "BBB").pillar = "Semis"
    session.get(Security, "BBB").pillar_id = "P01"
    session.commit()

    v = build_exposure(session)
    semis = next(p for p in v.pillars if p.name == "Semis")
    assert semis.count == 2
    assert semis.market_value == Decimal("200")
    assert semis.etf == "SMH"
    assert semis.fund_weight == Decimal("0.4")   # 200/500


def test_unpriced_excluded(session):
    commit_snapshot(holdings=[
        _h("AAA", 1, 100, isin="US0000000001"),
        _h("GHOST", 1, 100, exch="GR", isin="DE0000000001"),
    ], as_of=date(2026, 7, 1), filename="f", notes=None, pillar_assignments=None, session=session)
    _seed_quote(session, "AAA", 100)
    _seed_quote(session, "GHOST", None, ok=False, err="no live price")
    session.commit()
    v = build_exposure(session)
    assert v.name_count == 1
    assert "GHOST" in v.unpriced
    assert v.effective_n == Decimal("1")   # only one priced name


def test_no_data(session):
    assert build_exposure(session) is None
