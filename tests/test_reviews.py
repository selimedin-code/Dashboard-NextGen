"""Review log: price capture order, file validation, attachments, since-review move."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import reviews as rv
from app.models import QuoteCache, ReviewAttachment, ReviewLog
from app.providers.fmp import Quote
from app.ticker_detail import get_ticker_detail


class FakeClient:
    def __init__(self, price=None, fail=False):
        self.price, self.fail, self.calls = price, fail, []

    def get_quote(self, symbol):
        self.calls.append(symbol)
        if self.fail:
            raise RuntimeError("down")
        return Quote(symbol=symbol, price=Decimal(self.price), prev_close=None,
                     day_change_pct=None, currency="USD", name=None,
                     as_of=datetime.now(timezone.utc))


PDF = rv.UploadedFile("model.pdf", "application/pdf", b"%PDF-1.4 fake")
XLSX = rv.UploadedFile("dcf.xlsx", "application/vnd.ms-excel", b"PK fake")


def _create(session, client=None, **kw):
    args = dict(note="Thesis intact.", price_raw="", stance="", review_date=None, files=[])
    args.update(kw)
    return rv.create_review(session, "NVDA", client=client or FakeClient(fail=True), **args)


def test_manual_price_wins_and_skips_fetch(session):
    client = FakeClient(price="999")
    r = _create(session, client=client, price_raw="$1,234.50")
    assert r.price == Decimal("1234.50") and r.price_source == "manual"
    assert client.calls == []


def test_live_price_captured_and_cache_refreshed(session):
    r = _create(session, client=FakeClient(price="180.25"))
    assert r.price == Decimal("180.25") and r.price_source == "live"
    assert session.get(QuoteCache, "NVDA").price == Decimal("180.25")


def test_falls_back_to_cached_quote(session):
    session.add(QuoteCache(ticker="NVDA", price=Decimal("170"), ok=True))
    session.commit()
    r = _create(session, client=FakeClient(fail=True))
    assert r.price == Decimal("170") and r.price_source == "cached"


def test_no_price_anywhere_still_saves(session):
    r = _create(session)
    assert r.price is None and r.price_source is None


def test_requires_note_or_file(session):
    with pytest.raises(rv.ReviewError):
        _create(session, note="   ")
    r = _create(session, note="", files=[PDF])
    assert len(r.attachments) == 1


def test_rejects_bad_price_stance_and_file_type(session):
    with pytest.raises(rv.ReviewError):
        _create(session, price_raw="abc")
    with pytest.raises(rv.ReviewError):
        _create(session, stance="YOLO")
    with pytest.raises(rv.ReviewError):
        _create(session, files=[rv.UploadedFile("x.exe", None, b"MZ")])


def test_empty_browser_file_part_ignored(session):
    r = _create(session, files=[rv.UploadedFile("", "application/octet-stream", b"")])
    assert r.attachments == []


def test_attachments_add_download_scope_and_cascade(session):
    r = _create(session, files=[PDF], stance="hold")
    assert r.stance == "HOLD"
    rv.add_attachments(session, "NVDA", r.id, [XLSX])
    atts = session.execute(select(ReviewAttachment)).scalars().all()
    assert sorted(a.filename for a in atts) == ["dcf.xlsx", "model.pdf"]
    # An attachment is only reachable through its own ticker.
    assert rv.get_attachment(session, "AMD", atts[0].id) is None
    assert rv.get_attachment(session, "NVDA", atts[0].id).data in (PDF.data, XLSX.data)

    rv.delete_review(session, "NVDA", r.id)
    assert session.execute(select(ReviewAttachment)).scalars().all() == []
    with pytest.raises(rv.ReviewError):
        rv.delete_review(session, "NVDA", r.id)


def test_since_pct_and_ordering(session):
    _create(session, price_raw="100", review_date=date(2026, 8, 1), note="first")
    _create(session, price_raw="150", review_date=date(2026, 9, 1), note="second")
    rows = rv.list_reviews(session, "NVDA", Decimal("120"))
    assert [r.review.note for r in rows] == ["second", "first"]
    assert rows[0].since_pct == Decimal("-0.2")
    assert rows[1].since_pct == Decimal("0.2")


def test_ticker_detail_uses_cached_price_for_unheld_name(session):
    session.add(QuoteCache(ticker="NVDA", price=Decimal("200"), ok=True))
    session.commit()
    _create(session, price_raw="100")
    d = get_ticker_detail(session, "NVDA")
    assert d.current_price == Decimal("200")
    assert d.reviews[0].since_pct == Decimal("1")
    assert session.execute(select(ReviewLog)).scalar_one().ticker == "NVDA"
