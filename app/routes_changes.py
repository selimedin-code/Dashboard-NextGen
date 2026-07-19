"""Changes view (Phase 2): the decision log between two snapshots.

Reads the persisted `changes` table. The point of the page is the
FLOW_DRIVEN vs DISCRETIONARY split — it lets the manager see which moves were
actual decisions versus the mechanics of money coming in or out.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_session
from app.models import Change, Snapshot

router = APIRouter(dependencies=[Depends(require_auth)])
templates: Jinja2Templates = None


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


@router.get("/changes", response_class=HTMLResponse)
def changes_view(
    request: Request,
    to: str | None = None,
    session: Session = Depends(get_session),
):
    # All snapshot pairs that have changes, newest first, for the selector.
    snaps = session.execute(select(Snapshot).order_by(Snapshot.as_of.desc())).scalars().all()
    pairs = []
    for curr, prev in zip(snaps, snaps[1:]):   # snaps is desc, so prev is the older one
        pairs.append({"to_id": curr.id, "to": curr.as_of, "from": prev.as_of})

    selected = None
    if pairs:
        selected = next((p for p in pairs if str(p["to_id"]) == to), pairs[0])

    rows: list[Change] = []
    counts = {k: 0 for k in ("OPEN", "ADD", "TRIM", "CLOSE", "HOLD")}
    cls_counts = {"FLOW_DRIVEN": 0, "DISCRETIONARY": 0, "AMBIGUOUS": 0}
    if selected:
        rows = session.execute(
            select(Change).where(Change.to_snapshot == selected["to_id"])
        ).scalars().all()
        order = {"OPEN": 0, "CLOSE": 1, "ADD": 2, "TRIM": 3, "HOLD": 4}
        rows.sort(key=lambda c: (order.get(c.change_type, 9), c.ticker))
        for c in rows:
            counts[c.change_type] = counts.get(c.change_type, 0) + 1
            if c.classification in cls_counts:
                cls_counts[c.classification] += 1

    # Decisions first: hide the (often long) HOLD list behind a flag.
    show_holds = request.query_params.get("holds") == "1"
    visible = rows if show_holds else [c for c in rows if c.change_type != "HOLD"]

    return templates.TemplateResponse(
        request, "changes.html",
        {
            "pairs": pairs, "selected": selected, "rows": visible,
            "counts": counts, "cls_counts": cls_counts,
            "hold_count": counts.get("HOLD", 0), "show_holds": show_holds,
            "has_data": bool(pairs),
        },
    )
