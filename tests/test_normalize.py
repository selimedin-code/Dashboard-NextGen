from decimal import Decimal

import pytest

from app.ingest.normalize import (
    CASH_TICKER,
    ParseError,
    normalize_ticker,
    parse_decimal,
    validate_isin,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("139'971.00", Decimal("139971.00")),
        ("139’971.00", Decimal("139971.00")),      # typographic apostrophe
        ("1'249.94", Decimal("1249.94")),
        ("264’737.79", Decimal("264737.79")),
        ("50.44", Decimal("50.44")),
        ("13.70%", Decimal("13.70")),
        ("-11.57%", Decimal("-11.57")),
        (1249, Decimal("1249")),
        (Decimal("40.43"), Decimal("40.43")),
    ],
)
def test_parse_decimal_ok(raw, expected):
    assert parse_decimal(raw) == expected


def test_parse_decimal_float_no_binary_noise():
    # 0.1 as a float must not leak binary-float artifacts.
    assert parse_decimal(0.1) == Decimal("0.1")


@pytest.mark.parametrize("bad", ["n.a.", "", "-", "None", None, "abc"])
def test_parse_decimal_rejects(bad):
    with pytest.raises(ParseError):
        parse_decimal(bad)


@pytest.mark.parametrize(
    "raw,ticker,exch",
    [
        ("NVDA US", "NVDA", "US"),
        ("IFX GR", "IFX", "GR"),
        ("PLTR", "PLTR", None),
        ("USD Cash", CASH_TICKER, None),
        ("USD", CASH_TICKER, None),
        ("BRK.B US", "BRK.B", "US"),
    ],
)
def test_normalize_ticker(raw, ticker, exch):
    assert normalize_ticker(raw) == (ticker, exch)


def test_validate_isin():
    assert validate_isin("US67066G1040") == "US67066G1040"
    assert validate_isin("us67066g1040") == "US67066G1040"
    assert validate_isin("NOTANISIN") is None
    assert validate_isin(None) is None
