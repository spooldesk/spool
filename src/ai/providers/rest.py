"""Anthropic, OpenAI and Ollama, over their HTTP APIs.

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

from src.ai.client import LLMResponse, LLMUnavailable

logger = logging.getLogger(__name__)

#: The same wall clock Gemini gets. Nothing a customer waits on depends on
#: this — it bounds a background task — but an LLM that has not answered in
#: eight seconds is not going to save an operator any time.
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
    ) -> LLMResponse:
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as http:
                resp = await http.post(
                    self._url(),
                    headers=self._headers(),
                    json=self._body(system=system, user=user,
                                    max_output_tokens=max_output_tokens,
                                    temperature=temperature),
                )
        except httpx.TimeoutException:
            logger.warning("%s timed out after %ss", self.name, _TIMEOUT_SEC)
            raise LLMUnavailable(f"{self.name} timed out") from None
        except httpx.HTTPError as e:
            logger.warning("%s call failed: %s: %s", self.name, type(e).__name__, e)
            raise LLMUnavailable(f"{type(e).__name__}: {e}") from e

        latency_ms = int((time.monotonic() - started) * 1000)

        if resp.status_code >= 400:
            # The provider's own words, truncated. A wrong model name, an
            # expired key and an exhausted quota all arrive here, and the
            # message is the only thing that tells them apart — so it is
            # carried through to the operator rather than flattened.
            detail = _first_line(resp.text)
            logger.warning("%s returned %s: %s", self.name, resp.status_code, detail)
            raise LLMUnavailable(f"{self.name} {resp.status_code}: {detail}")

        try:
            text, tokens_in, tokens_out = self._parse(resp.json())
        except (ValueError, KeyError, TypeError, IndexError) as e:
            logger.warning("%s sent a shape we do not recognise: %s", self.name, e)
            raise LLMUnavailable(f"{self.name} returned an unreadable response") from e

        if not text.strip():
            raise LLMUnavailable(
                f"{self.name} returned an empty response "
                f"(tokens_out={tokens_out}, latency={latency_ms}ms)"
            )

        return LLMResponse(
            text=text.strip(),
            model=self._model_name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
        )


def _first_line(body: str, limit: int = 300) -> str:
    """Enough of an error to act on, not a screen of JSON in a log line."""
    flat = " ".join((body or "").split())
    return flat[:limit] or "no detail"


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
        return {
            "model": self._model_name,
            "max_tokens": max_output_tokens,
            "temperature": temperature,
            # A first-class field here, not a pseudo-message.
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }

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
