"""AI helpers (reply suggestions, classification, etc).

Provider-agnostic: handlers depend on `LLMClient` Protocol, concrete client is
constructed in `get_client()`. Today — Gemini. Tomorrow — swap to Groq/Ollama
by writing a new provider and changing `get_client()`.
"""

from src.ai.client import LLMClient, LLMResponse, LLMUnavailable, get_client

__all__ = ["LLMClient", "LLMResponse", "LLMUnavailable", "get_client"]
