"""Templates analytics page — usage, removal candidates, admin × template matrix.

Module name is `templates_page` (not `templates`) to avoid clashing with the
`templates` directory under `src/dashboard/`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.dashboard import queries

router = APIRouter()

REMOVAL_CANDIDATE_DAYS = 30


@router.get("/templates", response_class=HTMLResponse)
async def templates_page(request: Request):
    templates_engine = request.app.state.templates

    rows = await queries.templates_with_usage(include_archived=False)
    matrix = await queries.template_admin_matrix()

    # Candidates for removal: zero usage AND created more than 30d ago.
    # Created recently with zero usage is OK — give it time.
    cutoff = datetime.utcnow() - timedelta(days=REMOVAL_CANDIDATE_DAYS)
    candidates = [
        r for r in rows
        if r["usage_count"] == 0 and r["created_at"] and r["created_at"] < cutoff
    ]

    return templates_engine.TemplateResponse(
        request,
        "templates_analytics.html",
        {
            "active_page": "templates_analytics",
            "rows": rows,
            "candidates": candidates,
            "candidate_threshold_days": REMOVAL_CANDIDATE_DAYS,
            "matrix": matrix,
        },
    )
