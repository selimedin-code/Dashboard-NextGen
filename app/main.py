"""FastAPI application entrypoint.

Scaffold stage: auth, templating, static assets, a health check, and a home
page that reports database + snapshot status. The upload/parse feature and the
position table land in Phase 1 and beyond.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.config import get_settings
from app.db import get_session
from app.models import Snapshot

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _fmt_money(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}"


def _fmt_qty(value) -> str:
    """Share quantities: trim trailing zeros but keep fractional units visible."""
    if value is None:
        return "—"
    s = f"{value:,.6f}".rstrip("0").rstrip(".")
    return s or "0"


def _fmt_pct(value) -> str:
    """value is a fraction; 0.05 -> '+5.0%'."""
    if value is None:
        return "—"
    return f"{value * 100:+.1f}%"


def _fmt_pct_plain(value) -> str:
    """value is a fraction; 0.066 -> '6.6%' (no sign — for weights)."""
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _fmt_dec1(value) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}"


def _fmt_hhi(value) -> str:
    """Herfindahl as index points (×10000), the conventional scale."""
    if value is None:
        return "—"
    return f"{value * 10000:,.0f}"


templates.env.filters["money"] = _fmt_money
templates.env.filters["qty"] = _fmt_qty
templates.env.filters["pct"] = _fmt_pct
templates.env.filters["pct_plain"] = _fmt_pct_plain
templates.env.filters["dec1"] = _fmt_dec1
templates.env.filters["hhi"] = _fmt_hhi

# Cache-bust the stylesheet by its file mtime so CSS edits always reach the browser.
try:
    _css_version = str(int((BASE_DIR / "static" / "app.css").stat().st_mtime))
except OSError:
    _css_version = "0"
templates.env.globals["static_v"] = _css_version

app = FastAPI(title="NextGen Fund Dashboard", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Feature routers share the Jinja environment.
from app import routes_upload  # noqa: E402
from app import routes_changes  # noqa: E402
from app import routes_pillars  # noqa: E402
from app import routes_positions  # noqa: E402
from app import routes_exposure  # noqa: E402
from app import routes_ticker  # noqa: E402
from app import routes_performance  # noqa: E402
from app import routes_signals  # noqa: E402

routes_upload.init_templates(templates)
routes_changes.init_templates(templates)
routes_pillars.init_templates(templates)
routes_positions.init_templates(templates)
routes_exposure.init_templates(templates)
routes_ticker.init_templates(templates)
routes_performance.init_templates(templates)
routes_signals.init_templates(templates)
app.include_router(routes_upload.router)
app.include_router(routes_changes.router)
app.include_router(routes_pillars.router)
app.include_router(routes_positions.router)
app.include_router(routes_exposure.router)
app.include_router(routes_ticker.router)
app.include_router(routes_performance.router)
app.include_router(routes_signals.router)


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
    committed: str | None = None,
    refreshed: str | None = None,
    _user: str = Depends(require_auth),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """At-a-glance summary that pulls the headline number from each section.

    'Fail visibly' — a stale/missing snapshot or unfetched prices are shown
    loudly, never rendered as if current.
    """
    from app.exposure import build_exposure
    from app.performance import build_performance
    from app.positions import build_positions
    from app.signals import build_signals

    latest: Snapshot | None = session.execute(
        select(Snapshot).order_by(Snapshot.as_of.desc()).limit(1)
    ).scalar_one_or_none()

    ctx: dict = {"latest": latest, "committed": committed, "refreshed": refreshed,
                 "now": datetime.now(timezone.utc)}

    if latest is not None:
        ctx["staleness_days"] = (date.today() - latest.as_of).days
        ctx["positions"] = build_positions(session)
        ctx["exposure"] = build_exposure(session)
        ctx["perf"] = build_performance(session)
        sig = build_signals(session)
        ctx["signals"] = sig
        ctx["signal_total"] = sig.total if sig else 0

    return templates.TemplateResponse(request, "index.html", ctx)


@app.post("/refresh")
def refresh_all(
    _user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    """Refresh everything the Overview shows that IS price-driven, in one place:
    live quotes AND the benchmark history. Fundamentals and the NAV series have
    their own (slower / upload) cadence and are refreshed from their own pages."""
    from app.prices import refresh_quotes
    from app.performance import refresh_benchmarks

    msg = ""
    try:
        q = refresh_quotes(session)
        msg = f"{q['ok']} priced"
        try:
            refresh_benchmarks(session)
            msg += ", benchmarks updated"
        except Exception as exc:  # noqa: BLE001 — prices already saved; benchmarks best-effort
            msg += f" (benchmarks failed: {exc})"
    except Exception as exc:  # noqa: BLE001
        msg = f"error: {exc}"
    return RedirectResponse(url=f"/?refreshed={msg}", status_code=303)
