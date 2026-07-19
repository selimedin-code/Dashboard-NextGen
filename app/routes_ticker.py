"""Ticker detail page (Phase 4): position story + cached company data.

GET reads cache only. A per-ticker Refresh action fetches fundamentals into the
cache; the thesis note is editable and saved to the security."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_session
from app.fundamentals import refresh_ticker
from app.models import Security
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
    session: Session = Depends(get_session),
):
    detail = get_ticker_detail(session, ticker.upper())
    return templates.TemplateResponse(
        request, "ticker.html", {"d": detail, "refreshed": refreshed, "saved": saved}
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
