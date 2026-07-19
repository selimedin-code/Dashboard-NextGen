"""Phase 4 data layer + detail assembly, with a stubbed FMP client (no network)."""

from datetime import date, datetime, timezone
from decimal import Decimal

from app.fundamentals import _dma, _parse_range, refresh_ticker
from app.ingest.commit import commit_snapshot
from app.ingest.parser import ParsedHolding
from app.models import (
    EarningsHistory,
    EstimatesSnapshot,
    FundamentalsSnapshot,
    NewsItem,
    PriceHistoryCache,
    QuoteCache,
    Security,
)
from app.ticker_detail import get_ticker_detail
from sqlalchemy import select


class FakeFMP:
    """Returns canned payloads keyed by endpoint path."""

    def __init__(self):
        hist = [{"date": f"2026-06-{(i % 28) + 1:02d}", "price": 100 + i} for i in range(60)]
        self._data = {
            "profile": [{
                "companyName": "NVIDIA Corporation", "sector": "Technology",
                "industry": "Semiconductors", "exchange": "NASDAQ",
                "marketCap": 4_912_261_010_000, "beta": 2.211,
                "range": "164.07-236.54", "price": 202.81,
            }],
            "ratios-ttm": [{
                "priceToEarningsRatioTTM": 30.92, "netProfitMarginTTM": 0.6297,
                "operatingProfitMarginTTM": 0.6402, "priceToSalesRatioTTM": 19.38,
                "priceToBookRatioTTM": 25.2, "priceToEarningsGrowthRatioTTM": 0.28,
                "netIncomePerShareTTM": 6.57,
            }],
            "key-metrics-ttm": [{
                "evToEBITDATTM": 25.45, "evToSalesTTM": 19.38,
                "returnOnEquityTTM": 1.116, "returnOnAssetsTTM": 0.615,
            }],
            "price-target-consensus": [{
                "targetHigh": 500, "targetLow": 218, "targetConsensus": 319.48, "targetMedian": 300,
            }],
            "grades-consensus": [{
                "strongBuy": 2, "buy": 58, "hold": 16, "sell": 3, "strongSell": 0, "consensus": "Buy",
            }],
            "analyst-estimates": [
                {"date": "2027-01-25", "revenueAvg": 300e9, "epsAvg": 8.0,
                 "epsLow": 7.0, "epsHigh": 9.0, "numAnalystsEps": 40, "numAnalystsRevenue": 42},
                {"date": "2028-01-25", "revenueAvg": 380e9, "epsAvg": 10.5,
                 "epsLow": 9.0, "epsHigh": 12.0, "numAnalystsEps": 35, "numAnalystsRevenue": 38},
            ],
            "earnings": [
                {"date": "2026-05-28", "epsActual": 0.96, "epsEstimated": 0.88},   # +9.09%
                {"date": "2026-02-26", "epsActual": 0.89, "epsEstimated": 0.85},
            ],
            "historical-price-eod/light": list(reversed(hist)),   # provider is newest-first
            "news/stock": [
                {"url": "https://x.test/a", "title": "Chip demand strong",
                 "publisher": "Test Wire", "publishedDate": "2026-07-18 10:00:00", "text": "..."},
                {"url": "https://x.test/b", "title": "New GPU launch",
                 "publisher": "Test Wire", "publishedDate": "2026-07-17 09:00:00", "text": "..."},
            ],
        }

    def request(self, path, **params):
        return self._data.get(path, [])

    def close(self):
        pass


def test_parse_range():
    assert _parse_range("164.07-236.54") == (Decimal("164.07"), Decimal("236.54"))
    assert _parse_range(None) == (None, None)


def test_dma():
    series = [(f"d{i}", Decimal(i)) for i in range(1, 61)]   # 1..60
    # last 50 = 11..60, mean = 35.5
    assert _dma(series, 50) == Decimal("35.5")
    assert _dma(series, 200) is None


