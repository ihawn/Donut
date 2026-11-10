"""/effort: how hard the selected model thinks, in each provider's own terms and levels.

Claude calls it effort (low, medium, high, xhigh, max) and says per model which levels it takes; Codex calls it
reasoning effort and lists each model's levels with its own descriptions. Qwen Code has no such setting.
"""

# The Claude Agent SDK's own descriptions of each level.
CLAUDE_LEVELS = {
    "low": "Minimal thinking, fastest responses",
    "medium": "Moderate thinking",
    "high": "Deep reasoning (default)",
    "xhigh": "Extended reasoning depth",
    "max": "Maximum effort",
}


def _matches(entry, model):
    model = (model or "").lower()
    names = {str(entry.get(key, "")).lower() for key in ("value", "id", "model", "displayName")}
    return bool(model) and (model in names or any(model.startswith(name) for name in names if name))


def claude_options(info, model):
    """("Effort", [(level, description)], default) for a Claude model, from Claude Code's own model list.

    An empty list means the model takes no effort setting. A model Claude Code does not list gets every level.
    """
    entries = [entry for entry in (info or {}).get("models", []) if isinstance(entry, dict)]
    entry = next((e for e in entries if _matches(e, model)), None)
    if entry is None and not model:
        entry = next((e for e in entries if str(e.get("value")) == "default"), None)
    if entry is not None:
        if entry.get("supportsEffort") is False:
            return "Effort", [], None  # Claude Code says this model takes no effort setting.
        levels = entry.get("supportedEffortLevels") or list(CLAUDE_LEVELS)
    else:
        levels = list(CLAUDE_LEVELS)
    return "Effort", [(level, CLAUDE_LEVELS.get(level, "")) for level in levels], "high"


def codex_options(models, model):
    """("Reasoning effort", [(level, description)], default) for a Codex model, from Codex's model list."""
    entries = [entry for entry in models or [] if isinstance(entry, dict)]
    entry = next((e for e in entries if _matches(e, model)), None) or next(
        (e for e in entries if e.get("isDefault")), None
    )
    if entry is None:
        return "Reasoning effort", [], None
    levels = []
    for option in entry.get("supportedReasoningEfforts") or []:
        if isinstance(option, dict) and option.get("reasoningEffort"):
            levels.append((option["reasoningEffort"], option.get("description", "")))
        elif isinstance(option, str):
            levels.append((option, ""))
    return "Reasoning effort", levels, entry.get("defaultReasoningEffort")
