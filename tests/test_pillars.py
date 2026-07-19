import io

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.ingest.pillars import PillarParseError, apply_pillars, parse_pillar_file
from app.models import Security


def _xlsx(rows, headers=("Ticker", "Pillar")):
    wb = Workbook(); ws = wb.active
    ws.append(list(headers))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


def test_parse_excel_normalizes_ticker():
    data = _xlsx([("NVDA US", "AI Compute"), ("IFX GR", "Semis"), ("PLTR", "AI Software")])
    m = parse_pillar_file("map.xlsx", data)
    assert m == {"NVDA": "AI Compute", "IFX": "Semis", "PLTR": "AI Software"}


def test_parse_csv_and_header_synonyms():
    csv_bytes = b"Symbol,Theme\nNVDA US,AI Compute\nAMD,AI Compute\n"
    m = parse_pillar_file("map.csv", csv_bytes)
    assert m == {"NVDA": "AI Compute", "AMD": "AI Compute"}


def test_parse_rejects_missing_columns():
    data = _xlsx([("NVDA", "x")], headers=("Foo", "Bar"))
    with pytest.raises(PillarParseError):
        parse_pillar_file("map.xlsx", data)


def test_parse_rejects_bad_type():
    with pytest.raises(PillarParseError):
        parse_pillar_file("map.pdf", b"whatever")


def test_apply_updates_existing_and_creates_missing(session):
    session.add(Security(ticker="NVDA", name="NVIDIA", active=True))
    session.commit()

    result = apply_pillars(session, {"NVDA": "AI Compute", "RKLB": "Space"}, create_missing=True)
    session.commit()
    assert result == {"updated": 1, "created": 1}

    nvda = session.get(Security, "NVDA")
    rklb = session.get(Security, "RKLB")
    assert nvda.pillar == "AI Compute"
    assert rklb.pillar == "Space"
    assert rklb.active is False        # pre-registered, not yet held


def test_apply_no_create_when_disabled(session):
    apply_pillars(session, {"GHOST": "X"}, create_missing=False)
    session.commit()
    assert session.get(Security, "GHOST") is None
