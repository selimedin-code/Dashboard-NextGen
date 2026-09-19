"""Review log: dated notes per ticker, each stamped with the price at the time,
with attached reports (Excel models, PDF write-ups, decks).

Price capture order when a review is written: a price typed by hand wins; else a
live FMP quote (which also refreshes quote_cache); else the cached quote. The
source is recorded so a stale fallback is never mistaken for a live price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import PurePath

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import QuoteCache, ReviewAttachment, ReviewLog, Security
from app.prices import _upsert as _upsert_quote
from app.prices import resolve_fmp_symbol
from app.providers.fmp import FMPClient

STANCES = ("ADD", "HOLD", "TRIM", "EXIT", "WATCH")

MAX_FILE_BYTES = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    ".pdf", ".xlsx", ".xlsm", ".xls", ".csv",
    ".docx", ".doc", ".pptx", ".md", ".txt", ".html", ".htm",
    ".png", ".jpg", ".jpeg",
}


class ReviewError(ValueError):
    """User-facing validation failure (bad price, disallowed file, too large)."""


@dataclass
class UploadedFile:
    filename: str
    content_type: str | None
    data: bytes


@dataclass
class ReviewRow:
    review: ReviewLog
    since_pct: Decimal | None     # move from the review price to the current price


def parse_price(raw: str | None) -> Decimal | None:
    s = (raw or "").strip().replace(",", "").lstrip("$")
    if not s:
        return None
    try:
        p = Decimal(s)
    except InvalidOperation as exc:
        raise ReviewError(f"Price '{raw}' is not a number.") from exc
    if p <= 0:
        raise ReviewError("Price must be positive.")
    return p


def capture_price(session: Session, ticker: str, *, client: FMPClient | None = None
                  ) -> tuple[Decimal | None, str | None, datetime | None]:
    """(price, source, as_of) for `ticker` right now: live quote, else cached."""
    sec = session.get(Security, ticker)
    symbol = resolve_fmp_symbol(ticker, sec.exchange if sec else None)
    key = get_settings().fmp_api_key
    if symbol and (client is not None or key):
        owns = client is None
        if owns:
            client = FMPClient(key, max_retries=1)
        try:
            q = client.get_quote(symbol)
            if q.price is not None:
                _upsert_quote(session, ticker, symbol, q)
                return Decimal(q.price), "live", datetime.now(timezone.utc)
        except Exception:  # noqa: BLE001 — fall back to the cache below
            pass
        finally:
            if owns:
                client.close()
    cached = session.get(QuoteCache, ticker)
    if cached is not None and cached.ok and cached.price is not None:
        return Decimal(cached.price), "cached", cached.fetched_at
    return None, None, None


def validate_files(files: list[UploadedFile]) -> list[UploadedFile]:
    out = []
    for f in files:
        if not f.filename or not f.data:
            continue  # the empty part a browser sends when no file was chosen
        name = PurePath(f.filename).name
        ext = PurePath(name).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ReviewError(f"{name}: file type {ext or '(none)'} not allowed.")
        if len(f.data) > MAX_FILE_BYTES:
            raise ReviewError(f"{name}: larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        out.append(UploadedFile(name, f.content_type, f.data))
    return out


def _attach(review: ReviewLog, files: list[UploadedFile]) -> None:
    for f in files:
        review.attachments.append(ReviewAttachment(
            filename=f.filename, content_type=f.content_type,
            size_bytes=len(f.data), data=f.data,
        ))


def create_review(session: Session, ticker: str, *, note: str, price_raw: str | None,
                  stance: str | None, review_date: date | None,
                  files: list[UploadedFile], client: FMPClient | None = None) -> ReviewLog:
    note = (note or "").strip()
    files = validate_files(files)
    if not note and not files:
        raise ReviewError("Write a note or attach a file.")
    stance = (stance or "").strip().upper() or None
    if stance is not None and stance not in STANCES:
        raise ReviewError(f"Unknown stance '{stance}'.")

    price = parse_price(price_raw)
    if price is not None:
        source, as_of = "manual", None
    else:
        price, source, as_of = capture_price(session, ticker, client=client)

    review = ReviewLog(
        ticker=ticker, review_date=review_date or date.today(), note=note,
        stance=stance, price=price, price_source=source, price_as_of=as_of,
    )
    _attach(review, files)
    session.add(review)
    session.commit()
    return review


def add_attachments(session: Session, ticker: str, review_id: int,
                    files: list[UploadedFile]) -> ReviewLog:
    review = _get(session, ticker, review_id)
    files = validate_files(files)
    if not files:
        raise ReviewError("Choose at least one file.")
    _attach(review, files)
    session.commit()
    return review


def delete_review(session: Session, ticker: str, review_id: int) -> None:
    session.delete(_get(session, ticker, review_id))
    session.commit()


def delete_attachment(session: Session, ticker: str, attachment_id: int) -> None:
    att = get_attachment(session, ticker, attachment_id)
    if att is None:
        raise ReviewError("Attachment not found.")
    session.delete(att)
    session.commit()


def get_attachment(session: Session, ticker: str, attachment_id: int) -> ReviewAttachment | None:
    return session.execute(
        select(ReviewAttachment).join(ReviewLog)
        .where(ReviewAttachment.id == attachment_id, ReviewLog.ticker == ticker)
    ).scalar_one_or_none()


def _get(session: Session, ticker: str, review_id: int) -> ReviewLog:
    review = session.get(ReviewLog, review_id)
    if review is None or review.ticker != ticker:
        raise ReviewError("Review not found.")
    return review


def list_reviews(session: Session, ticker: str, current_price: Decimal | None) -> list[ReviewRow]:
    reviews = session.execute(
        select(ReviewLog).where(ReviewLog.ticker == ticker)
        .order_by(ReviewLog.review_date.desc(), ReviewLog.id.desc())
    ).scalars().all()
    rows = []
    for r in reviews:
        since = None
        if current_price and r.price:
            since = Decimal(current_price) / Decimal(r.price) - 1
        rows.append(ReviewRow(review=r, since_pct=since))
    return rows
