"""Performance view (Phase 6): fund NAV vs benchmarks + position contribution.

GET reads cache. NAV points are managed here (add/delete/CSV); a Refresh pulls
the benchmark price history."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_session
from app.performance import (
    add_nav_point,
    build_performance,
    delete_nav_point,
    parse_nav_csv,
    refresh_benchmarks,
)

router = APIRouter(dependencies=[Depends(require_auth)])
templates: Jinja2Templates = None


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


@router.get("/performance", response_class=HTMLResponse)
def performance_view(
    request: Request,
    msg: str | None = None,
    session: Session = Depends(get_session),
):
    view = build_performance(session)
    return templates.TemplateResponse(request, "performance.html", {"view": view, "msg": msg})


@router.post("/performance/nav")
def nav_add(
    as_of: str = Form(...),
    nav_per_share: str = Form(...),
    total_nav: str = Form(""),
    shares: str = Form(""),
    session: Session = Depends(get_session),
):
    def dec(s):
        s = (s or "").strip().replace(",", "").replace("'", "")
        try:
            return Decimal(s) if s else None
        except InvalidOperation:
            return None

    try:
        d = date.fromisoformat(as_of)
        nav = dec(nav_per_share)
    except ValueError:
        return RedirectResponse(url="/performance?msg=invalid date", status_code=303)
    if nav is None or nav <= 0:
        return RedirectResponse(url="/performance?msg=invalid NAV", status_code=303)
    add_nav_point(session, d, nav, total_nav=dec(total_nav), shares=dec(shares))
    return RedirectResponse(url=f"/performance?msg=NAV {d} saved", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/performance/nav/delete")
def nav_delete(as_of: str = Form(...), session: Session = Depends(get_session)):
    try:
        delete_nav_point(session, date.fromisoformat(as_of))
    except ValueError:
        pass
    return RedirectResponse(url="/performance?msg=NAV point removed", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/performance/nav/upload")
async def nav_upload(file: UploadFile, session: Session = Depends(get_session)):
    data = await file.read()
    points = parse_nav_csv(data)
    for d, nav in points:
        add_nav_point(session, d, nav)
    return RedirectResponse(url=f"/performance?msg={len(points)} NAV points imported",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.post("/performance/benchmarks/refresh")
def benchmarks_refresh(session: Session = Depends(get_session)):
    try:
        st = refresh_benchmarks(session)
        msg = "benchmarks: " + ", ".join(f"{k} {v}" for k, v in st.items())
    except Exception as exc:  # noqa: BLE001
        msg = f"error: {exc}"
    return RedirectResponse(url=f"/performance?msg={msg}", status_code=status.HTTP_303_SEE_OTHER)
