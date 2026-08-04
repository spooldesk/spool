"""Provider-agnostic LLM client interface.

Use cases call `client.complete(...)` and don't care which provider answers.
Today the factory returns a Gemini client; switching to Groq/Ollama is a
matter of writing a new provider and changing one branch in `get_client()`.
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


_singleton: Optional[LLMClient] = None


def get_client() -> Optional[LLMClient]:
    """Build (or reuse) the configured LLM client.

    Returns None if no provider is configured — callers must handle this.
    Lazy-imports the concrete provider so the bot starts even without the SDK.
    """
    global _singleton
    if _singleton is not None:
        return _singleton

    from src.config import load_settings
    settings = load_settings()

    if not settings.gemini_api_key:
        logger.info("No GEMINI_API_KEY in env — AI features disabled.")
        return None

    try:
        from src.ai.providers.gemini import GeminiClient
    except ImportError as e:
        logger.warning(f"google-generativeai not installed — AI disabled: {e}")
        return None

    _singleton = GeminiClient(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    logger.info(f"✅ LLM client ready: Gemini ({settings.gemini_model})")
    return _singleton
