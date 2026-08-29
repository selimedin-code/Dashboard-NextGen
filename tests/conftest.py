"""Test fixtures.

DB tests run against a dedicated `nextgen_test` Postgres database (JSONB/NUMERIC
mean SQLite is not a faithful stand-in). Each test gets a fresh session; tables
are created once per session and truncated between tests.
"""

from __future__ import annotations

import os

import pytest

# Point the app at the test database BEFORE importing anything that reads settings.
# Force it unless the environment already names a *_test database: the schema
# fixture drop_all()s whatever this points at, and a sourced .env once sent that
# at the dev database and wiped it.
if not os.environ.get("DATABASE_URL", "").rstrip("/").endswith("_test"):
    os.environ["DATABASE_URL"] = "postgresql+psycopg://localhost:5432/nextgen_test"

from sqlalchemy import text  # noqa: E402

from app.db import Base, engine, SessionLocal  # noqa: E402
from app import models  # noqa: F401,E402  (register tables on Base.metadata)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        # Clean every table so tests are independent.
        for table in reversed(Base.metadata.sorted_tables):
            s.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))
        s.commit()
        s.close()


@pytest.fixture(scope="session")
def portfolio_pdf_bytes() -> bytes:
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "CurrentPortfolio.pdf"
    if not path.exists():
        pytest.skip("CurrentPortfolio.pdf not present")
    return path.read_bytes()
