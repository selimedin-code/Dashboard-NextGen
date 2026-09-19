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

# The forced verdict once a horizon passes. "Right for the wrong reason" is its
# own bucket: it scores as luck, not skill.
VERDICTS = {
    "RIGHT_RIGHT": "Right, right reason",
    "RIGHT_WRONG": "Right, wrong reason",
    "WRONG": "Wrong",
}

MB = 1024 * 1024
# Report PDFs from the Claude skills run 35-45 MB. The whole upload is held in
# memory on a 512 MB Render instance, so cap the request total as well.
MAX_FILE_BYTES = 75 * MB
MAX_REQUEST_BYTES = 150 * MB
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
            raise ReviewError(f"{name}: larger than {MAX_FILE_BYTES // MB} MB.")
        out.append(UploadedFile(name, f.content_type, f.data))
    if sum(len(f.data) for f in out) > MAX_REQUEST_BYTES:
        raise ReviewError(f"Files total more than {MAX_REQUEST_BYTES // MB} MB — attach them in two goes.")
    return out


def _attach(review: ReviewLog, files: list[UploadedFile]) -> None:
    for f in files:
        review.attachments.append(ReviewAttachment(
            filename=f.filename, content_type=f.content_type,
            size_bytes=len(f.data), data=f.data,
        ))


def create_review(session: Session, ticker: str, *, note: str, price_raw: str | None,
                  stance: str | None, review_date: date | None,
                  files: list[UploadedFile], client: FMPClient | None = None,
                  expected_outcome: str | None = None, horizon_date: date | None = None,
                  invalidation: str | None = None) -> ReviewLog:
    note = (note or "").strip()
    files = validate_files(files)
    if not note and not files:
        raise ReviewError("Write a note or attach a file.")
    stance = (stance or "").strip().upper() or None
    if stance is not None and stance not in STANCES:
        raise ReviewError(f"Unknown stance '{stance}'.")
    expected_outcome = (expected_outcome or "").strip() or None
    invalidation = (invalidation or "").strip() or None
    if bool(expected_outcome) != (horizon_date is not None):
        raise ReviewError("An expected outcome needs a horizon date, and a horizon needs an "
                          "expected outcome — that pair is what gets scored later.")
    if horizon_date is not None and horizon_date <= (review_date or date.today()):
        raise ReviewError("The horizon must be after the review date.")

    price = parse_price(price_raw)
    if price is not None:
        source, as_of = "manual", None
    else:
        price, source, as_of = capture_price(session, ticker, client=client)

    review = ReviewLog(
        ticker=ticker, review_date=review_date or date.today(), note=note,
        stance=stance, price=price, price_source=source, price_as_of=as_of,
        expected_outcome=expected_outcome, horizon_date=horizon_date, invalidation=invalidation,
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


# ---------------------------------------------------------------------------
# Closed loop: reviews whose horizon has passed must be scored.
# ---------------------------------------------------------------------------


@dataclass
class DueRow:
    review: ReviewLog
    current_price: Decimal | None
    since_pct: Decimal | None
    days_overdue: int


def score_review(session: Session, review_id: int, verdict: str, note: str | None = None) -> ReviewLog:
    review = session.get(ReviewLog, review_id)
    if review is None:
        raise ReviewError("Review not found.")
    verdict = (verdict or "").strip().upper()
    if verdict not in VERDICTS:
        raise ReviewError("Pick a verdict.")
    if review.horizon_date is None:
        raise ReviewError("This review has no expected outcome to score.")
    review.verdict = verdict
    review.verdict_note = (note or "").strip() or None
    review.scored_at = datetime.now(timezone.utc)
    session.commit()
    return review


def unscore_review(session: Session, review_id: int) -> None:
    review = session.get(ReviewLog, review_id)
    if review is None:
        raise ReviewError("Review not found.")
    review.verdict = review.verdict_note = review.scored_at = None
    session.commit()


def _with_prices(session: Session, reviews, today: date) -> list[DueRow]:
    quotes = {q.ticker: q for q in session.execute(select(QuoteCache)).scalars().all()}
    rows = []
    for r in reviews:
        q = quotes.get(r.ticker)
        cur = Decimal(q.price) if q is not None and q.ok and q.price is not None else None
        since = (cur / Decimal(r.price) - 1) if (cur and r.price) else None
        overdue = (today - r.horizon_date).days if r.horizon_date else 0
        rows.append(DueRow(review=r, current_price=cur, since_pct=since, days_overdue=overdue))
    return rows


def due_count(session: Session, today: date | None = None) -> int:
    from sqlalchemy import func
    today = today or date.today()
    return session.execute(select(func.count()).select_from(ReviewLog).where(
        ReviewLog.horizon_date <= today, ReviewLog.scored_at.is_(None))).scalar_one()


@dataclass
class JournalView:
    due: list[DueRow]
    upcoming: list[DueRow]
    scored: list[DueRow]
    scorecard: dict


def build_journal(session: Session, today: date | None = None) -> JournalView:
    today = today or date.today()
    base = select(ReviewLog).where(ReviewLog.horizon_date.is_not(None))
    due = session.execute(base.where(ReviewLog.horizon_date <= today, ReviewLog.scored_at.is_(None))
                          .order_by(ReviewLog.horizon_date, ReviewLog.id)).scalars().all()
    upcoming = session.execute(base.where(ReviewLog.horizon_date > today, ReviewLog.scored_at.is_(None))
                               .order_by(ReviewLog.horizon_date, ReviewLog.id)).scalars().all()
    scored = session.execute(base.where(ReviewLog.scored_at.is_not(None))
                             .order_by(ReviewLog.scored_at.desc())).scalars().all()

    counts = {k: 0 for k in VERDICTS}
    by_stance: dict[str, dict[str, int]] = {}
    for r in scored:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
        st = by_stance.setdefault(r.stance or "—", {k: 0 for k in VERDICTS})
        st[r.verdict] = st.get(r.verdict, 0) + 1
    n = len(scored)
    scorecard = {
        "n": n, "counts": counts,
        "skill_rate": (Decimal(counts["RIGHT_RIGHT"]) / n) if n else None,
        "hit_rate": (Decimal(counts["RIGHT_RIGHT"] + counts["RIGHT_WRONG"]) / n) if n else None,
        "by_stance": by_stance,
    }
    return JournalView(due=_with_prices(session, due, today),
                       upcoming=_with_prices(session, upcoming, today),
                       scored=_with_prices(session, scored, today), scorecard=scorecard)
