"""What an instance may choose, and what each choice costs.

One table, read by three things that would otherwise disagree: the dashboard
drawing the dropdowns, `get_client()` building the provider, and the savings
report working out what the tokens cost. Before this the prices lived alone
in settings defaults, so switching model left the report quietly billing
Gemini rates for somebody else's tokens.

Model names go stale — every provider ships new ones and retires old ones
faster than this file will be edited. So the listed models are a convenience,
not a constraint: the dashboard offers them and also accepts anything typed
in, and an unknown name is passed to the provider untouched. The provider is
the authority on whether its own model exists, and its error message says so
better than a stale allowlist here would.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Model:
    """One model, and what a million tokens of it costs in USD.

    Prices are list prices at the time of writing and are seeded into the
    savings report when the model is picked. They are editable afterwards,
    and the report prints them, because a price nobody can see is a number
    that silently goes wrong.
    """

    name: str
    label: str
    price_in: float
    price_out: float


@dataclass(frozen=True, slots=True)
class Provider:
    id: str
    label: str
    #: The .env variable checked when no key is stored in the database. Keeps
    #: every instance that configured AI before this existed working untouched.
    env_var: str
    #: Where to get a key, shown next to the empty field. An operator who has
    #: to search for this is an operator who does not turn the feature on.
    console_url: str
    models: tuple[Model, ...]
    #: Ollama runs locally and authenticates with nothing.
    needs_key: bool = True
    #: Free-text host, for providers that are somewhere rather than something.
    host_hint: str = ""


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="gemini",
        label="Google Gemini",
        env_var="GEMINI_API_KEY",
        console_url="https://aistudio.google.com/apikey",
        models=(
            Model("gemini-2.5-flash", "Gemini 2.5 Flash", 0.30, 2.50),
            Model("gemini-2.5-pro", "Gemini 2.5 Pro", 1.25, 10.00),
            Model("gemini-2.0-flash", "Gemini 2.0 Flash", 0.10, 0.40),
        ),
    ),
    Provider(
        id="anthropic",
        label="Anthropic Claude",
        env_var="ANTHROPIC_API_KEY",
        console_url="https://console.anthropic.com/settings/keys",
        models=(
            Model("claude-haiku-4-5-20251001", "Claude Haiku 4.5", 1.00, 5.00),
            # Claude Platform model overview, verified 2026-10-01. API IDs use hyphens.
            Model("claude-sonnet-5-5", "Claude Sonnet 5.5", 2.00, 10.00),
            Model("claude-opus-5-5", "Claude Opus 5.5", 4.00, 20.00),
            Model("claude-sonnet-5", "Claude Sonnet 5", 3.00, 15.00),
            Model("claude-opus-5", "Claude Opus 5", 5.00, 25.00),
        ),
    ),
    Provider(
        id="openrouter",
        label="OpenRouter",
        env_var="OPENROUTER_API_KEY",
        console_url="https://openrouter.ai/settings/keys",
        models=(
            # OpenRouter's public model catalog, verified 2026-10-01.
            Model("anthropic/claude-sonnet-5.5", "Claude Sonnet 5.5", 2.00, 10.00),
            Model("anthropic/claude-opus-5.5", "Claude Opus 5.5", 4.00, 20.00),
            Model("anthropic/claude-opus-5", "Claude Opus 5", 5.00, 25.00),
            Model("anthropic/claude-haiku-4.5", "Claude Haiku 4.5", 1.00, 5.00),
        ),
    ),
    Provider(
        id="openai",
        label="OpenAI",
        env_var="OPENAI_API_KEY",
        console_url="https://platform.openai.com/api-keys",
        models=(
            Model("gpt-4o-mini", "GPT-4o mini", 0.15, 0.60),
            Model("gpt-4o", "GPT-4o", 2.50, 10.00),
        ),
    ),
    Provider(
        id="ollama",
        label="Ollama (self-hosted)",
        env_var="OLLAMA_HOST",
        console_url="https://ollama.com/download",
        needs_key=False,
        host_hint="http://localhost:11434",
        models=(
            Model("llama3.1:8b", "Llama 3.1 8B", 0.0, 0.0),
            Model("qwen2.5:14b", "Qwen 2.5 14B", 0.0, 0.0),
            Model("mistral-small", "Mistral Small", 0.0, 0.0),
        ),
    ),
)

#: The one an instance gets before anybody chooses, and the one whose prices
#: the savings report was seeded with.
DEFAULT_PROVIDER = "gemini"
DEFAULT_MODEL = "gemini-2.5-flash"

_BY_ID = {p.id: p for p in PROVIDERS}


def provider(provider_id: str) -> Provider | None:
    return _BY_ID.get((provider_id or "").strip().lower())


def is_known(provider_id: str) -> bool:
    return provider(provider_id) is not None


def model(provider_id: str, model_name: str) -> Model | None:
    """The catalogue entry, or None for a name typed in by hand.

    None is expected rather than exceptional. A model released last week is
    not in this file and should still work.
    """
    p = provider(provider_id)
    if p is None:
        return None
    return next((m for m in p.models if m.name == model_name), None)


def default_model_for(provider_id: str) -> str:
    p = provider(provider_id)
    return p.models[0].name if p and p.models else ""


def prices_for(provider_id: str, model_name: str) -> tuple[float, float] | None:
    """(input, output) per million tokens, or None if this model is unlisted.

    None means "leave whatever the operator has set alone". Guessing a price
    for a model we have never heard of would put a plausible, wrong number on
    a report somebody might show their finance team.
    """
    m = model(provider_id, model_name)
    return (m.price_in, m.price_out) if m else None
