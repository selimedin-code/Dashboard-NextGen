"""Snapshot upload routes: form -> parse+validate+preview -> commit.

The parsed file is staged in the DB between the preview and commit so the user
confirms exactly what they saw. Nothing is written to the holdings tables until
the commit step.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_session
from app.ingest.commit import CommitError, commit_snapshot
from app.ingest.normalize import ParseError
from app.ingest.parser import ParsedHolding, UnsupportedFileError, parse_upload
from app.ingest.validate import validate_snapshot
from app.models import Security, UploadStaging

router = APIRouter(dependencies=[Depends(require_auth)])
templates: Jinja2Templates = None  # injected from main.py via init_templates


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


_STAGING_TTL = timedelta(hours=6)


# --- (de)serialization: Decimal <-> str so JSON keeps exact values ---


def _dump(h: ParsedHolding) -> dict:
    return {
        "raw_ticker": h.raw_ticker, "ticker": h.ticker, "exchange": h.exchange,
        "units": str(h.units), "avg_cost": str(h.avg_cost),
        "isin": h.isin, "name": h.name, "is_cash": h.is_cash,
    }


def _load(d: dict) -> ParsedHolding:
    return ParsedHolding(
        raw_ticker=d["raw_ticker"], ticker=d["ticker"], exchange=d["exchange"],
        units=Decimal(d["units"]), avg_cost=Decimal(d["avg_cost"]),
        isin=d.get("isin"), name=d.get("name"), is_cash=d.get("is_cash", False),
    )


@router.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    return templates.TemplateResponse(request, "upload.html", {"today": date.today().isoformat()})


@router.post("/upload", response_class=HTMLResponse)
async def upload_preview(
    request: Request,
    file: UploadFile,
    as_of: str = Form(...),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    errors: list[str] = []
    try:
        as_of_date = date.fromisoformat(as_of)
    except ValueError:
        as_of_date = None
        errors.append(f"Invalid date: {as_of!r}. Use YYYY-MM-DD.")

    data = await file.read()
    if not data:
        errors.append("The uploaded file is empty.")

    result = None
    if not errors:
        try:
            result = parse_upload(file.filename or "upload", data)
        except (UnsupportedFileError, ParseError) as exc:
            errors.append(str(exc))

    if errors or result is None or as_of_date is None:
        return templates.TemplateResponse(
            request, "upload.html",
            {"today": as_of or date.today().isoformat(), "errors": errors, "notes": notes},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    validation = validate_snapshot(result.holdings, as_of_date, session, quote_check=None)
    validation.warnings = result.warnings + validation.warnings

    existing_pillars = sorted(
        p for p in session.execute(
            select(Security.pillar).where(Security.pillar.is_not(None)).distinct()
        ).scalars().all() if p
    )

    # Stage the parsed file for the commit step.
    _prune_staging(session)
    token = secrets.token_urlsafe(24)
    session.add(UploadStaging(
        token=token, as_of=as_of_date, filename=file.filename, notes=notes or None,
        source_format=result.source_format, parsed=[_dump(h) for h in result.holdings],
    ))
    session.commit()

    total_units_cost = sum((h.units * h.avg_cost for h in result.holdings), Decimal("0"))
    return templates.TemplateResponse(
        request, "upload_preview.html",
        {
            "token": token, "as_of": as_of_date, "notes": notes,
            "filename": file.filename, "source_format": result.source_format,
            "holdings": result.holdings, "validation": validation,
            "counts": validation.summary_counts(),
            "position_count": len(result.holdings),
            "cost_basis_total": total_units_cost,
            "existing_pillars": existing_pillars,
        },
    )


@router.post("/upload/commit")
async def upload_commit(
    request: Request,
    session: Session = Depends(get_session),
):
    form = await request.form()
    token = str(form.get("token", ""))
    overwrite = str(form.get("overwrite", ""))

    staged = session.get(UploadStaging, token)
    if staged is None:
        return templates.TemplateResponse(
            request, "upload.html",
            {"today": date.today().isoformat(),
             "errors": ["This upload session expired. Please upload the file again."]},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    holdings = [_load(d) for d in staged.parsed]

    # Pillar assignments come in as form fields named pillar__<TICKER>.
    form_pillars = {
        k[len("pillar__"):]: str(v)
        for k, v in form.items()
        if k.startswith("pillar__") and str(v).strip()
    }

    try:
        snapshot = commit_snapshot(
            holdings=holdings, as_of=staged.as_of, filename=staged.filename,
            notes=staged.notes, pillar_assignments=form_pillars, session=session,
            overwrite=(overwrite == "yes"),
        )
    except CommitError as exc:
        # Re-run validation to re-render the preview with the error.
        validation = validate_snapshot(holdings, staged.as_of, session, quote_check=None)
        return templates.TemplateResponse(
            request, "upload_preview.html",
            {
                "token": token, "as_of": staged.as_of, "notes": staged.notes,
                "filename": staged.filename, "source_format": staged.source_format,
                "holdings": holdings, "validation": validation,
                "counts": validation.summary_counts(), "position_count": len(holdings),
                "cost_basis_total": sum((h.units * h.avg_cost for h in holdings), Decimal("0")),
                "commit_error": str(exc),
            },
            status_code=status.HTTP_409_CONFLICT,
        )

    session.execute(delete(UploadStaging).where(UploadStaging.token == token))
    session.commit()
    return RedirectResponse(url=f"/?committed={snapshot.as_of}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/upload/cancel")
def upload_cancel(token: str = Form(...), session: Session = Depends(get_session)):
    session.execute(delete(UploadStaging).where(UploadStaging.token == token))
    session.commit()
    return RedirectResponse(url="/upload", status_code=status.HTTP_303_SEE_OTHER)


def _prune_staging(session: Session) -> None:
    cutoff = datetime.now(timezone.utc) - _STAGING_TTL
    session.execute(delete(UploadStaging).where(UploadStaging.created_at < cutoff))
