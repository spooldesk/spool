"""Gemini (Google AI Studio) provider for LLMClient.

Uses the modern `google-genai` SDK (the older `google-generativeai` is deprecated).
Free tier — no card required, just an API key from aistudio.google.com.
Rate-limit on free tier returns 429; we surface that as LLMUnavailable so
callers degrade gracefully.
"""

from __future__ import annotations

import asyncio
import logging
import time

from src.ai.client import LLMResponse, LLMUnavailable, provider_error_code

logger = logging.getLogger(__name__)

# Hard wall-clock timeout — if Gemini hangs, we drop the suggestion.
# The user-facing flow MUST NOT wait on AI; this just bounds the background task.
_TIMEOUT_SEC = 8.0


class GeminiClient:
    """Async wrapper around google-genai with timeouts and error normalization."""

    def __init__(self, *, api_key: str, model: str) -> None:
        # Lazy import keeps top-level imports clean and lets the bot start
        # even if the SDK isn't installed (factory checks).
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model_name = model
        # Detect whether this is a 2.5+ model — those have built-in "thinking"
        # which eats output tokens on short tasks. We disable it for classification.
        self._supports_thinking = "2.5" in model or "thinking" in model.lower()

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_output_tokens: int = 256,
        temperature: float = 0.2,
        timeout_seconds: float = _TIMEOUT_SEC,
    ) -> LLMResponse:
        from google.genai import types

        config_kwargs = {
            "system_instruction": system,
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        }
        # For 2.5 models, disable thinking on short classification tasks —
        # otherwise the reasoning budget eats the entire output and .text is empty.
        if self._supports_thinking:
            try:
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            except Exception:
                # Older SDK versions may not have ThinkingConfig — ignore.
                pass

        config = types.GenerateContentConfig(**config_kwargs)

        started = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model_name,
                    contents=user,
                    config=config,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("Gemini timeout after %ss", timeout_seconds)
            raise LLMUnavailable("Gemini timed out", code="timeout") from None
        except Exception as e:
            # Includes ResourceExhausted (429), permission errors, network issues
            status = getattr(e, "code", None)
            status = status if isinstance(status, int) else None
            code = provider_error_code(status)
            logger.warning("Gemini call failed (%s, status=%s)", code, status)
            raise LLMUnavailable(f"Gemini {code}", code=code, status_code=status) from None

        latency_ms = int((time.monotonic() - started) * 1000)

        # Extract text. New SDK returns .text directly; fall back to candidates if empty.
        text = ""
        try:
            text = (resp.text or "").strip()
        except Exception:
            try:
                candidates = getattr(resp, "candidates", None) or []
                if candidates:
                    parts = getattr(candidates[0].content, "parts", None) or []
                    text = "".join(getattr(p, "text", "") or "" for p in parts).strip()
            except Exception:
                text = ""

        tokens_in = tokens_out = 0
        try:
            usage = getattr(resp, "usage_metadata", None)
            if usage:
                tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0)
                tokens_out = int(getattr(usage, "candidates_token_count", 0) or 0)
        except Exception:
            pass

        if not text:
            # Empty response counts as unavailable — don't surface blank suggestions.
            # Common causes: safety filters, all output tokens consumed by thinking,
            # or hitting max_output_tokens before producing visible text.
            raise LLMUnavailable(
                f"Gemini returned empty response "
                f"(tokens_out={tokens_out}, latency={latency_ms}ms)", code="empty"
            )

        return LLMResponse(
            text=text,
            model=self._model_name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
        )
