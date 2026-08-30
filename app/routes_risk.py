"""Risk page — the fund's factor map and stress scenario (Section-3 view).

Zone 2 ships first: effective bets, the AI-capex-complex aggregate, and the
capex air-pocket scenario, all derived from the priced positions via
build_exposure. Zones for macro gauges and name-level tripwires follow.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_session
from app.exposure import build_exposure

router = APIRouter(dependencies=[Depends(require_auth)])
templates: Jinja2Templates = None


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


@router.get("/risk", response_class=HTMLResponse)
def risk_view(request: Request, session: Session = Depends(get_session)):
    view = build_exposure(session)
    return templates.TemplateResponse(request, "risk.html", {"view": view})
