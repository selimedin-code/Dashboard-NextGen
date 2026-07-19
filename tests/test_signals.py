from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.ingest.commit import commit_snapshot
from app.ingest.parser import ParsedHolding
from app.models import EarningsHistory, FundamentalsSnapshot, NewsItem, QuoteCache, Security
from app.signals import build_signals


def _h(t, u, c, isin=None, cash=False):
    return ParsedHolding(raw_ticker=f"{t} US", ticker=t, exchange="US",
                         units=Decimal(str(u)), avg_cost=Decimal(str(c)), isin=isin, is_cash=cash)


def _quote(session, t, price, prev):
    # day_change_pct stored as the raw percent number (FMP changePercentage)
    dc = (Decimal(str(price)) / Decimal(str(prev)) - 1) * 100
    session.add(QuoteCache(ticker=t, fmp_symbol=t, price=Decimal(str(price)),
                           prev_close=Decimal(str(prev)), day_change_pct=dc, ok=True,
                           fetched_at=datetime.now(timezone.utc)))


def _book(session):
    commit_snapshot(holdings=[
        _h("AAA", 100, 50, isin="US0000000001"),
        _h("BBB", 100, 50, isin="US0000000002"),
        _h("USD Cash", 1000, 1, cash=True),
    ], as_of=date(2026, 7, 1), filename="f", notes=None, pillar_assignments=None, session=session)


def test_outsized_move_flagged(session):
    _book(session)
    _quote(session, "AAA", 110, 100)   # +10% -> high
    _quote(session, "BBB", 101, 100)   # +1% -> no flag
    session.commit()
    v = build_signals(session)
    tickers = {s.ticker: s for s in v.moves}
    assert "AAA" in tickers and tickers["AAA"].severity == "high"
    assert "BBB" not in tickers


def test_trend_below_200d(session):
    _book(session)
    _quote(session, "AAA", 90, 90)
    _quote(session, "BBB", 90, 90)
    session.add(FundamentalsSnapshot(ticker="AAA", as_of=date(2026, 7, 1),
                                     dma_50=Decimal("95"), dma_200=Decimal("100")))
    session.commit()
    v = build_signals(session)
    assert any(s.ticker == "AAA" and "200-day" in s.headline for s in v.trend)


def test_stop_breach_and_near(session):
    _book(session)
    _quote(session, "AAA", 40, 40)     # stop 45 -> breached
    _quote(session, "BBB", 46, 46)     # stop 45 -> within 3% (near)
    session.get(Security, "AAA").stop_price = Decimal("45")
    session.get(Security, "BBB").stop_price = Decimal("45")
    session.commit()
    v = build_signals(session)
    by = {s.ticker: s.severity for s in v.stops}
    assert by["AAA"] == "high"
    assert by["BBB"] == "warn"


def test_upcoming_earnings(session):
    _book(session)
    _quote(session, "AAA", 50, 50)
    _quote(session, "BBB", 50, 50)
    soon = date.today() + timedelta(days=3)
    far = date.today() + timedelta(days=40)
    session.add(EarningsHistory(ticker="AAA", fiscal_ending=soon, eps_actual=None, eps_estimate=Decimal("1")))
    session.add(EarningsHistory(ticker="BBB", fiscal_ending=far, eps_actual=None, eps_estimate=Decimal("1")))
    session.commit()
    v = build_signals(session)
    tickers = {s.ticker for s in v.earnings}
    assert "AAA" in tickers        # within 14d
    assert "BBB" not in tickers     # too far out


def test_concentration_breach(session):
    # one name dominates the fund
    commit_snapshot(holdings=[
        _h("BIG", 100, 1, isin="US0000000001"),
        _h("SM", 1, 1, isin="US0000000002"),
    ], as_of=date(2026, 7, 1), filename="f", notes=None, pillar_assignments=None, session=session)
    _quote(session, "BIG", 100, 100)   # 10000 of ~10001 -> ~100%
    _quote(session, "SM", 1, 1)
    session.commit()
    v = build_signals(session)
    assert any(s.ticker == "BIG" and s.severity == "high" for s in v.concentration)


def test_recent_news_only(session):
    _book(session)
    _quote(session, "AAA", 50, 50); _quote(session, "BBB", 50, 50)
    session.add(NewsItem(ticker="AAA", url="u1", title="fresh",
                         published_at=datetime.now(timezone.utc) - timedelta(hours=5)))
    session.add(NewsItem(ticker="AAA", url="u2", title="stale",
                         published_at=datetime.now(timezone.utc) - timedelta(days=10)))
    session.commit()
    v = build_signals(session)
    titles = {s.headline for s in v.news}
    assert "fresh" in titles
    assert "stale" not in titles


def test_no_snapshot(session):
    v = build_signals(session)
    assert v.as_of is None
    assert v.total == 0
