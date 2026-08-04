"""Web dashboard for analytics over the support bot's data.

Runs as a SEPARATE process from the bot. Read-only to support.db. Shares
the same .env and ACCESS_PASSWORD for auth.

Launch:
    uvicorn src.dashboard.app:app --port 8000 --reload
"""
