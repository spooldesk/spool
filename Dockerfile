# syntax=docker/dockerfile:1
#
# Multi-stage build for the Telegram support bot + dashboard.
# Both processes (bot polling and the FastAPI dashboard) run from this one image
# — docker-compose picks which to run per service via `command`.
#
# Python is pinned to 3.12, and the reason is worth keeping: aiogram 3.10 pins
# aiohttp to ~=3.9.0, and aiohttp 3.9.5 publishes no Linux wheel for CPython
# 3.13 or newer. On 3.13 pip therefore compiles it from source — which worked
# until the base image moved to a patch release whose C API those 2024 sources
# no longer match, and the deploy failed with a gcc error.
#
# So this is not "an older Python". It is the newest one every pinned
# dependency actually ships a wheel for. Raising it means bumping aiogram
# first and checking the wheels again.

# ---------- Stage 1: build dependencies into a venv ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# A safety net, not an expectation: on this Python every pinned dependency has
# a wheel, so nothing should compile. If a build ever starts invoking gcc, that
# is the signal the Python version and the dependency set have drifted apart.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

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

# Persistent locations (mounted as volumes in docker-compose).
RUN mkdir -p /app/data /app/logs && chown -R appuser:appuser /app

USER appuser
EXPOSE 8000

# Default to the bot; the dashboard service overrides this in docker-compose.
CMD ["python", "-m", "src.main"]
