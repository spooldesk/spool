"""AI helpers (reply suggestions, classification, etc).

Provider-agnostic: handlers depend on `LLMClient` Protocol, concrete client is
constructed in `get_client()`, from whichever provider the instance has
chosen in its dashboard — see src/ai/registry.py.
"""

from src.ai.client import LLMClient, LLMResponse, LLMUnavailable, get_client

__all__ = ["LLMClient", "LLMResponse", "LLMUnavailable", "get_client"]
