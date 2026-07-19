"""FMP client parse logic, exercised without network by stubbing _get."""

from decimal import Decimal

from app.providers.fmp import FMPClient, Quote


def _client() -> FMPClient:
    c = FMPClient("dummy-key")
    c.close()  # we won't use the real http client
    return c


def test_get_quote_parses_fields(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_get", lambda path, **p: [{
        "symbol": "NVDA", "name": "NVIDIA Corporation", "price": 202.81,
        "previousClose": 207.4, "changePercentage": -2.21311,
        "currency": "USD", "timestamp": 1_752_000_000,
    }])
    q = c.get_quote("NVDA")
    assert isinstance(q, Quote)
    assert q.price == Decimal("202.81")
    assert q.prev_close == Decimal("207.4")
    assert q.day_change_pct == Decimal("-2.21311")
    assert q.currency == "USD"


def test_get_quote_empty_raises(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_get", lambda path, **p: [])
    try:
        c.get_quote("BADX")
        assert False, "expected error"
    except RuntimeError:
        pass


def test_get_quote_missing_price_raises(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_get", lambda path, **p: [{"symbol": "X", "price": None}])
    try:
        c.get_quote("X")
        assert False, "expected error"
    except RuntimeError:
        pass
