"""Overview page — KPI cards + headline charts."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.dashboard import queries
from src.dashboard.i18n import t as dt

router = APIRouter()


def _fmt_minutes(m: Optional[float]) -> str:
    if m is None:
        return "—"
    if m < 1:
        return f"<1 {dt('min_short')}"
    if m < 60:
        return f"{m:.0f} {dt('min_short')}"
    if m < 60 * 24:
        return f"{m / 60:.1f} {dt('hour_short')}"
    return f"{m / 60 / 24:.1f} {dt('day_short')}"


def _fmt_hours(h: Optional[float]) -> str:
    if h is None:
        return "—"
    if h < 1:
        return f"{h * 60:.0f} {dt('min_short')}"
    if h < 24:
        return f"{h:.1f} {dt('hour_short')}"
    return f"{h / 24:.1f} {dt('day_short')}"


_PERIOD_CHOICES = (7, 30, 90)


def _avg_skip_none(values: list) -> float | None:
    """Plain average over the non-None values. None means 'no data on that day'."""
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


@router.get("/overview", response_class=HTMLResponse)
async def overview(request: Request, days: int | None = None):
    templates = request.app.state.templates

    # Validate period — anything outside the allowed set falls back to 30
    period_days = days if days in _PERIOD_CHOICES else 30

    stats = await queries.overview_stats()
    trend = await queries.tickets_per_day(days=period_days)
    first_response_trend = await queries.avg_first_response_per_day(days=period_days)
    close_time_trend = await queries.avg_close_time_per_day(days=period_days)
    by_category = await queries.tickets_by_category()
    # Admin-side stats moved to its own (paused) page; show top users instead —
    # consistent with /users page in the nav.
    top_users = await queries.users_with_ticket_stats(limit=5)
    heatmap, heatmap_max = await queries.activity_heatmap()
    # Dynamic height for the user bar chart — clamped to look good with 1..50 rows.
    users_chart_height = max(200, min(400, len(top_users) * 40 + 60))

    # Convert first-response from minutes → hours (uniform unit on the page).
    # Preserve None so the line chart breaks where there's no data.
    first_response_hours_values = [
        (v / 60.0 if v is not None else None) for _, v in first_response_trend
    ]
    close_time_hours_values = [v for _, v in close_time_trend]

    # Overall averages across the chosen period (single numbers under each chart)
    avg_first_response_hours_period = _avg_skip_none(first_response_hours_values)
    avg_close_hours_period = _avg_skip_none(close_time_hours_values)

    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "active_page": "overview",
            "period_days": period_days,
            "period_choices": _PERIOD_CHOICES,
            "kpis": [
                {"label": dt("kpi_total"), "value": str(stats["total_tickets"])},
                {"label": dt("kpi_active"), "value": str(stats["active_tickets"])},
                {
                    "label": dt("kpi_awaiting"),
                    "value": str(stats["awaiting_response"]),
                    "hint": dt("kpi_awaiting_hint"),
                    "alert": stats["awaiting_response"] > 0,
                },
                {
                    "label": dt("kpi_first_response"),
                    "value": _fmt_minutes(stats["avg_first_response_minutes"]),
                },
                {
                    "label": dt("kpi_close_time"),
                    "value": _fmt_hours(stats["avg_close_hours"]),
                },
                {
                    "label": "CSAT",
                    "value": (f"{stats['csat_avg']:.1f} / 5"
                              if stats["csat_avg"] is not None else "—"),
                    "hint": (f"{stats['csat_count']} {dt('ratings')}"
                             if stats["csat_count"] else dt("kpi_csat_hint_none")),
                },
            ],
            # Pass data, not serialized strings: |tojson escapes script delimiters.
            "trend_labels_json": [d for d, _ in trend],
            "trend_values_json": [n for _, n in trend],
            # Time-series for both metrics, BOTH IN HOURS (uniform unit).
            # Days without data come through as None → Chart.js breaks the line
            # there instead of dropping to zero, which would lie about the metric.
            "first_response_labels_json": [d for d, _ in first_response_trend],
            "first_response_values_json": [
                round(v, 2) if v is not None else None for v in first_response_hours_values
            ],
            "close_time_labels_json": [d for d, _ in close_time_trend],
            "close_time_values_json": [
                round(v, 2) if v is not None else None for v in close_time_hours_values
            ],
            # Period-level summaries shown above each line chart
            "avg_first_response_hours_period": avg_first_response_hours_period,
            "avg_close_hours_period": avg_close_hours_period,
            "fmt_hours": _fmt_hours,
            "category_labels_json": [c for c, _ in by_category],
            "category_values_json": [n for _, n in by_category],
            "users_labels_json": [u["label"] for u in top_users],
            "users_values_json": [u["total"] for u in top_users],
            "users_chart_height": users_chart_height,
            "heatmap_rows": heatmap,
            "heatmap_max": heatmap_max,
        },
    )
