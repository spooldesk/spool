"""Offline release configuration checks. Never prints secret values or connects."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.database.connection_url import database_url


def evaluate(config: dict[str, str], *, public_url: str, production: bool = True) -> list[dict]:
    checks = []
    def check(name, ok, action):
        checks.append({"name": name, "ok": bool(ok), "action": "" if ok else action})
    def value(key):
        return (config.get(key) or "").strip()

    password = value("ACCESS_PASSWORD")
    check("bootstrap_password", len(password) >= 12 and password.lower() not in {
        "change-me", "changeme", "password123456", "test-password-123"},
        "Set a unique ACCESS_PASSWORD of at least 12 characters; rename the bootstrap owner after login.")
    try:
        database = database_url(config)
    except ValueError:
        database = ""
    check("database", database.startswith(("sqlite+aiosqlite:///", "postgresql+asyncpg://")),
          "Set a supported DATABASE_URL, or valid POSTGRES_HOST/PORT/PASSWORD settings, and persist storage.")
    parsed = urlsplit(public_url)
    check("public_url", bool(parsed.hostname) and parsed.scheme in {"http", "https"}
          and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
          "Pass the public origin using --public-url, without credentials, query or fragment.")
    if production:
        check("postgresql", database.startswith("postgresql+asyncpg://"),
              "Production installations use PostgreSQL. Configure POSTGRES_HOST/PORT/PASSWORD.")
        check("https", parsed.scheme == "https", "Terminate TLS before exposing the portal.")
        check("secure_cookie", value("DASH_COOKIE_SECURE").lower() == "true",
              "Set DASH_COOKIE_SECURE=true in the effective production environment.")
        check("session_secret", len(value("DASH_SECRET_KEY")) >= 32,
              "Generate a persistent DASH_SECRET_KEY (at least 32 characters).")
        check("encryption_key", len(value("SECRETS_KEY")) >= 32,
              "Set a persistent SECRETS_KEY and keep a separate protected recovery copy.")
        check("demo_mode", value("DEMO_MODE") != "1", "Disable DEMO_MODE for customer data.")
        check("test_mode", value("TESTING") != "1", "Unset TESTING so delivery and SLA workers run.")
    bot, chat = value("BOT_TOKEN"), value("ADMIN_CHAT_ID")
    check("telegram", (not bot and chat in {"", "0"}) or (
        bool(bot) and ":" in bot and "replace" not in bot.lower()
        and chat.startswith("-") and chat[1:].isdigit()),
        "Configure BOT_TOKEN and a negative ADMIN_CHAT_ID together, or leave both unset.")
    slack = [value("SLACK_BOT_TOKEN"), value("SLACK_SIGNING_SECRET")]
    check("slack", not any(slack) or all(slack),
          "Configure both Slack credentials, or leave both unset.")
    email = [value(key) for key in ("EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST")]
    check("email", not any(email) or all(email),
          "Configure EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_IMAP_HOST and EMAIL_SMTP_HOST together.")
    for key, default in (("EMAIL_IMAP_PORT", "993"), ("EMAIL_SMTP_PORT", "587")):
        port = value(key) or default
        check(key.lower(), port.isdigit() and 0 < int(port) < 65536,
              f"Set a valid TCP port in {key}.")
    hops = value("TRUSTED_PROXY_HOPS") or "0"
    check("proxy_hops", hops.isdigit() and 0 <= int(hops) <= 4,
          "Set TRUSTED_PROXY_HOPS to the exact number of trusted reverse proxies.")
    return checks


def compose_environment(env_file: Path, files: list[Path]) -> dict[str, str]:
    """Render the effective dashboard env; never echo Compose output/secrets."""
    command = ["docker", "compose", "--env-file", str(env_file.resolve())]
    for file in files:
        command.extend(["-f", str(file.resolve())])
    command.extend(["config", "--format", "json"])
    child_env = dict(os.environ, SPOOLDESK_ENV_FILE=str(env_file.resolve()))
    try:
        result = subprocess.run(command, env=child_env, capture_output=True,
                                text=True, encoding="utf-8", timeout=30, check=True)
        environment = json.loads(result.stdout)["services"]["dashboard"]["environment"]
        if not isinstance(environment, dict):
            raise ValueError("Invalid service environment")
        return {key: "" if value is None else str(value) for key, value in environment.items()}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        # Both stderr and the rendered config can contain passwords. Return
        # only the action; never chain the original subprocess exception.
        raise ValueError("Compose configuration could not be resolved. Check Docker Compose, selected files and required variables.") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--compose-file", type=Path, action="append", default=[],
                        help="Repeat in deployment order to check the effective dashboard environment")
    parser.add_argument("--local", action="store_true", help="Allow HTTP and development cookie/key settings")
    args = parser.parse_args()
    if not args.env_file.is_file():
        print(json.dumps({"ok": False, "error": "Environment file does not exist"}))
        return 1
    if args.compose_file:
        try:
            config = compose_environment(args.env_file, args.compose_file)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
    else:
        from dotenv import dotenv_values
        config = {**dotenv_values(args.env_file, interpolate=False), **os.environ}
    checks = evaluate(config, public_url=args.public_url, production=not args.local)
    ok = all(item["ok"] for item in checks)
    print(json.dumps({"ok": ok, "checks": checks,
                      "scope": "Offline configuration only; external delivery, restore, DNS and TLS need a staging rehearsal."}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
