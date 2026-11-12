"""One spend figure for everything Donut runs: conversations, auto's rating, and the workers of a /work run.

Each provider is counted with the most exact source it gives:

- Claude Code reports a cost for every turn, at the list price of the platform the turn ran on (Anthropic's API,
  Amazon Bedrock, Google Vertex), with its cache prices. Donut uses that figure as it is.
- Codex and OpenAI-compatible endpoints report tokens only. Donut prices them with PRICES, or with "prices" in the
  configuration: {"prices": {"model-name": {"input": 2, "cached_input": 0.1, "output": 10}}}, in dollars for a
  million tokens. A model with no price counts as zero and is listed as not priced, never guessed.
- A local model costs nothing.

Work that a subscription covers (a Claude plan login, Codex signed in with ChatGPT) has no price for each token.
It counts at the list price, so the figure shows what the work is worth; {"spend": {"plans": "free"}} in the
configuration counts it as zero, so the figure shows only what is billed.
"""

import json
import os
from pathlib import Path

# Dollars for a million tokens: (input, cached input, output). The longest name found in the model ID wins.
PRICES = {
    "gpt-6-astra": (10, 1, 50),
    "gpt-6.1-sol": (2, 0.1, 10),
    "gpt-6.1-terra": (1, 0.05, 5),
}

_INPUT = ("input_tokens", "inputTokens", "prompt_tokens")
_CACHED = ("cached_input_tokens", "cachedInputTokens", "cache_read_input_tokens")
_OUTPUT = ("output_tokens", "outputTokens", "completion_tokens")


def rates(model, config=None):
    """(input, cached input, output) prices for a model, or None when none is known."""
    table = dict(PRICES)
    for name, price in ((config or {}).get("prices") or {}).items():
        if isinstance(price, dict):
            price = (price.get("input", 0), price.get("cached_input", price.get("input", 0)), price.get("output", 0))
        if isinstance(price, (list, tuple)) and len(price) == 3:
            table[str(name).lower()] = tuple(float(value) for value in price)
    found = [name for name in table if name in (model or "").lower()]
    return table[max(found, key=len)] if found else None


def cost(model, usage, config=None):
    """Dollars for a token report, or None when the model has no price.

    The input count includes the cached tokens (OpenAI's and Codex's reports do), which have their own price.
    """
    price = rates(model, config)
    if price is None or not isinstance(usage, dict):
        return None

    def count(keys):
        return next((int(usage[key] or 0) for key in keys if key in usage), 0)

    cached = count(_CACHED)
    fresh = max(0, count(_INPUT) - cached)
    return (fresh * price[0] + cached * price[1] + count(_OUTPUT) * price[2]) / 1_000_000


def codex_plan():
    """Whether Codex is signed in with a ChatGPT plan, not an API key."""
    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("auth_mode") == "chatgpt"
    except (OSError, ValueError, AttributeError):
        return False


def platform(profile, account=None):
    """How a model's work is paid for: "local", "plan", "api", "bedrock" or "vertex".

    account is what Claude Code says about the login: its apiKeySource ("none" for a plan login), or the provider
    it names in its usage report ("bedrock", "vertex").
    """
    engine = (profile or {}).get("engine")
    if engine == "ollama":
        return "local"
    if engine == "codex":
        return "plan" if codex_plan() else "api"
    if engine == "claude":
        if account in ("bedrock", "vertex"):
            return account
        return "plan" if account == "none" else "api"
    return "api"


def counted(where, config=None):
    """Whether work on this platform adds to the figure."""
    if where == "local":
        return False
    return not (where == "plan" and ((config or {}).get("spend") or {}).get("plans") == "free")


def claude_account(init, usage=None):
    """What says how a Claude turn is paid for, from the stream's init event and the result's modelUsage."""
    providers = {str(entry.get("provider", "")).lower() for entry in (usage or {}).values() if isinstance(entry, dict)}
    for name in ("bedrock", "vertex"):
        if any(name in provider for provider in providers):
            return name
    return (init or {}).get("apiKeySource")
