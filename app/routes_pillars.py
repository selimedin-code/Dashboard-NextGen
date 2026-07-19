"""Pillars view: assign every holding to the 11-pillar framework.

Inline edit the held tickers, or bulk-upload a Ticker→Pillar file. Both write to
`securities.pillar`, which the exposure and position views group by.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_session
from app.ingest.pillars import PillarParseError, apply_pillars, parse_pillar_file
from app.models import HoldingSnapshot, Security, Snapshot

router = APIRouter(dependencies=[Depends(require_auth)])
templates: Jinja2Templates = None


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


def _held_tickers(session: Session) -> list[str]:
    """Non-cash tickers in the most recent snapshot."""
    latest = session.execute(
        select(Snapshot).order_by(Snapshot.as_of.desc()).limit(1)
    ).scalar_one_or_none()
    if latest is None:
        return []
    rows = session.execute(
        select(HoldingSnapshot.ticker)
        .where(HoldingSnapshot.snapshot_id == latest.id)
        .where(HoldingSnapshot.ticker != "USD Cash")
    ).scalars().all()
    return sorted(rows)


def _page_context(session: Session, **extra) -> dict:
    held = _held_tickers(session)
    secs = {
        s.ticker: s for s in session.execute(select(Security)).scalars().all()
    }
    rows = [{"ticker": t, "pillar": (secs[t].pillar if t in secs else None),
             "name": (secs[t].name if t in secs else None)} for t in held]
    existing_pillars = sorted({s.pillar for s in secs.values() if s.pillar})
    # per-pillar counts across held names, for the summary
    counts: dict[str, int] = {}
    for r in rows:
        key = r["pillar"] or "— unassigned —"
        counts[key] = counts.get(key, 0) + 1
    unassigned = sum(1 for r in rows if not r["pillar"])
    ctx = {
        "rows": rows, "existing_pillars": existing_pillars,
        "pillar_counts": sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])),
        "held_count": len(rows), "unassigned": unassigned,
    }
    ctx.update(extra)
    return ctx


@router.get("/pillars", response_class=HTMLResponse)
def pillars_view(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "pillars.html", _page_context(session))


@router.post("/pillars/save")
async def pillars_save(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    mapping = {
        k[len("pillar__"):]: str(v).strip()
        for k, v in form.items()
        if k.startswith("pillar__") and str(v).strip()
    }
    if mapping:
        apply_pillars(session, mapping, create_missing=False)
        session.commit()
    return RedirectResponse(url="/pillars?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/pillars/upload", response_class=HTMLResponse)
async def pillars_upload(
    request: Request,
    file: UploadFile,
    session: Session = Depends(get_session),
):
    data = await file.read()
    try:
        mapping = parse_pillar_file(file.filename or "upload", data)
    except PillarParseError as exc:
        return templates.TemplateResponse(
            request, "pillars.html",
            _page_context(session, upload_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    result = apply_pillars(session, mapping, create_missing=True)
    session.commit()
    return templates.TemplateResponse(
        request, "pillars.html",
        _page_context(
            session,
            upload_result=f"Applied {result['updated'] + result['created']} pillar assignments "
                          f"({result['updated']} updated, {result['created']} pre-registered for "
                          f"tickers not yet held).",
        ),
    )
