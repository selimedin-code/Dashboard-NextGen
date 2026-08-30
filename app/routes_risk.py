"""Risk page — the fund's factor map, stress scenario, and tripwires.

Zone 2: effective bets + capex air-pocket scenario (via build_exposure).
Zone 3: the written exit rules (via build_tripwires), with inline editing of
manual/semi readings. Macro gauges (Zone 1) follow.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_session
from app.exposure import build_exposure
from app.tripwires import build_tripwires, update_tripwire

router = APIRouter(dependencies=[Depends(require_auth)])
templates: Jinja2Templates = None


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


@router.get("/risk", response_class=HTMLResponse)
def risk_view(
    request: Request,
    saved: str | None = None,
    session: Session = Depends(get_session),
):
    view = build_exposure(session)
    wires = build_tripwires(session)
    return templates.TemplateResponse(
        request, "risk.html", {"view": view, "wires": wires, "saved": saved}
    )


@router.post("/risk/tripwire/{wire_id}")
def tripwire_update(
    wire_id: int,
    status: str = Form(...),
    latest_value: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_session),
):
    ok = update_tripwire(
        session, wire_id, status=status, latest_value=latest_value, note=note
    )
    if ok:
        session.commit()
    return RedirectResponse(
        url=f"/risk?saved={'1' if ok else 'err'}#tripwires", status_code=303
    )
