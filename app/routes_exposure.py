"""Exposure & concentration view (Phase 5). Reads the price cache via build_exposure."""

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


@router.get("/exposure", response_class=HTMLResponse)
def exposure_view(request: Request, session: Session = Depends(get_session)):
    view = build_exposure(session)
    return templates.TemplateResponse(request, "exposure.html", {"view": view})
