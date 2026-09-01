"""Provider-agnostic LLM client interface.

Use cases call `client.complete(...)` and do not care which provider answers,
which is what made the provider a thing an operator can choose rather than a
thing baked into a deployment. What they pick lives in the database — see
src/ai/registry.py for the catalogue and src/services/secret_store.py for
where the credential goes — and falls back to the environment, so an instance
configured before any of this existed keeps working with nothing changed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LLMResponse:
    """Normalized LLM response across providers."""

    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0


class LLMUnavailable(Exception):
    """Raised when no LLM is configured or all providers failed.

    Callers MUST catch this and degrade gracefully (skip AI suggestion, don't
    surface to the user). The bot keeps working without AI.
    """


class LLMClient(Protocol):
    """Minimal contract for any LLM provider."""

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_output_tokens: int = 256,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Single-turn completion. Raises LLMUnavailable on hard failure."""
        ...


#: Built clients, keyed by the configuration that produced them. Keyed rather
#: than a single slot because the provider is now a runtime setting: with one
#: slot, changing it in the dashboard saved the choice and kept answering from
#: the old model, which looks exactly like the setting not working.
_clients: dict[tuple, LLMClient] = {}


def reset_cache() -> None:
    """Drop every built client. For tests; ordinary changes re-key themselves."""
    _clients.clear()


def _fingerprint(secret: str) -> str:
    """Enough of a key to notice it changed, not enough to be one."""
    import hashlib
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16] if secret else ""


async def get_client() -> Optional[LLMClient]:
    """The configured LLM client, or None.

    None is a normal answer — no provider configured, no credential, or a
    credential this instance cannot decrypt — and every caller degrades to
    "no AI" rather than failing. The bot works without it.

    Configuration comes from the database so an operator can change it
    without a restart, and falls back to the environment so an instance set
    up before any of this existed keeps working untouched.
    """
    from src.ai import registry
    from src.services import settings_service

    provider_id = await settings_service.get_ai_provider()
    spec = registry.provider(provider_id)
    if spec is None:  # pragma: no cover — get_ai_provider already falls back
        logger.warning("Unknown AI provider %r configured.", provider_id)
        return None

    model = await settings_service.get_ai_model()
    host = await settings_service.get_ai_host() or spec.host_hint
    key = "" if not spec.needs_key else (
        await settings_service.get_ai_key(provider_id) or "")

    if spec.needs_key and not key:
        if await settings_service.ai_key_is_stored(provider_id):
            # There is one; it did not decrypt. Almost always a database
            # restored without its secrets key — a different problem from
            # never having set one, and a different thing to tell somebody.
            logger.warning(
                "An API key is stored for %s but could not be decrypted. "
                "Re-enter it in the dashboard; see src/services/secret_store.py.",
                spec.label,
            )
        else:
            logger.info("No API key for %s — AI features disabled.", spec.label)
        return None

    cache_key = (provider_id, model, host, _fingerprint(key))
    if (existing := _clients.get(cache_key)) is not None:
        return existing

    try:
        client = _build(provider_id, model=model, key=key, host=host)
    except ImportError as e:
        logger.warning("%s SDK not installed — AI disabled: %s", spec.label, e)
        return None

    _clients[cache_key] = client
    logger.info("LLM client ready: %s (%s)", spec.label, model)
    return client


def _build(provider_id: str, *, model: str, key: str, host: str) -> LLMClient:
    """The one place a provider id becomes an object.

    Imports are deferred per branch so an instance using one provider does
    not need another's dependencies present to start.
    """
    if provider_id == "gemini":
        from src.ai.providers.gemini import GeminiClient
        return GeminiClient(api_key=key, model=model)

    from src.ai.providers import rest
    if provider_id == "anthropic":
        return rest.AnthropicClient(api_key=key, model=model, base_url=host)
    if provider_id == "openai":
        return rest.OpenAIClient(api_key=key, model=model, base_url=host)
    if provider_id == "ollama":
        return rest.OllamaClient(model=model, base_url=host)

    raise ValueError(f"no client for provider {provider_id!r}")
