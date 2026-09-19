"""Commit a validated, previewed snapshot to the database.

Writes are transactional: the snapshot, its holdings, and the securities metadata
upserts all succeed together or not at all. Overwriting an existing `as_of` is
allowed only when the caller passes overwrite=True (the UI gates this behind an
explicit confirmation).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.ingest.diff import rebuild_changes
from app.ingest.parser import ParsedHolding
from app.models import HoldingSnapshot, Security, Snapshot


class CommitError(RuntimeError):
    pass


def commit_snapshot(
    *,
    holdings: list[ParsedHolding],
    as_of: date,
    filename: str | None,
    notes: str | None,
    pillar_assignments: dict[str, str] | None,
    session: Session,
    overwrite: bool = False,
) -> Snapshot:
    pillar_assignments = pillar_assignments or {}

    existing = session.execute(
        select(Snapshot).where(Snapshot.as_of == as_of)
    ).scalar_one_or_none()
    if existing is not None:
        if not overwrite:
            raise CommitError(
                f"A snapshot for {as_of} already exists. Confirm overwrite to replace it."
            )
        # Cascade deletes the old holdings.
        session.execute(delete(Snapshot).where(Snapshot.id == existing.id))
        session.flush()

    snapshot = Snapshot(as_of=as_of, filename=filename, notes=notes)
    session.add(snapshot)
    session.flush()  # assign snapshot.id

    for h in holdings:
        session.add(
            HoldingSnapshot(
                snapshot_id=snapshot.id,
                ticker=h.ticker,
                raw_ticker=h.raw_ticker,
                units=h.units,
                avg_cost=h.avg_cost,
            )
        )
        if h.is_cash:
            continue
        _upsert_security(session, h, as_of, pillar_assignments.get(h.ticker))

    # Manual-trade snapshots are derived from their predecessor, so re-derive any
    # that sit after this file. Then rebuild the change history in the same
    # transaction — cheap, and correct even when this snapshot lands mid-series.
    session.flush()
    from app.trades import rebuild_manual_snapshots
    rebuild_manual_snapshots(session)
    rebuild_changes(session, commit=False)
    from app.attribution import safe_rebuild
    safe_rebuild(session)

    session.commit()
    return snapshot


def _upsert_security(
    session: Session, h: ParsedHolding, as_of: date, pillar: str | None
) -> None:
    sec = session.get(Security, h.ticker)
    if sec is None:
        sec = Security(ticker=h.ticker, first_seen=as_of)
        session.add(sec)
    # Refresh identity fields from the file; keep first_seen and manual fields.
    if h.name:
        sec.name = h.name
    if h.exchange:
        sec.exchange = h.exchange
    if h.isin:
        sec.isin = h.isin
    if pillar:
        sec.pillar = pillar.strip() or None
    if sec.first_seen is None:
        sec.first_seen = as_of
    sec.active = True
