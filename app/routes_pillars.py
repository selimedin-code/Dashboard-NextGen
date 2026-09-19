"""Pillars view: assign every holding to the 13-pillar framework, and set the
policy-benchmark target weight per pillar.

Inline edit the held tickers, or bulk-upload a Ticker→Pillar file. Both write to
`securities.pillar`, which the exposure and position views group by. Targets
live on `pillars.target_weight` and drive the policy benchmark + attribution.
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
from app.ingest.seed import SeedFormatError, detect_kind, import_from_csv, parse_target_weight
from app.models import HoldingSnapshot, Pillar, Security, Snapshot

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
    held = set(_held_tickers(session))
    secs = {
        s.ticker: s for s in session.execute(select(Security)).scalars().all()
    }
    rows = [{"ticker": t, "pillar": (secs[t].pillar if t in secs else None),
             "name": (secs[t].name if t in secs else None)} for t in sorted(held)]
    existing_pillars = sorted({s.pillar for s in secs.values() if s.pillar})
    unassigned = sum(1 for r in rows if not r["pillar"])

    # Taxonomy: one row per pillar, with the count of HELD names in it.
    pillars = session.execute(select(Pillar).order_by(Pillar.id)).scalars().all()
    held_by_pid: dict[str, int] = {}
    for t in held:
        pid = secs[t].pillar_id if t in secs else None
        if pid:
            held_by_pid[pid] = held_by_pid.get(pid, 0) + 1
    # Current weight per pillar (share of the whole fund) next to its target.
    current: dict[str, object] = {}
    from app.exposure import build_exposure
    ev = build_exposure(session)
    if ev is not None:
        current = {pe.name: pe.fund_weight for pe in ev.pillars}
    taxonomy = [{
        "id": p.id, "name": p.name, "primary_etf": p.primary_etf,
        "alt_etf": p.alt_etf, "caveat": p.caveat, "held": held_by_pid.get(p.id, 0),
        "target": p.target_weight, "current": current.get(p.name),
    } for p in pillars]
    target_total = sum((t["target"] for t in taxonomy if t["target"] is not None), 0)

    ctx = {
        "rows": rows, "existing_pillars": existing_pillars,
        "taxonomy": taxonomy, "target_total": target_total,
        "cash_weight": ev.cash_pct if ev is not None else None,
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


@router.post("/pillars/targets")
async def pillars_targets(request: Request, session: Session = Depends(get_session)):
    """Save policy target weights (entered in %). Blank clears a target."""
    form = await request.form()
    pillars = {p.id: p for p in session.execute(select(Pillar)).scalars().all()}
    try:
        for k, v in form.items():
            if not k.startswith("target__") or k[len("target__"):] not in pillars:
                continue
            raw = str(v).strip()
            # The form is in percent: a bare "0.5" means 0.5%, not 50%.
            if raw and not raw.endswith("%"):
                raw += "%"
            pillars[k[len("target__"):]].target_weight = parse_target_weight(raw)
    except SeedFormatError as exc:
        session.rollback()
        return templates.TemplateResponse(
            request, "pillars.html", _page_context(session, upload_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST)
    session.flush()
    from app.attribution import safe_rebuild
    safe_rebuild(session)
    session.commit()
    return RedirectResponse(url="/pillars?targets=1#targets", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/pillars/upload", response_class=HTMLResponse)
async def pillars_upload(
    request: Request,
    file: UploadFile,
    session: Session = Depends(get_session),
):
    data = await file.read()
    filename = file.filename or "upload"

    # A CSV may be the pillar taxonomy or the securities seed; detect and route.
    # Anything else falls back to the simple Ticker,Pillar mapping.
    is_seed = filename.lower().endswith(".csv")
    if is_seed:
        try:
            rows0 = data.decode("utf-8-sig", errors="replace").splitlines()
            headers = set(rows0[0].split(",")) if rows0 else set()
            is_seed = detect_kind(headers) != "unknown"
        except Exception:
            is_seed = False

    try:
        if is_seed:
            summary = import_from_csv(session, filename, data)
            session.flush()
            from app.attribution import safe_rebuild
            safe_rebuild(session)
            session.commit()
            if summary["kind"] == "pillars":
                msg = (f"Imported pillar taxonomy: {summary['created']} added, "
                       f"{summary['updated']} updated.")
            else:
                msg = (f"Imported securities seed: {summary['updated']} updated, "
                       f"{summary['created']} added — names, ISINs and pillars set.")
        else:
            mapping = parse_pillar_file(filename, data)
            result = apply_pillars(session, mapping, create_missing=True)
            session.commit()
            msg = (f"Applied {result['updated'] + result['created']} pillar assignments "
                   f"({result['updated']} updated, {result['created']} pre-registered).")
    except (PillarParseError, SeedFormatError) as exc:
        return templates.TemplateResponse(
            request, "pillars.html",
            _page_context(session, upload_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return templates.TemplateResponse(
        request, "pillars.html", _page_context(session, upload_result=msg)
    )