def _held_nvda(session):
    commit_snapshot(holdings=[
        ParsedHolding(raw_ticker="NVDA US", ticker="NVDA", exchange="US",
                      units=Decimal("100"), avg_cost=Decimal("50"), isin="US67066G1040"),
    ], as_of=date(2026, 7, 1), filename="f", notes=None, pillar_assignments=None, session=session)
    session.add(QuoteCache(ticker="NVDA", fmp_symbol="NVDA", price=Decimal("202.81"),
                           prev_close=Decimal("200"), ok=True, fetched_at=datetime.now(timezone.utc)))
    session.commit()


def test_refresh_ticker_maps_all_sources(session):
    _held_nvda(session)
    status = refresh_ticker(session, "NVDA", client=FakeFMP())
    assert all(v == "ok" for v in status.values())

    f = session.execute(select(FundamentalsSnapshot).where(FundamentalsSnapshot.ticker == "NVDA")).scalar_one()
    assert f.company_name == "NVIDIA Corporation"
    assert f.pe_trailing == Decimal("30.92")
    assert f.profit_margin == Decimal("0.6297")
    assert f.ev_to_ebitda == Decimal("25.45")
    assert f.target_consensus == Decimal("319.48")
    assert f.grades_consensus == "Buy"
    assert f.week52_low == Decimal("164.07") and f.week52_high == Decimal("236.54")
    assert f.dma_50 is not None       # 60 points cached -> 50d available

    assert session.execute(select(EstimatesSnapshot).where(EstimatesSnapshot.ticker == "NVDA")).scalars().all()
    assert session.execute(select(NewsItem).where(NewsItem.ticker == "NVDA")).scalars().all()
    ph = session.get(PriceHistoryCache, "NVDA")
    assert len(ph.series) == 60


def test_earnings_surprise_computed(session):
    _held_nvda(session)
    refresh_ticker(session, "NVDA", client=FakeFMP())
    e = session.execute(
        select(EarningsHistory).where(EarningsHistory.ticker == "NVDA",
                                      EarningsHistory.fiscal_ending == date(2026, 5, 28))
    ).scalar_one()
    # (0.96 - 0.88)/0.88 * 100 = 9.09
    assert round(e.surprise_pct, 2) == Decimal("9.09")


def test_detail_assembly_and_derived(session):
    _held_nvda(session)
    refresh_ticker(session, "NVDA", client=FakeFMP())
    d = get_ticker_detail(session, "NVDA")

    assert d.held is True
    assert d.position.units == Decimal("100")
    assert d.fundamentals.company_name == "NVIDIA Corporation"
    assert len(d.estimates) == 2
    assert d.chart is not None and d.chart.points

    # implied upside = 319.48 / 202.81 - 1  ~= 0.575
    assert round(d.derived["implied_upside"], 3) == Decimal("0.575")
    # 52w position = (202.81 - 164.07)/(236.54 - 164.07) ~= 0.535
    assert round(d.derived["range52_pos"], 3) == Decimal("0.535")
    assert d.derived["total_grades"] == 79


def test_note_persists(session):
    _held_nvda(session)
    sec = session.get(Security, "NVDA")
    sec.thesis_note = "core compute"
    session.commit()
    assert get_ticker_detail(session, "NVDA").security.thesis_note == "core compute"


def test_detail_for_unheld_ticker(session):
    d = get_ticker_detail(session, "ZZZ")
    assert d.held is False
    assert d.position is None
    assert d.fundamentals is None


def test_earnings_duplicate_fiscal_date_deduped(session):
    """A provider returning two rows for the same fiscal date must not blow up the
    unique key (regression: XNDU in the bulk refresh)."""
    _held_nvda(session)

    class DupFMP(FakeFMP):
        def request(self, path, **params):
            if path == "earnings":
                return [
                    {"date": "2026-08-05", "epsActual": None, "epsEstimated": -0.37},
                    {"date": "2026-08-05", "epsActual": None, "epsEstimated": -0.235},
                ]
            return super().request(path, **params)

    status = refresh_ticker(session, "NVDA", client=DupFMP())   # must not raise
    assert status["earnings"] == "ok"
    from app.models import EarningsHistory
    rows = session.execute(
        select(EarningsHistory).where(EarningsHistory.ticker == "NVDA",
                                      EarningsHistory.fiscal_ending == date(2026, 8, 5))
    ).scalars().all()
    assert len(rows) == 1
