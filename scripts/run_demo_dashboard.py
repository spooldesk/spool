"""Run the isolated demo dashboard, without loading .env or external credentials.

Defaults to demo_support.db. --database accepts another demo seeded database.
Run: .venv/Scripts/python.exe scripts/run_demo_dashboard.py
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.demo_environment import (
    configure, database_path, pin_offline_settings, verify_seed_marker,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="A database created by seed_demo_data.py")
    args = parser.parse_args()
    try:
        path = database_path(args.database)
        if not path.is_file():
            parser.error("Demo database not found — run scripts/seed_demo_data.py first")
        if args.database:
            verify_seed_marker(path)
        configure(path)
        pin_offline_settings(path)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))

    import uvicorn

    # Hosting platforms inject PORT; local screenshots stay on loopback.
    port = int(os.getenv("PORT", "8010"))
    host = os.getenv("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    uvicorn.run("src.dashboard.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
