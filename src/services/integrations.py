"""Owner-only connections; secrets are encrypted and never returned to forms."""
import json
import os
import re
from sqlalchemy import select
from src.config import channel_values
from src.database.db import db_session
from src.database.models import AdminUser, AppSetting
from src.services import secret_store

FIELDS = {
    "telegram": {"bot_token": "BOT_TOKEN", "admin_chat_id": "ADMIN_CHAT_ID"},
    "slack": {"bot_token": "SLACK_BOT_TOKEN", "signing_secret": "SLACK_SIGNING_SECRET",
              "public_url": "SLACK_PUBLIC_URL"},
    "email": {"address": "EMAIL_ADDRESS", "password": "EMAIL_PASSWORD",
              "login": "EMAIL_LOGIN", "imap_host": "EMAIL_IMAP_HOST", "imap_port": "EMAIL_IMAP_PORT",
              "smtp_host": "EMAIL_SMTP_HOST", "smtp_port": "EMAIL_SMTP_PORT",
              "from_name": "EMAIL_FROM_NAME", "subject": "EMAIL_SUBJECT"},
}
SECRETS = {"bot_token", "signing_secret", "password"}
DEFAULTS = {"imap_port": "993", "smtp_port": "587", "from_name": "Support",
            "subject": "Re: your support request", "admin_chat_id": "0"}


def _decode(channel, raw):
    if raw is None:
        values = {f: os.getenv(env, DEFAULTS.get(f, "")) for f, env in FIELDS[channel].items()}
        enabled = bool(values.get("bot_token") if channel != "email" else values.get("address"))
        return values, enabled, False
    data = json.loads(raw)
    values = {}
    unreadable = False
    for field in FIELDS[channel]:
        value = str(data.get("values", {}).get(field, DEFAULTS.get(field, "")))
        if field in SECRETS and value:
            plain = secret_store.decrypt(value)
            unreadable |= plain is None
            value = plain or ""
        values[field] = value
    return values, bool(data.get("enabled")), unreadable


async def state(channel: str) -> dict:
    if channel not in FIELDS:
        raise ValueError("integration_unknown")
    async with db_session() as session:
        row = await session.get(AppSetting, "integration_" + channel)
        try:
            values, enabled, unreadable = _decode(channel, row.value if row else None)
        except (ValueError, TypeError, AttributeError):
            values = {f: DEFAULTS.get(f, '') for f in FIELDS[channel]}
            enabled, unreadable = False, True
    required = {"telegram": ("bot_token",), "slack": ("bot_token", "signing_secret"),
                "email": ("address", "password", "imap_host", "smtp_host")}[channel]
    return {"channel": channel, "enabled": enabled,
            "configured": enabled and not unreadable and all(values[f] for f in required),
            "unreadable": unreadable, "source": "database" if row else "environment",
            "values": {f: v for f, v in values.items() if f not in SECRETS},
            "secrets_present": {f: bool(v) for f, v in values.items() if f in SECRETS}}


def _validate(channel, values, enabled):
    if any(len(v) > (2048 if f in SECRETS else 320) or any(c in v for c in '\r\n\x00')
           for f, v in values.items()):
        raise ValueError("integration_invalid")
    if channel == "telegram":
        if values["bot_token"] and not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", values["bot_token"]):
            raise ValueError("integration_invalid")
        chat = values["admin_chat_id"] or "0"
        if not re.fullmatch(r"-?\d{1,18}", chat):
            raise ValueError("integration_invalid")
        values["admin_chat_id"] = chat
        required = ("bot_token",)
    elif channel == "slack":
        from src.services.connection_urls import public_origin
        values['public_url'] = public_origin(values.get('public_url', ''))
        if values["bot_token"] and not values["bot_token"].startswith("xoxb-"):
            raise ValueError("integration_invalid")
        required = ("bot_token", "signing_secret")
    else:
        if values["address"] and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", values["address"]):
            raise ValueError("e_invalid_email")
        for field in ("imap_port", "smtp_port"):
            try:
                port = int(values[field])
            except ValueError:
                raise ValueError("integration_invalid") from None
            if not 1 <= port <= 65535:
                raise ValueError("integration_invalid")
        for field in ("imap_host", "smtp_host"):
            if values[field] and not re.fullmatch(r"[A-Za-z0-9._:\-]+", values[field]):
                raise ValueError("integration_invalid")
        values["address"] = values["address"].lower()
        required = ("address", "password", "imap_host", "smtp_host")
    if enabled and not all(values[f] for f in required):
        raise ValueError("integration_incomplete")


