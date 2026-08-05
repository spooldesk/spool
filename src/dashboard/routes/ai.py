"""AI Analytics page — accept rate, latency, tokens, recent rejections."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.dashboard import queries
from src.dashboard.formatting import fmt_ms as _fmt_ms

router = APIRouter()


def _fmt_tokens(n: int | None) -> str:
    if not n:
        return "0"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.2f}M"


def _fmt_pct(p: float | None) -> str:
    if p is None:
        return "—"
    return f"{p:.0f}%"


@router.get("/ai", response_class=HTMLResponse)
async def ai_page(request: Request):
    templates = request.app.state.templates
    stats = await queries.ai_overview()
    per_day = await queries.ai_calls_per_day(days=30)
    rejected = await queries.ai_recent_rejected(limit=15)
    # Accept rate says operators liked the suggestion. This says whether
    # the customer did — which is a different and more honest question.
    quality = await queries.ai_vs_human_csat()
    by_category = await queries.ai_csat_by_category()

    return templates.TemplateResponse(
        request,
        "ai.html",
        {
            "active_page": "ai",
            "stats": stats,
            "per_day_labels_json": json.dumps([d for d, _ in per_day]),
            "per_day_values_json": json.dumps([n for _, n in per_day]),
            "rejected": rejected,
            "quality": quality,
            "by_category": by_category,
            "fmt_tokens": _fmt_tokens,
            "fmt_pct": _fmt_pct,
            "fmt_ms": _fmt_ms,
        },
    )
