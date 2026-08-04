# syntax=docker/dockerfile:1
#
# Multi-stage build for the Telegram support bot + dashboard.
# Both processes (bot polling and the FastAPI dashboard) run from this one image
# — docker-compose picks which to run per service via `command`.
#
# Python is pinned to 3.13: the dependency stack (aiogram 3.10 → pydantic-core
# 2.20) has no wheels for 3.14, so newer Python would force a fragile source build.

# ---------- Stage 1: build dependencies into a venv ----------
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build toolchain for any package without a prebuilt wheel (e.g. aiohttp on 3.13).
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ---------- Stage 2: slim runtime ----------
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# tzdata: Debian slim ships no IANA tz database, but the app calls
# ZoneInfo("Europe/Kyiv") at import time. Without this the bot/dashboard crash.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY src ./src

# Persistent locations (mounted as volumes in docker-compose).
RUN mkdir -p /app/data /app/logs && chown -R appuser:appuser /app

USER appuser
EXPOSE 8000

# Default to the bot; the dashboard service overrides this in docker-compose.
CMD ["python", "-m", "src.main"]
