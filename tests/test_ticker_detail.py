"""_derived must always populate every key the template reads: `x is not none`
passes for Jinja Undefined and the subsequent compare then 500s the page
(seen live for CBRS/XNDU, which lack a 50d/200d moving average)."""

from decimal import Decimal
from types import SimpleNamespace

from app.ticker_detail import TickerDetail, _derived

TEMPLATE_KEYS = {"implied_upside", "range52_pos", "dist_dma50", "dist_dma200", "total_grades"}


def _detail(fundamentals) -> TickerDetail:
    return TickerDetail(ticker="TEST", security=None, pillar_name=None,
                        crossref_name=None, held=True, position=None,
                        fundamentals=fundamentals)


def _fund(**overrides) -> SimpleNamespace:
    base = dict(target_consensus=None, week52_high=None, week52_low=None,
                dma_50=None, dma_200=None, grades_strong_buy=None, grades_buy=None,
                grades_hold=None, grades_sell=None, grades_strong_sell=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_derived_all_keys_present_without_fundamentals():
    d = _derived(_detail(None), position=None)
    assert TEMPLATE_KEYS <= d.keys()


def test_derived_all_keys_present_with_sparse_fundamentals():
    # A ticker with a price but no moving averages (the CBRS/XNDU case).
    pos = SimpleNamespace(price=Decimal("10"))
    d = _derived(_detail(_fund()), position=pos)
    assert TEMPLATE_KEYS <= d.keys()
    assert d["dist_dma50"] is None
    assert d["dist_dma200"] is None


def test_derived_computes_when_data_present():
    pos = SimpleNamespace(price=Decimal("10"))
    d = _derived(_detail(_fund(dma_50=8, dma_200=20, target_consensus=15,
                                week52_high=20, week52_low=5)), position=pos)
    assert d["dist_dma50"] == Decimal("0.25")
    assert d["dist_dma200"] == Decimal("-0.5")
    assert d["implied_upside"] == Decimal("0.5")
    assert d["range52_pos"] == Decimal("5") / Decimal("15")
