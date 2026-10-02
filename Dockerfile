# syntax=docker/dockerfile:1
#
# Multi-stage build for Spooldesk's channels and dashboard.
# Both processes (bot polling and the FastAPI dashboard) run from this one image
# — docker-compose picks which to run per service via `command`.
#
# Python 3.12 is the release target and is also used in CI. Dependencies,
# including transitives, come from a hashed lock. Missing wheels fail the
# build instead of silently compiling a different artifact from source.

# ---------- Stage 1: build dependencies into a venv ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN python -m pip install --require-hashes --only-binary=:all: -r requirements.txt \
    && python -m pip check

# ---------- Stage 2: slim runtime ----------
FROM python:3.12-slim

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
COPY LICENSE /app/LICENSE

# Persistent locations (mounted as volumes in docker-compose).
RUN mkdir -p /app/data /app/logs && chown -R appuser:appuser /app

USER appuser
EXPOSE 8000

# Default to the bot; the dashboard service overrides this in docker-compose.
CMD ["python", "-m", "src.main"]
