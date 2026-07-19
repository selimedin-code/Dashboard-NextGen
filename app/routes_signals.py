"""Signals view (Phase 7). Reads cache; a Refresh pulls fundamentals for every
held ticker so the trend/earnings/news flags cover the whole book."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_session
from app.fundamentals import refresh_all_fundamentals
from app.signals import build_signals

router = APIRouter(dependencies=[Depends(require_auth)])
templates: Jinja2Templates = None


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


@router.get("/signals", response_class=HTMLResponse)
def signals_view(request: Request, msg: str | None = None, session: Session = Depends(get_session)):
    view = build_signals(session)
    return templates.TemplateResponse(request, "signals.html", {"view": view, "msg": msg})


@router.post("/signals/refresh-fundamentals")
def signals_refresh(session: Session = Depends(get_session)):
    try:
        s = refresh_all_fundamentals(session)
        msg = f"fundamentals: {s['ok']}/{s['total']} ok" + (f", {s['failed']} failed" if s["failed"] else "")
    except Exception as exc:  # noqa: BLE001
        msg = f"error: {exc}"
    return RedirectResponse(url=f"/signals?msg={msg}", status_code=status.HTTP_303_SEE_OTHER)
