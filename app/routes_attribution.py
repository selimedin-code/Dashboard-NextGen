"""Attribution page: is the edge in the wave tilts or in the names?

Brinson allocation / selection / interaction per snapshot period against the
policy benchmark (stored in attribution_period), rolled up to trailing 12
months and calendar quarters, plus the trading-alpha counterfactual (what the
discretionary sizing and timing between snapshots was worth)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.attribution import load_periods, rebuild_attribution, rollups
from app.auth import require_auth
from app.db import get_session
from app.performance import load_policy, trading_alpha
from app.risk_config import SNAPSHOT_STALE_DAYS

router = APIRouter(dependencies=[Depends(require_auth)])
templates: Jinja2Templates = None


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


@router.get("/attribution", response_class=HTMLResponse)
def attribution_view(
    request: Request,
    r: str = "t12m",
    p: int | None = None,
    msg: str | None = None,
    session: Session = Depends(get_session),
):
    policy = load_policy(session)
    periods = load_periods(session)
    rus = rollups(periods)
    ru = next((x for x in rus if x.key == r), rus[0])

    trading = trading_alpha(session)
    t12 = {x.to_id for x in rus[0].periods} if rus else set()
    recent = [t for t in trading if t.to_id in t12] if t12 else trading[-12:]
    tsum = {
        "discretionary": sum((t.discretionary for t in recent), 0),
        "flow": sum((t.flow for t in recent), 0),
        "ambiguous": sum((t.ambiguous for t in recent), 0),
        "n": len(recent),
        "wide": sum(1 for t in recent if t.wide),
    }
    sel = next((t for t in trading if t.to_id == p), trading[-1] if trading else None)
    sel_period = next((x for x in periods if sel and x.to_id == sel.to_id), None)

    return templates.TemplateResponse(request, "attribution.html", {
        "policy": policy, "periods": periods, "rollups": rus, "ru": ru,
        "trading": trading, "tsum": tsum, "sel": sel, "sel_period": sel_period,
        "stale_days": SNAPSHOT_STALE_DAYS, "msg": msg,
    })


@router.post("/attribution/rebuild")
def attribution_rebuild(session: Session = Depends(get_session)):
    try:
        n = rebuild_attribution(session, commit=True)
        msg = f"recomputed {n} period(s)"
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        msg = f"error: {exc}"
    return RedirectResponse(url=f"/attribution?msg={msg}", status_code=status.HTTP_303_SEE_OTHER)
