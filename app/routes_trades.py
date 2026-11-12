"""Central multi-leg trade entry: one page for a whole broker confirmation.

A thin wrapper over trades.record_trade — each leg goes through the same engine
as the ticker-page form (manual snapshot, cash leg, change log, undo), and legs
sharing the trade date land on the same manual snapshot.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.config import get_settings
from app.db import get_session
from app.models import Snapshot
from app.providers.fmp import FMPClient
from app import trades as tr

router = APIRouter(dependencies=[Depends(require_auth)])
templates: Jinja2Templates = None


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


ROWS = 6


def _ctx(session: Session) -> dict:
    latest = session.execute(
        select(Snapshot).order_by(Snapshot.as_of.desc()).limit(1)
    ).scalar_one_or_none()
    return {"rows": range(ROWS), "today": date.today().isoformat(), "latest": latest}


@router.get("/trades", response_class=HTMLResponse)
def trades_form(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "trades.html", _ctx(session))


@router.post("/trades", response_class=HTMLResponse)
async def trades_apply(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    adjust_cash = form.get("adjust_cash") == "1"
    note = str(form.get("note", "")).strip() or None
    error: str | None = None
    results: list[tr.TradeResult] = []

    try:
        trade_date = date.fromisoformat(str(form.get("trade_date", "")))
    except ValueError:
        trade_date = None
        error = f"Invalid trade date: {form.get('trade_date')!r}. Use YYYY-MM-DD."

    legs = []
    for i in range(ROWS):
        ticker = str(form.get(f"ticker_{i}", "")).strip()
        if ticker:
            legs.append((ticker, str(form.get(f"side_{i}", "")),
                         str(form.get(f"units_{i}", "")).strip(),
                         str(form.get(f"price_{i}", "")).strip() or None))
    if not legs and error is None:
        error = "No trades entered — fill at least one row."

    client: FMPClient | None = None
    if error is None and any(price is None for *_, price in legs):
        key = get_settings().fmp_api_key
        client = FMPClient(key, min_interval=0.05) if key else None

    failed_leg: tuple | None = None
    if error is None:
        try:
            for ticker, side, units, price in legs:
                results.append(tr.record_trade(
                    session, ticker, side=side, units_raw=units, price_raw=price,
                    trade_date=trade_date, adjust_cash=adjust_cash, note=note,
                    client=client,
                ))
        except tr.TradeError as exc:
            failed_leg = (ticker, side, units)
            error = str(exc)
        finally:
            if client is not None:
                client.close()

    ctx = _ctx(session)
    ctx.update({
        "results": results, "error": error, "failed_leg": failed_leg,
        "form": form if error else None,
        "today": str(form.get("trade_date") or date.today().isoformat()),
    })
    code = status.HTTP_400_BAD_REQUEST if error else status.HTTP_200_OK
    return templates.TemplateResponse(request, "trades.html", ctx, status_code=code)
