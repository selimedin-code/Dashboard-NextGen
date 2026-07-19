"""Position table (Phase 3): the main screen.

GET /positions reads from the price cache only (never blocks on the API).
POST /positions/refresh fetches live quotes into the cache, then redirects back.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_session
from app.positions import build_positions
from app.prices import refresh_quotes

router = APIRouter(dependencies=[Depends(require_auth)])
templates: Jinja2Templates = None


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


@router.get("/positions", response_class=HTMLResponse)
def positions_view(
    request: Request,
    refreshed: str | None = None,
    session: Session = Depends(get_session),
):
    view = build_positions(session)
    return templates.TemplateResponse(
        request, "positions.html", {"view": view, "refreshed": refreshed}
    )


@router.post("/positions/refresh")
def positions_refresh(session: Session = Depends(get_session)):
    try:
        summary = refresh_quotes(session)
        msg = f"{summary['ok']} priced"
        if summary["unpriceable"]:
            msg += f", {summary['unpriceable']} unpriceable"
        if summary["failed"]:
            msg += f", {summary['failed']} failed"
    except Exception as exc:  # noqa: BLE001 — surface the failure to the page
        msg = f"error: {exc}"
    return RedirectResponse(url=f"/positions?refreshed={msg}", status_code=status.HTTP_303_SEE_OTHER)
