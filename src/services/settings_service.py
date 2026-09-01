"""Runtime-editable settings stored in the DB (AppSetting key-value table).

Difference from src.config.Settings:
    - config.Settings = read-only, from .env, needs a restart to change
      (tokens, chat IDs, passwords — infra-level).
    - this module = editable from the dashboard at runtime, no restart
      (brand name, admin tags per category, AI toggles — content-level).

Each setting has a canonical key constant + a default. Defaults live here so a
fresh DB behaves sensibly before anyone touches the settings screen.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import select

from src.database.db import db_session
from src.database.models import AppSetting

logger = logging.getLogger(__name__)


# ==================== Keys ====================

KEY_CATEGORY_ADMIN_TAGS = "category_admin_tags"  # JSON: {category_name: "@a @b"}
KEY_AI_ENABLED = "ai_enabled"                    # "1" / "0"
KEY_AI_DEFLECTION = "ai_deflection_enabled"      # "1" = AI answers the user before an operator
KEY_AI_BLACKLIST = "ai_blacklist_categories"     # JSON list of category fragments
# Shadow mode. The AI runs, is measured and is logged, but nothing it
# produces reaches a customer or an operator. The point is to be able to
# answer "is it any good on OUR tickets" before trusting it with one.
KEY_AI_SHADOW = "ai_shadow_mode"                 # "1" / "0"
# What the savings report assumes. Every one of them is a guess about YOUR
# team, so every one of them is editable and printed on the report itself.
KEY_AI_PRICE_IN = "ai_price_per_1m_in"           # your provider's input price, USD
KEY_AI_PRICE_OUT = "ai_price_per_1m_out"         # ...and output
KEY_AI_ASSIST_MINUTES = "ai_minutes_per_assist"  # minutes an accepted draft saves
KEY_ASK_CATEGORY = "ask_category"                # "1" = ask user to pick a category
KEY_BRAND_NAME = "brand_name"                    # white-label: support team/service name
KEY_REDIRECT_ENABLED = "redirect_enabled"        # "1" = redirect a matched team elsewhere
KEY_REDIRECT_TEAM_MATCH = "redirect_team_match"  # case-insensitive substring of team name
KEY_REDIRECT_TARGET = "redirect_target"          # text/link shown to the redirected user
KEY_ENABLED_LANGUAGES = "enabled_languages"      # JSON list of offered language codes
KEY_DEFAULT_LANGUAGE = "default_language"        # fallback / pre-pick language code
KEY_ADMIN_NO_TAG = "admin_no_tag_usernames"      # JSON list: usernames never @-tagged in lists
KEY_DASH_SECRET = "dash_secret_key"              # stable secret for signing dashboard session cookies
KEY_DASH_LANGUAGE = "dash_language"              # operator dashboard UI language (en/ru)
# Whether published articles are also served as pages on the open web. The
# widget can read them either way; this decides whether they have a URL an
# operator can paste into a reply and a search engine can index. Off means
# the help centre exists only inside the chat panel.
KEY_HELP_PUBLIC = "help_public"                  # "1" / "0"
# Which LLM answers, and as which model. Content-level rather than infra by a
# hair: the choice is a preference, and the credential it needs is stored
# encrypted (src/services/secret_store.py) precisely so that changing provider
# does not require somebody with SSH.
KEY_AI_PROVIDER = "ai_provider"                  # a registry.PROVIDERS id
KEY_AI_MODEL = "ai_model"                        # a model name, listed or not
KEY_AI_HOST = "ai_host"                          # base URL, for self-hosted
#: One per provider, so switching back does not mean pasting the key again.
KEY_AI_KEY_PREFIX = "ai_key_"                    # + provider id; encrypted


# ==================== Defaults ====================

_DEFAULTS: dict[str, str] = {
    KEY_CATEGORY_ADMIN_TAGS: json.dumps({}),  # no per-category tag rules out of the box
    KEY_AI_ENABLED: "1",                  # AI suggestions on by default (if key present)
    KEY_AI_DEFLECTION: "0",               # deflection is opt-in — enable from AI config
    KEY_AI_BLACKLIST: json.dumps([]),     # empty: nothing withheld from the AI by default
    # Gemini 2.5 Flash list price at the time of writing. Deliberately not
    # hidden in code: prices move, and a stale one quietly inflates the result.
    KEY_AI_PRICE_IN: "0.30",
    KEY_AI_PRICE_OUT: "2.50",
    # Conservative on purpose. An accepted suggestion saves the writing, not
    # the reading, the diagnosis or the follow-up.
    KEY_AI_ASSIST_MINUTES: "3",
    KEY_ASK_CATEGORY: "1",                # ask the user to pick a category by default
    KEY_BRAND_NAME: "Support",            # neutral default; rebrandable from Settings
    KEY_REDIRECT_ENABLED: "0",            # off by default → clean white-label start
    KEY_REDIRECT_TEAM_MATCH: "",
    KEY_REDIRECT_TARGET: "",
    KEY_ENABLED_LANGUAGES: json.dumps(["ru", "uk", "en"]),  # all three by default
    KEY_DEFAULT_LANGUAGE: "uk",           # matches i18n.DEFAULT_LANGUAGE
    KEY_ADMIN_NO_TAG: json.dumps([]),     # nobody excluded from @-tagging by default
    # On, because "published" already means a customer may read it, and an
    # article nobody can link to does not deflect the ticket it was written
    # to prevent. Turn it off for an instance whose articles are internal.
    KEY_HELP_PUBLIC: "1",
    # Gemini, because it is the one with a free tier that needs no card.
    KEY_AI_PROVIDER: "gemini",
    KEY_AI_MODEL: "gemini-2.5-flash",
    KEY_AI_HOST: "",
}


# ==================== Core get/set ====================


async def get_value(key: str, default: Optional[str] = None) -> str:
    """Read a setting. Falls back to the module default, then to `default` arg."""
    async with db_session() as session:
        row = (await session.execute(
            select(AppSetting).where(AppSetting.key == key)
        )).scalar_one_or_none()
    if row is not None:
        return row.value
    if key in _DEFAULTS:
        return _DEFAULTS[key]
    return default if default is not None else ""


async def set_value(key: str, value: str, *, updated_by: Optional[int] = None) -> None:
    """Upsert a setting."""
    async with db_session() as session:
        row = (await session.execute(
            select(AppSetting).where(AppSetting.key == key)
        )).scalar_one_or_none()
        if row is None:
            row = AppSetting(key=key, value=value, updated_by=updated_by)
            session.add(row)
        else:
            row.value = value
            row.updated_by = updated_by
        await session.commit()
    logger.info(f"Setting {key!r} updated by {updated_by}")


# ==================== Typed accessors ====================


async def get_category_admin_tags() -> dict[str, str]:
    """Return {category_name: 'space-separated @tags'}. Empty dict on parse error."""
    raw = await get_value(KEY_CATEGORY_ADMIN_TAGS)
    try:
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Bad category_admin_tags JSON, returning empty: {e}")
    return {}


async def set_category_admin_tags(mapping: dict[str, str], *, updated_by: int) -> None:
    await set_value(
        KEY_CATEGORY_ADMIN_TAGS,
        json.dumps(mapping, ensure_ascii=False),
        updated_by=updated_by,
    )


async def get_tags_for_category(category: str) -> str:
    """Tags string for one category, or '' if none configured. Case-insensitive."""
    if not category:
        return ""
    tags = await get_category_admin_tags()
    # exact first, then case-insensitive
    if category in tags:
        return tags[category]
    low = category.lower()
    for k, v in tags.items():
        if k.lower() == low:
            return v
    return ""


# ---- AI toggles ----

async def get_ai_enabled() -> bool:
    return (await get_value(KEY_AI_ENABLED)) == "1"


async def set_ai_enabled(enabled: bool, *, updated_by: int) -> None:
    await set_value(KEY_AI_ENABLED, "1" if enabled else "0", updated_by=updated_by)


async def get_ai_deflection_enabled() -> bool:
    """AI answers the user right after ticket creation (opt-in)."""
    return (await get_value(KEY_AI_DEFLECTION)) == "1"


async def set_ai_deflection_enabled(enabled: bool, *, updated_by: int) -> None:
    await set_value(KEY_AI_DEFLECTION, "1" if enabled else "0", updated_by=updated_by)


async def get_ai_blacklist() -> list[str]:
    """Category fragments that must never be sent to the LLM. Lowercased."""
    raw = await get_value(KEY_AI_BLACKLIST)
    try:
        data = json.loads(raw) if raw else []
        if isinstance(data, list):
            return [str(x).strip().lower() for x in data if str(x).strip()]
    except (json.JSONDecodeError, ValueError):
        pass
    return []


async def set_ai_blacklist(fragments: list[str], *, updated_by: int) -> None:
    cleaned = [f.strip().lower() for f in fragments if f and f.strip()]
    await set_value(KEY_AI_BLACKLIST, json.dumps(cleaned, ensure_ascii=False), updated_by=updated_by)


async def is_category_ai_blacklisted(category: str | None) -> bool:
    """True if the category matches any blacklist fragment (substring, ci)."""
    if not category:
        return False
    low = category.lower()
    return any(frag in low for frag in await get_ai_blacklist())


# ---- Ask-category toggle ----

async def get_ask_category() -> bool:
    """Whether the bot asks the user to pick a category before describing the issue."""
    return (await get_value(KEY_ASK_CATEGORY)) == "1"


async def set_ask_category(enabled: bool, *, updated_by: int) -> None:
    await set_value(KEY_ASK_CATEGORY, "1" if enabled else "0", updated_by=updated_by)


# ---- Branding (white-label) ----

async def get_brand_name() -> str:
    """Support team/service name shown in user-facing texts. Never empty."""
    name = (await get_value(KEY_BRAND_NAME)).strip()
    return name or _DEFAULTS[KEY_BRAND_NAME]


async def set_brand_name(name: str, *, updated_by: int) -> None:
    await set_value(KEY_BRAND_NAME, (name or "").strip(), updated_by=updated_by)


# ---- Which LLM, and its credential ----

async def get_ai_provider() -> str:
    from src.ai import registry
    chosen = (await get_value(KEY_AI_PROVIDER)).strip().lower()
    # An id from a config somebody edited by hand, or from a release that
    # dropped a provider. Falling back beats a dashboard that cannot render.
    return chosen if registry.is_known(chosen) else registry.DEFAULT_PROVIDER


async def get_ai_model() -> str:
    from src.ai import registry
    name = (await get_value(KEY_AI_MODEL)).strip()
    return name or registry.default_model_for(await get_ai_provider())


async def get_ai_host() -> str:
    """Base URL for a provider that is somewhere rather than something."""
    return (await get_value(KEY_AI_HOST)).strip()


async def set_ai_engine(*, provider: str, model: str, host: str = "",
                        updated_by: int) -> None:
    """Change which LLM answers, and move the report's prices with it.

    The prices follow the model here rather than in the route that calls
    this, because the rule is about the two values agreeing and not about
    HTTP. Left in the route, every other caller skipped it and the savings
    report went on billing Gemini's rates for somebody else's tokens — a
    number that is wrong and looks entirely fine.

    Only for a model the catalogue knows the price of, and only when the
    model actually changed: an operator who typed their own price should not
    have it overwritten by saving the same form twice.
    """
    from src.ai import registry
    if not registry.is_known(provider):
        raise ValueError(f"unknown provider: {provider!r}")

    chosen = (model or "").strip() or registry.default_model_for(provider)
    previous = await get_value(KEY_AI_MODEL)

    await set_value(KEY_AI_PROVIDER, provider, updated_by=updated_by)
    await set_value(KEY_AI_MODEL, chosen, updated_by=updated_by)
    await set_value(KEY_AI_HOST, (host or "").strip(), updated_by=updated_by)

    if chosen != previous:
        if (prices := registry.prices_for(provider, chosen)) is not None:
            await set_ai_pricing(price_in=prices[0], price_out=prices[1],
                                 updated_by=updated_by)


async def get_ai_key(provider: str) -> str | None:
    """The stored key in plaintext, the environment's if none is stored, or
    None.

    None has two causes and the caller cannot tell them apart, which is
    deliberate: nothing was ever set, or what was set cannot be decrypted
    because this database was restored without its secrets key. Both mean
    "there is no usable credential", and both are reported the same way.
    """
    from src.ai import registry
    from src.services import secret_store

    stored = (await get_value(f"{KEY_AI_KEY_PREFIX}{provider}")).strip()
    if stored:
        return secret_store.decrypt(stored)

    p = registry.provider(provider)
    if p:
        import os
        return os.getenv(p.env_var, "").strip() or None
    return None


async def set_ai_key(provider: str, key: str, *, updated_by: int) -> None:
    """Store a key, or clear it with an empty string."""
    from src.services import secret_store
    value = "" if not key.strip() else secret_store.encrypt(key.strip())
    await set_value(f"{KEY_AI_KEY_PREFIX}{provider}", value, updated_by=updated_by)


async def ai_key_is_stored(provider: str) -> bool:
    """Whether a key sits in the database, readable or not.

    Distinct from get_ai_key() returning something: a key that is present but
    undecryptable should be reported as "there is one and it is broken", not
    as "there is none" — otherwise the fix looks like a first-time setup.
    """
    return bool((await get_value(f"{KEY_AI_KEY_PREFIX}{provider}")).strip())


# ---- Help centre ----

async def get_help_public() -> bool:
    """Whether the help centre answers on the open web, not just in the widget."""
    return (await get_value(KEY_HELP_PUBLIC)) == "1"


async def set_help_public(enabled: bool, *, updated_by: int) -> None:
    await set_value(KEY_HELP_PUBLIC, "1" if enabled else "0", updated_by=updated_by)


# ---- Team redirect (configurable per-team routing to another bot) ----

async def get_redirect_config() -> tuple[bool, str, str]:
    """(enabled, team_match, target). Redirect fires only when fully configured."""
    enabled = (await get_value(KEY_REDIRECT_ENABLED)) == "1"
    team_match = (await get_value(KEY_REDIRECT_TEAM_MATCH)).strip()
    target = (await get_value(KEY_REDIRECT_TARGET)).strip()
    return enabled, team_match, target


async def set_redirect_config(*, enabled: bool, team_match: str, target: str,
                              updated_by: int) -> None:
    await set_value(KEY_REDIRECT_ENABLED, "1" if enabled else "0", updated_by=updated_by)
    await set_value(KEY_REDIRECT_TEAM_MATCH, (team_match or "").strip(), updated_by=updated_by)
    await set_value(KEY_REDIRECT_TARGET, (target or "").strip(), updated_by=updated_by)


# ---- Languages (configurable multilingual) ----

def _valid_lang_codes() -> list[str]:
    from src.utils.i18n import SUPPORTED_LANGUAGES
    return list(SUPPORTED_LANGUAGES.keys())


async def get_enabled_languages() -> list[str]:
    """Offered language codes, in display order. Never empty; valid codes only."""
    valid = _valid_lang_codes()
    raw = await get_value(KEY_ENABLED_LANGUAGES)
    try:
        data = json.loads(raw) if raw else []
    except (json.JSONDecodeError, ValueError):
        data = []
    codes = [c for c in data if c in valid]
    return codes or valid  # fall back to all if misconfigured/empty


async def set_enabled_languages(codes: list[str], *, updated_by: int) -> None:
    valid = _valid_lang_codes()
    cleaned = [c for c in codes if c in valid]
    if not cleaned:
        cleaned = valid  # never persist an empty set (would lock out the picker)
    await set_value(KEY_ENABLED_LANGUAGES, json.dumps(cleaned), updated_by=updated_by)


async def get_default_language() -> str:
    """Fallback/pre-pick language. Guaranteed to be an enabled language."""
    enabled = await get_enabled_languages()
    code = (await get_value(KEY_DEFAULT_LANGUAGE)).strip()
    if code in enabled:
        return code
    return enabled[0]


async def set_default_language(code: str, *, updated_by: int) -> None:
    if code in _valid_lang_codes():
        await set_value(KEY_DEFAULT_LANGUAGE, code, updated_by=updated_by)


# ---- Admin usernames that must never be @-tagged in lists ----

async def get_admin_no_tag_usernames() -> set[str]:
    """Lowercased usernames (no @) that should be shown without a mention link."""
    raw = await get_value(KEY_ADMIN_NO_TAG)
    try:
        data = json.loads(raw) if raw else []
        if isinstance(data, list):
            return {str(x).strip().lstrip("@").lower() for x in data if str(x).strip()}
    except (json.JSONDecodeError, ValueError):
        pass
    return set()


async def set_admin_no_tag_usernames(usernames: list[str], *, updated_by: int) -> None:
    cleaned = sorted({u.strip().lstrip("@") for u in usernames if u and u.strip()})
    await set_value(KEY_ADMIN_NO_TAG, json.dumps(cleaned, ensure_ascii=False), updated_by=updated_by)


# ---- Dashboard UI language (instance-level; en is the product default) ----

async def get_dash_language() -> str:
    from src.dashboard.i18n import DEFAULT_DASH_LANGUAGE, SUPPORTED_DASH_LANGUAGES
    code = (await get_value(KEY_DASH_LANGUAGE, DEFAULT_DASH_LANGUAGE)).strip()
    return code if code in SUPPORTED_DASH_LANGUAGES else DEFAULT_DASH_LANGUAGE


async def set_dash_language(code: str, *, updated_by: int = 0) -> None:
    from src.dashboard.i18n import SUPPORTED_DASH_LANGUAGES
    if code in SUPPORTED_DASH_LANGUAGES:
        await set_value(KEY_DASH_LANGUAGE, code, updated_by=updated_by)



async def get_ai_shadow_mode() -> bool:
    """True while the AI is being evaluated rather than used."""
    return (await get_value(KEY_AI_SHADOW, "0")) == "1"


async def set_ai_shadow_mode(enabled: bool, *, updated_by: int) -> None:
    await set_value(KEY_AI_SHADOW, "1" if enabled else "0", updated_by=updated_by)


# ---- Savings-report assumptions ----

async def _get_float(key: str, fallback: float) -> float:
    raw = (await get_value(key, "")).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    return value if value >= 0 else fallback


async def get_ai_pricing() -> tuple[float, float]:
    """USD per million tokens, (input, output)."""
    return (await _get_float(KEY_AI_PRICE_IN, 0.30),
            await _get_float(KEY_AI_PRICE_OUT, 2.50))


async def set_ai_pricing(*, price_in: float, price_out: float,
                         updated_by: int) -> None:
    """Just the token prices.

    Separate from set_savings_assumptions because these two follow the model
    and the third — minutes an accepted draft saves an operator — is a fact
    about the team. Switching from Gemini to Claude should not silently
    revise somebody's estimate of their own staff.
    """
    await set_value(KEY_AI_PRICE_IN, f"{max(0.0, price_in):.4f}", updated_by=updated_by)
    await set_value(KEY_AI_PRICE_OUT, f"{max(0.0, price_out):.4f}", updated_by=updated_by)


async def get_ai_assist_minutes() -> float:
    """Minutes an accepted suggestion saves the operator who accepted it."""
    return await _get_float(KEY_AI_ASSIST_MINUTES, 3.0)


async def set_savings_assumptions(*, price_in: float, price_out: float,
                                  assist_minutes: float, updated_by: int) -> None:
    await set_value(KEY_AI_PRICE_IN, f"{max(0.0, price_in):.4f}", updated_by=updated_by)
    await set_value(KEY_AI_PRICE_OUT, f"{max(0.0, price_out):.4f}", updated_by=updated_by)
    await set_value(KEY_AI_ASSIST_MINUTES, f"{max(0.0, assist_minutes):.1f}",
                    updated_by=updated_by)

# ---- Dashboard session secret (signs login cookies) ----

async def get_or_create_secret_key() -> str:
    """Return a stable secret for signing dashboard session cookies.

    Precedence: DASH_SECRET_KEY env var > persisted app_setting > freshly
    generated (and persisted). Persisting means sessions survive restarts;
    using a dedicated key (not the user's password) means rotating an admin
    password no longer logs everyone out.
    """
    import os
    import secrets

    env = os.getenv("DASH_SECRET_KEY")
    if env:
        return env
    val = (await get_value(KEY_DASH_SECRET, "")).strip()
    if not val:
        val = secrets.token_urlsafe(48)
        await set_value(KEY_DASH_SECRET, val)
        logger.info("Generated and stored a new dashboard secret key")
    return val


# ---- Website widget ----------------------------------------------------
#
# Off until someone turns it on. Everything else in this file configures
# behaviour the instance already had; this one opens a door to the public
# internet, and a door that appears by itself is not a door anyone chose.

KEY_WIDGET_ENABLED = "widget_enabled"      # "1" = the embed endpoints answer
KEY_WIDGET_ORIGINS = "widget_origins"      # newline/comma list of allowed sites
KEY_WIDGET_GREETING = "widget_greeting"    # first line a visitor sees


async def get_widget_enabled() -> bool:
    return (await get_value(KEY_WIDGET_ENABLED, "0")) == "1"


async def set_widget_enabled(enabled: bool, *, updated_by: int = 0) -> None:
    await set_value(KEY_WIDGET_ENABLED, "1" if enabled else "0", updated_by=updated_by)


async def get_widget_origins() -> list[str]:
    """Sites allowed to embed the widget, for CORS.

    This is a spam control, not a security boundary, and the difference is
    worth being clear about: browsers enforce it, and anything that is not a
    browser ignores it entirely. What actually protects the endpoints is the
    rate limiting in the widget routes. An empty list means any site — which
    is the right default for something that has to be pasted into a page and
    just work, given that it is off until switched on.
    """
    raw = await get_value(KEY_WIDGET_ORIGINS, "")
    parts = [p.strip().rstrip("/") for p in raw.replace(",", "\n").splitlines()]
    return [p for p in parts if p]


async def set_widget_origins(raw: str, *, updated_by: int = 0) -> None:
    await set_value(KEY_WIDGET_ORIGINS, (raw or "").strip(), updated_by=updated_by)


KEY_WIDGET_ASK_IDENTITY = "widget_ask_identity"   # "1" = ask before the first message
KEY_WIDGET_IDENTITY_SECRET = "widget_identity_secret"  # signs identities the host vouches for


async def get_widget_ask_identity() -> bool:
    """Whether to ask a visitor who they are before their first message.

    Off by default, and that default is a real trade-off rather than
    caution: every field in front of the box costs conversations that would
    otherwise have been started. A site with two support requests a week
    wants the name; a site drowning in them wants the message first and the
    name never.
    """
    return (await get_value(KEY_WIDGET_ASK_IDENTITY, "0")) == "1"


async def set_widget_ask_identity(on: bool, *, updated_by: int = 0) -> None:
    await set_value(KEY_WIDGET_ASK_IDENTITY, "1" if on else "0", updated_by=updated_by)


async def get_widget_identity_secret() -> str:
    """The shared secret a host site signs its logged-in visitors with.

    Empty means nobody is signing anything, and every identity the widget
    reports is therefore something a visitor typed. That is a perfectly
    normal way to run this — it is simply the difference between "says they
    are" and "is", and the ticket page says which one it is looking at.
    """
    return (await get_value(KEY_WIDGET_IDENTITY_SECRET, "")).strip()


async def set_widget_identity_secret(value: str, *, updated_by: int = 0) -> None:
    await set_value(KEY_WIDGET_IDENTITY_SECRET, (value or "").strip()[:200],
                    updated_by=updated_by)


async def get_widget_greeting() -> str:
    """The opening line, or empty to use the translated default."""
    return (await get_value(KEY_WIDGET_GREETING, "")).strip()


async def set_widget_greeting(text: str, *, updated_by: int = 0) -> None:
    await set_value(KEY_WIDGET_GREETING, (text or "").strip()[:300], updated_by=updated_by)
