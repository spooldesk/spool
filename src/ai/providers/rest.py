"""Anthropic, OpenAI, OpenRouter and Ollama, over their HTTP APIs.

One file rather than three because they are the same request with different
JSON around it: post a system prompt and a user message, read back some text
and a token count. What differs is a URL, a header and three field names, and
splitting that across three files would mean three copies of the timeout, the
error normalisation and the empty-response rule — which is how two of them
end up behaving differently for no reason anybody chose.

Called over `httpx`, which is already a dependency, rather than each vendor's
SDK. Three more SDKs would be three more things to pin, three more release
cadences, and a larger image, in exchange for wrapping a single POST.

Gemini keeps its own module: it genuinely needs the vendor SDK, because its
"thinking" budget has to be turned off explicitly or short classification
tasks come back empty.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from src.ai.client import LLMResponse, LLMUnavailable, provider_error_code

logger = logging.getLogger(__name__)

#: Background suggestions stay short; interactive callers supply their own budget.
_TIMEOUT_SEC = 8.0


class _RestClient:
    """Shared machinery. Subclasses describe their own request and response."""

    #: For log lines and error messages.
    name = "REST"

    def __init__(self, *, api_key: str = "", model: str, base_url: str = "") -> None:
        self._api_key = api_key
        self._model_name = model
        self._base_url = (base_url or self.default_base_url).rstrip("/")

    # ---- what a subclass fills in ------------------------------------

    default_base_url = ""

    def _url(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _body(self, *, system: str, user: str, max_output_tokens: int,
              temperature: float) -> dict[str, Any]:
        raise NotImplementedError

    def _parse(self, payload: dict[str, Any]) -> tuple[str, int, int]:
        """(text, tokens_in, tokens_out)."""
        raise NotImplementedError

    # ---- the part that is the same for all of them --------------------

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_output_tokens: int = 256,
        temperature: float = 0.2,
        timeout_seconds: float = _TIMEOUT_SEC,
    ) -> LLMResponse:
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=min(8.0, timeout_seconds))) as http:
                resp = await http.post(
                    self._url(),
                    headers=self._headers(),
                    json=self._body(system=system, user=user,
                                    max_output_tokens=max_output_tokens,
                                    temperature=temperature),
                )
        except httpx.TimeoutException:
            logger.warning("%s timed out after %ss", self.name, timeout_seconds)
            raise LLMUnavailable(f"{self.name} timed out", code="timeout") from None
        except httpx.HTTPError as e:
            logger.warning("%s connection failed (%s)", self.name, type(e).__name__)
            raise LLMUnavailable(f"{self.name} connection failed", code="network") from None

        latency_ms = int((time.monotonic() - started) * 1000)

        if resp.status_code >= 400:
            # Error bodies can echo submitted secrets or prompts, including via proxies.
            code = provider_error_code(resp.status_code)
            logger.warning("%s returned %s (%s)", self.name, resp.status_code, code)
            raise LLMUnavailable(f"{self.name} HTTP {resp.status_code} ({code})",
                                 code=code, status_code=resp.status_code)

        try:
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError("response_shape")
            if payload.get("stop_reason") in {"max_tokens", "refusal"}:
                code = "truncated" if payload["stop_reason"] == "max_tokens" else "refusal"
                raise LLMUnavailable(f"{self.name} {code}", code=code)
            text, tokens_in, tokens_out = self._parse(payload)
            if not isinstance(text, str):
                raise ValueError("text_shape")
        except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError):
            logger.warning("%s returned an unreadable response", self.name)
            raise LLMUnavailable(f"{self.name} returned an unreadable response", code="invalid_response") from None

        if not text.strip():
            raise LLMUnavailable(
                f"{self.name} returned an empty response "
                f"(tokens_out={tokens_out}, latency={latency_ms}ms)", code="empty"
            )

        return LLMResponse(
            text=text.strip(),
            model=self._model_name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
        )


class AnthropicClient(_RestClient):
    name = "Anthropic"
    default_base_url = "https://api.anthropic.com"

    def _url(self) -> str:
        return f"{self._base_url}/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            # Pinned deliberately. The API is versioned by header, so leaving
            # it out or floating it means a future change to the response
            # shape arrives unannounced on somebody's production instance.
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def _body(self, *, system, user, max_output_tokens, temperature):
        body = {
            "model": self._model_name,
            "max_tokens": max_output_tokens,
            # A first-class field here, not a pseudo-message.
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        # Opus 4.7+ and newer model generations reject custom sampling values.
        # Leave provider defaults intact; guide consistency through the system prompt.
        # 5.5 always thinks. The assistant budgets thinking tokens explicitly.
        if self._model_name in {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"}:
            body["output_config"] = {"effort": "low"}
        elif self._model_name in {"claude-opus-5", "claude-sonnet-5"}:
            body["thinking"] = {"type": "disabled"}
        return body

    def _parse(self, payload):
        blocks = payload.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        usage = payload.get("usage") or {}
        return (text,
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0))


class OpenAIClient(_RestClient):
    name = "OpenAI"
    default_base_url = "https://api.openai.com"

    def _url(self) -> str:
        return f"{self._base_url}/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _body(self, *, system, user, max_output_tokens, temperature):
        return {
            "model": self._model_name,
            "max_completion_tokens": max_output_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

    def _parse(self, payload):
        choices = payload.get("choices") or []
        text = (choices[0].get("message") or {}).get("content") or "" if choices else ""
        usage = payload.get("usage") or {}
        return (text,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0))


class OpenRouterClient(OpenAIClient):
    """OpenAI-compatible gateway with its own credential and model identifiers."""

    name = "OpenRouter"
    default_base_url = "https://openrouter.ai/api"

    def __init__(self, *, api_key: str, model: str) -> None:
        # Explicit provider selection, never an implicit redirect of another key.
        super().__init__(api_key=api_key, model=model)

    def _body(self, *, system, user, max_output_tokens, temperature):
        body = super()._body(system=system, user=user,
                             max_output_tokens=max_output_tokens, temperature=temperature)
        body["max_tokens"] = body.pop("max_completion_tokens")
        # Some reasoning models reject sampling overrides. Use model defaults.
        body.pop("temperature")
        body["stream"] = False
        if self._model_name in {"anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5",
                                "anthropic/claude-fable-5.1"}:
            body["reasoning"] = {"effort": "low", "exclude": True}
        elif self._model_name in {"anthropic/claude-opus-5", "anthropic/claude-sonnet-5"}:
            body["reasoning"] = {"enabled": False}
        return body

    def _parse(self, payload):
        # Gateway failures can arrive inside HTTP 200. Never echo raw error details.
        if "error" in payload:
            error = payload["error"]
            status = error.get("code") if isinstance(error, dict) else None
            status = status if type(status) is int and 400 <= status <= 599 else None
            code = provider_error_code(status)
            raise LLMUnavailable(f"OpenRouter failed ({code})", code=code, status_code=status)
        choices = payload.get("choices") or []
        if choices:
            finish = choices[0].get("finish_reason")
            message = choices[0].get("message") or {}
            code = {"length": "truncated", "content_filter": "refusal",
                    "error": "unavailable"}.get(finish)
            if message.get("refusal"):
                code = "refusal"
            if code:
                raise LLMUnavailable(f"OpenRouter {code}", code=code)
        # Only message.content is an answer; reasoning fields remain private.
        return super()._parse(payload)


class OllamaClient(_RestClient):
    """A model on the instance's own hardware.

    No key, and no cost: the savings report shows nothing saved for this
    provider because nothing was spent per token. What it costs is the
    machine, which is not a number this application can know.
    """

    name = "Ollama"
    default_base_url = "http://localhost:11434"

    def _url(self) -> str:
        return f"{self._base_url}/api/chat"

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _body(self, *, system, user, max_output_tokens, temperature):
        return {
            "model": self._model_name,
            # One response, not a stream. The callers here want a whole answer
            # or nothing; there is no UI showing tokens as they arrive.
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": temperature,
                        "num_predict": max_output_tokens},
        }

    def _parse(self, payload):
        text = (payload.get("message") or {}).get("content") or ""
        return (text,
                int(payload.get("prompt_eval_count") or 0),
                int(payload.get("eval_count") or 0))
