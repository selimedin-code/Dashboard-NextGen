import io
from decimal import Decimal

from openpyxl import Workbook

from app.ingest.normalize import CASH_TICKER
from app.ingest.parser import parse_excel, parse_pdf, parse_upload


def _make_excel(rows, headers=("Ticker", "ISIN", "Name", "Units", "AvgCost")):
    wb = Workbook()
    ws = wb.active
    ws.append(["some title junk"])            # header not on row 1 — parser must find it
    ws.append(list(headers))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_excel_basic():
    data = _make_excel([
        ("NVDA US", "US67066G1040", "NVIDIA Corp", 1249.94, 50.44),
        ("IFX GR", "DE0006231004", "Infineon", 1548.06, 34.83),
        ("USD Cash", "n.a.", "", 139971.00, 1.00),
    ])
    result = parse_excel(data)
    assert result.source_format == "excel"
    by = {h.ticker: h for h in result.holdings}
    assert by["NVDA"].exchange == "US"
    assert by["NVDA"].units == Decimal("1249.94")
    assert by["NVDA"].avg_cost == Decimal("50.44")
    assert by["IFX"].exchange == "GR"
    assert by[CASH_TICKER].is_cash
    assert by[CASH_TICKER].avg_cost == Decimal("1")


def test_parse_excel_column_order_and_synonyms():
    data = _make_excel(
        [("AMD US", "US0079031078", "AMD", 277.73, 134.65)],
        headers=("Symbol", "Quantity", "Cost", "ISIN", "Description"),
    )
    # header order differs; values must line up with headers
    # rebuild with matching row order:
    wb_data = _make_excel(
        [("AMD US", 277.73, 134.65, "US0079031078", "AMD")],
        headers=("Symbol", "Quantity", "Cost", "ISIN", "Description"),
    )
    result = parse_excel(wb_data)
    h = result.holdings[0]
    assert h.ticker == "AMD"
    assert h.units == Decimal("277.73")
    assert h.avg_cost == Decimal("134.65")


def test_parse_pdf_matches_file_exactly(portfolio_pdf_bytes):
    result = parse_pdf(portfolio_pdf_bytes)
    assert result.source_format == "pdf"

    holdings = result.holdings
    non_cash = [h for h in holdings if not h.is_cash]
    cash = [h for h in holdings if h.is_cash]

    assert len(non_cash) == 55
    assert len(cash) == 1
    assert cash[0].units == Decimal("139971.00")
    assert cash[0].avg_cost == Decimal("1")

    by = {h.ticker: h for h in holdings}
    # spot checks against the actual file
    assert by["NVDA"].units == Decimal("1249.94")
    assert by["NVDA"].avg_cost == Decimal("50.44")
    assert by["NVDA"].exchange == "US"
    assert by["TSM"].units == Decimal("369.37")
    assert by["TSM"].avg_cost == Decimal("131.44")
    assert by["IFX"].exchange == "GR"
    assert by["IFX"].isin == "DE0006231004"
    assert by["EQIX"].units == Decimal("40.43")
    assert by["EQIX"].avg_cost == Decimal("816.35")
    assert by["BBAI"].units == Decimal("7236.74")

    # known ISIN collision must be preserved, not deduped
    colliding = [h.ticker for h in holdings if h.isin == "US8887871080"]
    assert set(colliding) == {"MDB", "TOST"}


def test_parse_upload_dispatch(portfolio_pdf_bytes):
    assert parse_upload("CurrentPortfolio.pdf", portfolio_pdf_bytes).source_format == "pdf"
