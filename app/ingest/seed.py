"""Seed importers for the pillar taxonomy and the securities pillar map.

Two file shapes, auto-detected by header so one uploader handles both:

  pillars.csv          -> pillar_id, pillar_name, primary_etf, alt_etf, etf_caveat
                          (+ optional macro_bet, target_weight)
  securities_seed.csv  -> ticker, ..., pillar_id, pillar_name, crossref_pillar_id, ...

Both are idempotent upserts. The securities import also refreshes name / ISIN /
exchange from the (clean) seed, which fixes names mangled by PDF extraction.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest.normalize import normalize_ticker, validate_isin
from app.models import Pillar, Security


class SeedFormatError(ValueError):
    pass


def _read_csv(filename: str, data: bytes) -> list[dict]:
    if not filename.lower().endswith(".csv"):
        raise SeedFormatError(f"Expected a .csv file, got {filename!r}.")
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise SeedFormatError("The CSV has no data rows.")
    return rows


def detect_kind(headers: set[str]) -> str:
    h = {c.strip().lower() for c in headers}
    if {"pillar_id", "pillar_name"} <= h and ("primary_etf" in h or "etf_caveat" in h):
        return "pillars"
    if "ticker" in h and ("pillar_id" in h or "pillar_name" in h):
        return "securities"
    return "unknown"


def import_from_csv(session: Session, filename: str, data: bytes) -> dict:
    """Detect the file kind and import. Returns a summary dict. Caller commits."""
    rows = _read_csv(filename, data)
    kind = detect_kind(set(rows[0].keys()))
    if kind == "pillars":
        return {"kind": "pillars", **import_pillars(session, rows)}
    if kind == "securities":
        return {"kind": "securities", **import_securities(session, rows)}
    raise SeedFormatError(
        "Unrecognized columns. Expected a pillar taxonomy (pillar_id, pillar_name, "
        "primary_etf) or a securities seed (ticker, pillar_id/pillar_name)."
    )


def import_pillars(session: Session, rows: list[dict]) -> dict:
    existing = {p.id: p for p in session.execute(select(Pillar)).scalars().all()}
    created = updated = 0
    for r in rows:
        pid = (r.get("pillar_id") or "").strip()
        name = (r.get("pillar_name") or "").strip()
        if not pid or not name:
            continue
        p = existing.get(pid)
        if p is None:
            p = Pillar(id=pid, name=name)
            session.add(p)
            existing[pid] = p
            created += 1
        else:
            updated += 1
        p.name = name
        p.primary_etf = (r.get("primary_etf") or "").strip() or None
        p.alt_etf = (r.get("alt_etf") or "").strip() or None
        p.caveat = (r.get("etf_caveat") or "").strip() or None
        # Optional column (Risk page): only set when present, so an older seed
        # file never nulls an existing mapping.
        mb = (r.get("macro_bet") or "").strip()
        if mb:
            p.macro_bet = mb
        tw = parse_target_weight(r.get("target_weight"))
        if tw is not None:
            p.target_weight = tw
    return {"created": created, "updated": updated}


def parse_target_weight(raw) -> Decimal | None:
    """'22.5%', '22.5' or '0.225' -> Decimal('0.225'). Blank -> None. A bare
    number above 1 is read as a percentage."""
    s = str(raw or "").strip().replace(",", "")
    if not s:
        return None
    pct = s.endswith("%")
    try:
        v = Decimal(s.rstrip("%").strip())
    except InvalidOperation as exc:
        raise SeedFormatError(f"Target weight '{raw}' is not a number.") from exc
    if pct or v > 1:
        v = v / 100
    if v < 0 or v > 1:
        raise SeedFormatError(f"Target weight '{raw}' must be between 0% and 100%.")
    return v


def import_securities(session: Session, rows: list[dict]) -> dict:
    pillar_names = {p.id: p.name for p in session.execute(select(Pillar)).scalars().all()}
    existing = {s.ticker: s for s in session.execute(select(Security)).scalars().all()}
    created = updated = 0
    for r in rows:
        raw = (r.get("ticker") or "").strip()
        if not raw:
            continue
        ticker, _ = normalize_ticker(raw)
        # Only keep pillar links the taxonomy actually knows — a dangling id would
        # violate the FK and abort the whole import.
        pid = (r.get("pillar_id") or "").strip() or None
        if pid not in pillar_names:
            pid = None
        crossref = (r.get("crossref_pillar_id") or "").strip() or None
        if crossref not in pillar_names:
            crossref = None
        # Prefer the canonical pillar name from the taxonomy; fall back to the row's.
        pillar_name = pillar_names.get(pid) or (r.get("pillar_name") or "").strip() or None

        sec = existing.get(ticker)
        if sec is None:
            sec = Security(ticker=ticker, active=False)
            session.add(sec)
            existing[ticker] = sec
            created += 1
        else:
            updated += 1

        if r.get("name"):
            sec.name = r["name"].strip()
        exch = (r.get("exchange") or "").strip()
        if exch:
            sec.exchange = exch
        isin = validate_isin(r.get("isin"))
        if isin:
            sec.isin = isin
        sec.pillar = pillar_name
        sec.pillar_id = pid
        sec.crossref_pillar_id = crossref
    return {"created": created, "updated": updated}
