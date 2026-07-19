from sqlalchemy import select

from app.ingest.seed import detect_kind, import_from_csv
from app.models import Pillar, Security

PILLARS_CSV = (
    b"pillar_id,pillar_name,primary_etf,alt_etf,etf_caveat\n"
    b"P01,AI Semis & Chip Supply,SMH,SOXX,\n"
    b'P03,Neoclouds & AI Compute,,SKYY,"No suitable ETF exists; SKYY is a weak proxy."\n'
    b"P04,Mag 7 / Hyperscalers,MAGS,QQQ,\n"
)

SEED_CSV = (
    b"ticker,raw_ticker,exchange,isin,name,pillar_id,pillar_name,crossref_pillar_id,file_sector\n"
    b"NVDA,NVDA US,US,US67066G1040,NVIDIA Corp,P01,AI Semis & Chip Supply,P04,Technology\n"
    b"IFX,IFX GR,GR,DE0006231004,Infineon Technologies AG,P01,AI Semis & Chip Supply,,Technology\n"
    b"CRWV,CRWV US,US,US21873S1087,CoreWeave Inc,P03,Neoclouds & AI Compute,,Technology\n"
)


def test_detect_kind():
    assert detect_kind({"pillar_id", "pillar_name", "primary_etf", "alt_etf"}) == "pillars"
    assert detect_kind({"ticker", "pillar_id", "pillar_name", "name"}) == "securities"
    assert detect_kind({"foo", "bar"}) == "unknown"


def test_import_pillars(session):
    summary = import_from_csv(session, "pillars.csv", PILLARS_CSV)
    session.commit()
    assert summary == {"kind": "pillars", "created": 3, "updated": 0}

    p3 = session.get(Pillar, "P03")
    assert p3.name == "Neoclouds & AI Compute"
    assert p3.primary_etf is None
    assert p3.alt_etf == "SKYY"
    assert "weak proxy" in p3.caveat


def test_import_securities_resolves_pillar_and_crossref(session):
    import_from_csv(session, "pillars.csv", PILLARS_CSV)
    session.commit()
    summary = import_from_csv(session, "securities_seed.csv", SEED_CSV)
    session.commit()
    assert summary["kind"] == "securities"
    assert summary["created"] == 3

    nvda = session.get(Security, "NVDA")
    assert nvda.pillar == "AI Semis & Chip Supply"
    assert nvda.pillar_id == "P01"
    assert nvda.crossref_pillar_id == "P04"
    assert nvda.isin == "US67066G1040"
    assert nvda.exchange == "US"

    ifx = session.get(Security, "IFX")   # normalized from "IFX GR"
    assert ifx.pillar_id == "P01"
    assert ifx.crossref_pillar_id is None


def test_securities_import_refreshes_existing_name(session):
    session.add(Security(ticker="NVDA", name="mangled name from pdf", active=True))
    session.commit()
    import_from_csv(session, "pillars.csv", PILLARS_CSV)
    import_from_csv(session, "securities_seed.csv", SEED_CSV)
    session.commit()
    nvda = session.get(Security, "NVDA")
    assert nvda.name == "NVIDIA Corp"       # refreshed from the clean seed
    assert nvda.active is True               # existing flag preserved


def test_dangling_pillar_ref_becomes_null(session):
    import_from_csv(session, "pillars.csv", PILLARS_CSV)
    session.commit()
    bad = (
        b"ticker,pillar_id,pillar_name,crossref_pillar_id\n"
        b"ZZZ,P99,Nonexistent,P77\n"     # neither pillar exists in the taxonomy
    )
    import_from_csv(session, "seed.csv", bad)
    session.commit()
    z = session.get(Security, "ZZZ")
    assert z.pillar_id is None
    assert z.crossref_pillar_id is None


def test_unknown_csv_raises(session):
    import pytest
    from app.ingest.seed import SeedFormatError
    with pytest.raises(SeedFormatError):
        import_from_csv(session, "x.csv", b"a,b\n1,2\n")
