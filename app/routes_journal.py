"""Journal: review entries whose horizon has passed must be scored.

The dashboard knows the entry price and the current price; what it can't do is
decide whether the call was right for the right reason. This page forces that
verdict and keeps the running scorecard."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import reviews as rv
from app.auth import require_auth
from app.db import get_session

router = APIRouter(dependencies=[Depends(require_auth)])
templates: Jinja2Templates = None


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


@router.get("/reviews/due", response_class=HTMLResponse)
def reviews_due(request: Request, msg: str | None = None, err: str | None = None,
                session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "reviews_due.html", {
        "j": rv.build_journal(session), "verdicts": rv.VERDICTS, "msg": msg, "err": err,
    })


def _back(ok: str | None = None, err: str | None = None) -> RedirectResponse:
    q = f"msg={quote(ok)}" if ok else f"err={quote(err or '')}"
    return RedirectResponse(url=f"/reviews/due?{q}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/reviews/{review_id}/score")
def review_score(review_id: int, verdict: str = Form(""), note: str = Form(""),
                 session: Session = Depends(get_session)):
    try:
        r = rv.score_review(session, review_id, verdict, note)
    except rv.ReviewError as exc:
        session.rollback()
        return _back(err=str(exc))
    return _back(ok=f"{r.ticker} ({r.review_date}) scored: {rv.VERDICTS[r.verdict]}.")


@router.post("/reviews/{review_id}/unscore")
def review_unscore(review_id: int, session: Session = Depends(get_session)):
    try:
        rv.unscore_review(session, review_id)
    except rv.ReviewError as exc:
        return _back(err=str(exc))
    return _back(ok="Verdict cleared — the entry is back in the due list.")