async def save(channel: str, submitted: dict[str, str], *, enabled: bool, actor_id: int):
    if channel not in FIELDS:
        raise ValueError("integration_unknown")
    async with db_session(write=True) as session:
        actor = await session.get(AdminUser, actor_id)
        if not actor or not actor.is_active or actor.role != "owner":
            raise ValueError("e_forbidden_section")
        key = "integration_" + channel
        row = await session.get(AppSetting, key)
        try:
            values, _, _ = _decode(channel, row.value if row else None)
        except (ValueError, TypeError, AttributeError):
            values = {f: DEFAULTS.get(f, '') for f in FIELDS[channel]}
        for field in FIELDS[channel]:
            value = str(submitted.get(field, "")).strip()
            if field in SECRETS:
                if submitted.get("clear_" + field) == "1":
                    values[field] = ""
                elif value:
                    values[field] = value
            elif field in submitted:
                values[field] = value
        _validate(channel, values, enabled)
        stored = {f: secret_store.encrypt(v) if f in SECRETS else v for f, v in values.items()}
        raw = json.dumps({"enabled": enabled, "values": stored})
        if row is None:
            session.add(AppSetting(key=key, value=raw, updated_by=actor_id))
        else:
            row.value, row.updated_by = raw, actor_id


async def refresh() -> None:
    async with db_session() as session:
        rows = {r.key: r.value for r in (await session.execute(select(AppSetting).where(
            AppSetting.key.in_(["integration_" + c for c in FIELDS])))).scalars()}
    snapshot = {}
    for channel, fields in FIELDS.items():
        raw = rows.get("integration_" + channel)
        if raw is None:
            continue
        try:
            values, enabled, unreadable = _decode(channel, raw)
        except (ValueError, TypeError, AttributeError):
            values, enabled, unreadable = {}, False, True
        for field, env in fields.items():
            snapshot[env] = values.get(field, "") if enabled and not unreadable else ""
    channel_values.install(snapshot)


async def save_widget(submitted: dict, *, actor_id: int):
    from urllib.parse import urlsplit
    raw = str(submitted.get('widget_origins', '')).strip()
    origins = [v.strip().rstrip('/') for v in raw.replace(',', '\n').splitlines() if v.strip()]
    if len(origins) > 50 or len(raw) > 8000:
        raise ValueError('integration_invalid')
    for origin in origins:
        try:
            parts = urlsplit(origin)
            if (parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password
                    or parts.path or parts.query or parts.fragment or any(c.isspace() for c in origin)):
                raise ValueError('integration_invalid')
            _ = parts.port
        except ValueError:
            raise ValueError('integration_invalid') from None
    greeting = str(submitted.get('widget_greeting', '')).strip()
    secret = str(submitted.get('widget_identity_secret', '')).strip()
    if len(greeting) > 300 or len(secret) > 200 or (secret and len(secret) < 32):
        raise ValueError('integration_invalid')
    values = {
        'widget_enabled': '1' if submitted.get('widget_enabled') == '1' else '0',
        'widget_origins': '\n'.join(dict.fromkeys(origins)),
        'widget_greeting': greeting,
        'widget_ask_identity': '1' if submitted.get('widget_ask_identity') == '1' else '0',
    }
    if 'widget_public_url' in submitted:
        from src.services.connection_urls import public_origin
        values['widget_public_url'] = public_origin(str(submitted['widget_public_url']))
    if submitted.get('clear_widget_identity_secret') == '1':
        values['widget_identity_secret'] = ''
    elif secret:
        values['widget_identity_secret'] = 'enc:' + secret_store.encrypt(secret)
    async with db_session(write=True) as session:
        actor = await session.get(AdminUser, actor_id)
        if not actor or not actor.is_active or actor.role != 'owner':
            raise ValueError('e_forbidden_section')
        for key, value in values.items():
            row = await session.get(AppSetting, key)
            if row:
                row.value, row.updated_by = value, actor_id
            else:
                session.add(AppSetting(key=key, value=value, updated_by=actor_id))
