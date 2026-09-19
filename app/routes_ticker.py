"""Ticker detail page (Phase 4): position story + cached company data.

GET reads cache only. A per-ticker Refresh action fetches fundamentals into the
cache; the thesis note is editable and saved to the security."""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_session
from app.fundamentals import refresh_ticker
from app.models import Security
from app import reviews as rv
from app import trades as tr
from app.ticker_detail import get_ticker_detail

router = APIRouter(dependencies=[Depends(require_auth)])
templates: Jinja2Templates = None


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


@router.get("/ticker/{ticker}", response_class=HTMLResponse)
def ticker_view(
    request: Request,
    ticker: str,
    refreshed: str | None = None,
    saved: str | None = None,
    review: str | None = None,
    review_err: str | None = None,
    trade: str | None = None,
    trade_err: str | None = None,
    session: Session = Depends(get_session),
):
    detail = get_ticker_detail(session, ticker.upper())
    return templates.TemplateResponse(
        request, "ticker.html",
        {"d": detail, "refreshed": refreshed, "saved": saved,
         "review": review, "review_err": review_err, "trade": trade, "trade_err": trade_err,
         "today": date.today().isoformat()},
    )


@router.post("/ticker/{ticker}/refresh")
def ticker_refresh(ticker: str, session: Session = Depends(get_session)):
    ticker = ticker.upper()
    try:
        status_map = refresh_ticker(session, ticker)
        ok = sum(1 for v in status_map.values() if v == "ok")
        msg = f"{ok}/{len(status_map)} sources ok"
    except Exception as exc:  # noqa: BLE001
        msg = f"error: {exc}"
    return RedirectResponse(url=f"/ticker/{ticker}?refreshed={msg}",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ticker/{ticker}/note")
def ticker_note(ticker: str, thesis_note: str = Form(""), session: Session = Depends(get_session)):
    ticker = ticker.upper()
    sec = session.get(Security, ticker)
    if sec is None:
        sec = Security(ticker=ticker, active=False)
        session.add(sec)
    sec.thesis_note = thesis_note.strip() or None
    session.commit()
    return RedirectResponse(url=f"/ticker/{ticker}?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ticker/{ticker}/stop")
def ticker_stop(ticker: str, stop_price: str = Form(""), session: Session = Depends(get_session)):
    from decimal import Decimal, InvalidOperation

    ticker = ticker.upper()
    sec = session.get(Security, ticker)
    if sec is None:
        sec = Security(ticker=ticker, active=False)
        session.add(sec)
    s = (stop_price or "").strip().replace(",", "")
    try:
        sec.stop_price = Decimal(s) if s else None
    except InvalidOperation:
        sec.stop_price = None
    session.commit()
    return RedirectResponse(url=f"/ticker/{ticker}?saved=1", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Review log
# ---------------------------------------------------------------------------


def _back(ticker: str, *, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    q = f"review={quote(ok)}" if ok else f"review_err={quote(err or '')}"
    return RedirectResponse(url=f"/ticker/{ticker}?{q}#reviews",
                            status_code=status.HTTP_303_SEE_OTHER)


async def _read(files: list[UploadFile]) -> list[rv.UploadedFile]:
    return [rv.UploadedFile(f.filename or "", f.content_type, await f.read()) for f in files]


@router.post("/ticker/{ticker}/reviews")
async def review_create(
    ticker: str,
    note: str = Form(""),
    price: str = Form(""),
    stance: str = Form(""),
    review_date: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    session: Session = Depends(get_session),
):
    ticker = ticker.upper()
    try:
        d = date.fromisoformat(review_date) if review_date.strip() else None
    except ValueError:
        return _back(ticker, err=f"Bad date '{review_date}'.")
    try:
        r = rv.create_review(session, ticker, note=note, price_raw=price, stance=stance,
                             review_date=d, files=await _read(files))
    except rv.ReviewError as exc:
        session.rollback()
        return _back(ticker, err=str(exc))
    if r.price is None:
        return _back(ticker, ok="Review saved — no price available (enter one by hand next time).")
    return _back(ticker, ok=f"Review saved at {r.price:,.2f} ({r.price_source}).")


@router.post("/ticker/{ticker}/reviews/{review_id}/attach")
async def review_attach(ticker: str, review_id: int,
                        files: list[UploadFile] = File(default=[]),
                        session: Session = Depends(get_session)):
    ticker = ticker.upper()
    try:
        r = rv.add_attachments(session, ticker, review_id, await _read(files))
    except rv.ReviewError as exc:
        session.rollback()
        return _back(ticker, err=str(exc))
    return _back(ticker, ok=f"Attached to review of {r.review_date}.")


@router.post("/ticker/{ticker}/reviews/{review_id}/delete")
def review_delete(ticker: str, review_id: int, session: Session = Depends(get_session)):
    ticker = ticker.upper()
    try:
        rv.delete_review(session, ticker, review_id)
    except rv.ReviewError as exc:
        return _back(ticker, err=str(exc))
    return _back(ticker, ok="Review deleted.")


@router.post("/ticker/{ticker}/attachments/{attachment_id}/delete")
def attachment_delete(ticker: str, attachment_id: int, session: Session = Depends(get_session)):
    ticker = ticker.upper()
    try:
        rv.delete_attachment(session, ticker, attachment_id)
    except rv.ReviewError as exc:
        return _back(ticker, err=str(exc))
    return _back(ticker, ok="Attachment removed.")


@router.get("/ticker/{ticker}/attachments/{attachment_id}")
def attachment_download(ticker: str, attachment_id: int, session: Session = Depends(get_session)):
    att = rv.get_attachment(session, ticker.upper(), attachment_id)
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    # PDFs/images open in the browser tab; spreadsheets and docs download.
    ctype = att.content_type or "application/octet-stream"
    inline = ctype == "application/pdf" or ctype.startswith("image/")
    disp = "inline" if inline else "attachment"
    ascii_name = att.filename.encode("ascii", "replace").decode().replace('"', "")
    return Response(
        content=att.data, media_type=ctype,
        headers={"Content-Disposition":
                 f"{disp}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(att.filename)}"},
    )


# ---------------------------------------------------------------------------
# Single-ticker trades (manual snapshot on top of the custodian file)
# ---------------------------------------------------------------------------


def _trade_back(ticker: str, *, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    q = f"trade={quote(ok)}" if ok else f"trade_err={quote(err or '')}"
    return RedirectResponse(url=f"/ticker/{ticker}?{q}#trade",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ticker/{ticker}/trades")
def trade_record(
    ticker: str,
    side: str = Form(""),
    units: str = Form(""),
    price: str = Form(""),
    trade_date: str = Form(""),
    adjust_cash: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_session),
):
    ticker = ticker.upper()
    try:
        d = date.fromisoformat(trade_date) if trade_date.strip() else date.today()
    except ValueError:
        return _trade_back(ticker, err=f"Bad date '{trade_date}'.")
    try:
        r = tr.record_trade(session, ticker, side=side, units_raw=units, price_raw=price,
                            trade_date=d, adjust_cash=bool(adjust_cash), note=note)
    except (tr.TradeError, rv.ReviewError) as exc:
        session.rollback()
        return _trade_back(ticker, err=str(exc))
    t = r.trade
    msg = (f"{t.side.title()} {tr.fmt_units(t.units)} @ {t.price:,.2f} ({t.price_source}) "
           f"recorded for {d}. Units {tr.fmt_units(r.units_before)} → {tr.fmt_units(r.units_after)}")
    if r.avg_cost_after is not None:
        msg += f", avg cost {r.avg_cost_after:,.2f}"
    if t.adjust_cash and r.cash_after is not None:
        msg += f"; cash {r.cash_after:,.2f}"
    return _trade_back(ticker, ok=msg + ".")


@router.post("/ticker/{ticker}/trades/{trade_id}/undo")
def trade_undo(ticker: str, trade_id: int, session: Session = Depends(get_session)):
    ticker = ticker.upper()
    try:
        tr.undo_trade(session, ticker, trade_id)
    except tr.TradeError as exc:
        session.rollback()
        return _trade_back(ticker, err=str(exc))
    return _trade_back(ticker, ok="Trade undone.")
