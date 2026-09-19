"""Record a single-ticker trade between custodian files.

The app stays snapshot-based: a trade is not a ledger entry on its own, it lands
on a "manual" snapshot whose holdings are always rebuilt as

    previous snapshot (by as_of)  +  that snapshot's trades replayed in id order

so the diff engine, positions, exposure and chart markers see it exactly like an
uploaded file. Rebuilding from the predecessor (rather than editing in place)
keeps manual snapshots correct when a custodian file is later inserted before
them, and makes undo trivial. A custodian file uploaded for the same date
replaces the manual snapshot outright.

Cost math matches the diff engine's blended-cost identity: a BUY re-blends
avg_cost; a SELL leaves it unchanged; selling to zero closes the position.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.ingest.diff import rebuild_changes
from app.ingest.normalize import CASH_TICKER, normalize_ticker
from app.models import HoldingSnapshot, ManualTrade, ReviewLog, Security, Snapshot
from app.providers.fmp import FMPClient
from app.reviews import capture_price, parse_price

SIDES = ("BUY", "SELL")
MANUAL_FILENAME = "manual trade entry"
_Q = Decimal("0.000001")   # Money column scale


def fmt_units(x) -> str:
    """100.000000 -> '100', 12.5 -> '12.5', with thousands separators."""
    return format(Decimal(x).normalize(), ",f")


class TradeError(ValueError):
    """User-facing validation failure."""


@dataclass
class Lot:
    raw_ticker: str
    units: Decimal
    avg_cost: Decimal


@dataclass
class TradeResult:
    trade: ManualTrade
    snapshot: Snapshot
    units_before: Decimal
    units_after: Decimal
    avg_cost_after: Decimal | None
    cash_after: Decimal | None


def parse_units(raw: str | None) -> Decimal | str:
    s = (raw or "").strip().replace(",", "").lower()
    if s == "all":
        return "all"
    try:
        u = Decimal(s)
    except InvalidOperation as exc:
        raise TradeError(f"Units '{raw}' is not a number.") from exc
    if u <= 0:
        raise TradeError("Units must be positive.")
    return u


# ---------------------------------------------------------------------------
# Pure replay
# ---------------------------------------------------------------------------


def apply_trade(book: dict[str, Lot], t: ManualTrade, *, strict: bool = True) -> None:
    """Apply one trade to `book` in place. strict=False (used when an upload
    reshuffles the series) clamps an oversized sell instead of raising."""
    units, price = Decimal(t.units), Decimal(t.price)
    lot = book.get(t.ticker)
    if t.side == "BUY":
        if lot is None:
            book[t.ticker] = Lot(t.raw_ticker, units, price)
        else:
            new_units = lot.units + units
            lot.avg_cost = ((lot.units * lot.avg_cost + units * price) / new_units).quantize(_Q)
            lot.units = new_units
        cash_delta = -units * price
    else:
        held = lot.units if lot else Decimal(0)
        if units > held:
            if strict:
                raise TradeError(f"Cannot sell {fmt_units(units)} {t.ticker}: only {fmt_units(held)} held.")
            units = held
        if lot is not None:
            lot.units = held - units
            if lot.units == 0:
                del book[t.ticker]
        cash_delta = units * price

    if t.adjust_cash and cash_delta:
        cash = book.get(CASH_TICKER)
        if cash is None:
            book[CASH_TICKER] = Lot(CASH_TICKER, cash_delta.quantize(_Q), Decimal(1))
        else:
            cash.units = (cash.units + cash_delta).quantize(_Q)


def _load_book(session: Session, snapshot_id: int) -> dict[str, Lot]:
    rows = session.execute(
        select(HoldingSnapshot).where(HoldingSnapshot.snapshot_id == snapshot_id)
    ).scalars().all()
    return {h.ticker: Lot(h.raw_ticker, Decimal(h.units), Decimal(h.avg_cost)) for h in rows}


def _trades_of(session: Session, snapshot_id: int) -> list[ManualTrade]:
    return list(session.execute(
        select(ManualTrade).where(ManualTrade.snapshot_id == snapshot_id).order_by(ManualTrade.id)
    ).scalars().all())


def _previous(session: Session, snap: Snapshot) -> Snapshot | None:
    return session.execute(
        select(Snapshot).where(Snapshot.as_of < snap.as_of)
        .order_by(Snapshot.as_of.desc()).limit(1)
    ).scalar_one_or_none()


def _rebuild_one(session: Session, snap: Snapshot, *, strict: bool) -> dict[str, Lot]:
    prev = _previous(session, snap)
    book = _load_book(session, prev.id) if prev else {}
    for t in _trades_of(session, snap.id):
        apply_trade(book, t, strict=strict)
    session.execute(delete(HoldingSnapshot).where(HoldingSnapshot.snapshot_id == snap.id))
    session.expire(snap, ["holdings"])   # the bulk delete bypassed the loaded collection
    for ticker, lot in book.items():
        session.add(HoldingSnapshot(snapshot_id=snap.id, ticker=ticker, raw_ticker=lot.raw_ticker,
                                    units=lot.units, avg_cost=lot.avg_cost))
    session.flush()
    return book


def rebuild_manual_snapshots(session: Session) -> None:
    """Re-derive every manual snapshot from its predecessor, oldest first. Called
    after an upload so a custodian file inserted earlier in the series flows
    through to the manual snapshots after it."""
    manual = session.execute(
        select(Snapshot).where(Snapshot.source == "manual").order_by(Snapshot.as_of)
    ).scalars().all()
    for snap in manual:
        _rebuild_one(session, snap, strict=False)


# ---------------------------------------------------------------------------
# Record / undo
# ---------------------------------------------------------------------------


def record_trade(session: Session, ticker: str, *, side: str, units_raw: str,
                 price_raw: str | None, trade_date: date, adjust_cash: bool = True,
                 note: str | None = None, client: FMPClient | None = None) -> TradeResult:
    side = (side or "").strip().upper()
    if side not in SIDES:
        raise TradeError("Choose Buy or Sell.")
    ticker = ticker.strip().upper()
    if not ticker or ticker == CASH_TICKER.upper():
        raise TradeError("Not a tradable ticker.")

    latest = session.execute(
        select(Snapshot).order_by(Snapshot.as_of.desc()).limit(1)
    ).scalar_one_or_none()
    if latest is None:
        raise TradeError("Upload a custodian file first — trades apply on top of it.")
    if trade_date < latest.as_of:
        raise TradeError(f"Trade date must be on or after the latest snapshot ({latest.as_of}). "
                         "Earlier trades are already reflected in the custodian file.")
    if trade_date == latest.as_of and latest.source != "manual":
        raise TradeError(f"The custodian file is dated {latest.as_of}; a trade on the same day "
                         "would overwrite it. Date the trade the next day or later.")

    units = parse_units(units_raw)
    base_book = _load_book(session, latest.id)
    held = base_book[ticker].units if ticker in base_book else Decimal(0)
    if units == "all":
        if side != "SELL" or held == 0:
            raise TradeError("'all' only works for selling a held position.")
        units = held

    price = parse_price(price_raw)
    if price is not None:
        price_source = "manual"
    else:
        price, price_source, _ = capture_price(session, ticker, client=client)
        if price is None:
            raise TradeError("No live price available — enter the trade price.")

    if trade_date == latest.as_of:          # another trade on today's manual snapshot
        snap = latest
    else:
        snap = Snapshot(as_of=trade_date, filename=MANUAL_FILENAME, source="manual")
        session.add(snap)
        session.flush()

    sec = session.get(Security, ticker)
    raw_ticker = (base_book[ticker].raw_ticker if ticker in base_book
                  else f"{ticker} {sec.exchange}" if sec and sec.exchange else f"{ticker} US")
    trade = ManualTrade(snapshot_id=snap.id, ticker=ticker, raw_ticker=raw_ticker, side=side,
                        units=units, price=price, price_source=price_source,
                        adjust_cash=adjust_cash, note=(note or "").strip() or None)
    session.add(trade)
    session.flush()

    try:
        book = _rebuild_one(session, snap, strict=True)
    except TradeError:
        session.rollback()
        raise

    if sec is None:
        _, exchange = normalize_ticker(raw_ticker)
        session.add(Security(ticker=ticker, exchange=exchange, first_seen=trade_date, active=True))
    else:
        sec.active = ticker in book
        if sec.first_seen is None:
            sec.first_seen = trade_date

    after = book.get(ticker)
    if trade.note:
        stance = "ADD" if side == "BUY" else ("EXIT" if after is None else "TRIM")
        session.add(ReviewLog(ticker=ticker, review_date=trade_date, price=price,
                              price_source=price_source, stance=stance,
                              note=f"{side.title()} {fmt_units(units)} @ {price:,.2f} — {trade.note}"))

    snap.notes = _summary(session, snap.id)
    rebuild_changes(session, commit=False)
    from app.attribution import safe_rebuild
    safe_rebuild(session)
    session.commit()
    cash = book.get(CASH_TICKER)
    return TradeResult(trade=trade, snapshot=snap, units_before=held,
                       units_after=after.units if after else Decimal(0),
                       avg_cost_after=after.avg_cost if after else None,
                       cash_after=cash.units if cash else None)


def undo_trade(session: Session, ticker: str, trade_id: int) -> None:
    trade = session.get(ManualTrade, trade_id)
    if trade is None or trade.ticker != ticker.upper():
        raise TradeError("Trade not found.")
    snap = session.get(Snapshot, trade.snapshot_id)
    newer = session.execute(
        select(func.count()).select_from(Snapshot).where(Snapshot.as_of > snap.as_of)
    ).scalar_one()
    if newer:
        raise TradeError("Only trades on the latest snapshot can be undone — "
                         "a newer snapshot already builds on this one.")
    session.delete(trade)
    session.flush()
    if not _trades_of(session, snap.id):
        session.delete(snap)
        session.flush()
    else:
        _rebuild_one(session, snap, strict=True)
        snap.notes = _summary(session, snap.id)
    rebuild_changes(session, commit=False)
    from app.attribution import safe_rebuild
    safe_rebuild(session)
    session.commit()


def _summary(session: Session, snapshot_id: int) -> str:
    return "; ".join(f"{t.side} {fmt_units(t.units)} {t.ticker} @ {Decimal(t.price):,.2f}"
                     for t in _trades_of(session, snapshot_id))


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


@dataclass
class TradeRow:
    trade: ManualTrade
    as_of: date
    undoable: bool


def list_trades(session: Session, ticker: str) -> list[TradeRow]:
    latest_as_of = session.execute(select(func.max(Snapshot.as_of))).scalar_one_or_none()
    rows = session.execute(
        select(ManualTrade, Snapshot.as_of).join(Snapshot, Snapshot.id == ManualTrade.snapshot_id)
        .where(ManualTrade.ticker == ticker)
        .order_by(Snapshot.as_of.desc(), ManualTrade.id.desc())
    ).all()
    return [TradeRow(trade=t, as_of=d, undoable=(d == latest_as_of)) for t, d in rows]


@dataclass
class Provenance:
    """Where the current holdings come from, for the freshness banners: the age
    that matters is the last CUSTODIAN file, not a manual snapshot on top."""
    custodian_as_of: date | None
    manual_trades: int


def provenance(session: Session) -> Provenance:
    custodian = session.execute(
        select(func.max(Snapshot.as_of)).where(Snapshot.source == "upload")
    ).scalar_one_or_none()
    q = select(func.count()).select_from(ManualTrade).join(Snapshot)
    if custodian is not None:
        q = q.where(Snapshot.as_of > custodian)
    return Provenance(custodian_as_of=custodian, manual_trades=session.execute(q).scalar_one())
