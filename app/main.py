"""FastAPI application entrypoint.

Scaffold stage: auth, templating, static assets, a health check, and a home
page that reports database + snapshot status. The upload/parse feature and the
position table land in Phase 1 and beyond.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.config import get_settings
from app.db import get_session
from app.models import HoldingSnapshot, Snapshot

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="NextGen Fund Dashboard", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    """Unauthenticated liveness probe for Render. Also checks DB connectivity."""
    settings = get_settings()
    db_ok = True
    db_error: str | None = None
    try:
        from app.db import engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — health check must never raise
        db_ok = False
        db_error = str(exc)
    status_code = 200 if db_ok else 503
    return JSONResponse(
        {"status": "ok" if db_ok else "degraded", "database": db_ok, "error": db_error,
         "env": settings.app_env},
        status_code=status_code,
    )


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    _user: str = Depends(require_auth),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Home page. Reports the latest snapshot and how stale it is.

    'Fail visibly' — a stale or missing snapshot is shown loudly, never rendered
    as if it were current.
    """
    latest: Snapshot | None = session.execute(
        select(Snapshot).order_by(Snapshot.as_of.desc()).limit(1)
    ).scalar_one_or_none()

    position_count = 0
    staleness_days: int | None = None
    if latest is not None:
        position_count = session.execute(
            select(func.count())
            .select_from(HoldingSnapshot)
            .where(HoldingSnapshot.snapshot_id == latest.id)
        ).scalar_one()
        staleness_days = (date.today() - latest.as_of).days

    snapshot_count = session.execute(
        select(func.count()).select_from(Snapshot)
    ).scalar_one()

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "latest": latest,
            "position_count": position_count,
            "staleness_days": staleness_days,
            "snapshot_count": snapshot_count,
            "now": datetime.now(timezone.utc),
        },
    )
