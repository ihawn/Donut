import json
import os
from pathlib import Path
import re
import tempfile
from .providers import validate


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "donut/config.json"


def load():
    path = config_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"aliases": {}}


def save(config):
    for alias, profile in config["aliases"].items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", alias):
            raise ValueError("Aliases may contain letters, numbers, underscores and hyphens.")
        validate(profile)
        allowed = {
            "engine",
            "model",
            "base_url",
            "key_env",
            "auto_approve",
            "claude_config_dir",
            "unload_others",
            "permission_mode",
            "effort",
        }
        if set(profile) - allowed:
            raise ValueError("Unexpected profile fields; credentials cannot be saved.")
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(config, file, indent=2)
            file.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


ROLE_DEFAULT_KEYS = {"plan": "planner", "work": "default_model"}


def role_choice(config, role):
    """(alias, profile) for "plan", "work", "review" or "solo", including a model picked with /planner and the like.

    A choice saved under config["roles"] wins. Otherwise plan and work use their default alias, and review
    follows the planner unless config["reviewer"] is "off" or names an alias. None means nothing is configured
    (or review is off).
    """
    aliases = config.get("aliases", {})
    saved = (config.get("roles") or {}).get(role)
    if isinstance(saved, dict) and saved.get("alias") in aliases:
        profile = dict(aliases[saved["alias"]])
        if saved.get("model"):
            profile["model"] = saved["model"]
        if saved.get("effort"):
            profile["effort"] = saved["effort"]
        return saved["alias"], profile
    if role == "review":
        reviewer = config.get("reviewer")
        if reviewer == "off":
            return None
        if reviewer in aliases:
            return reviewer, dict(aliases[reviewer])
        return role_choice(config, "plan")
    if role not in ROLE_DEFAULT_KEYS:
        return None  # The soloist has no default; /solo asks the first time.
    alias = config.get(ROLE_DEFAULT_KEYS[role])
    if alias in aliases:
        return alias, dict(aliases[alias])
    return None


def remember_role(role, alias, model, effort=None):
    """Save which alias, model and effort a role uses, so /model, /review and /effort choices stick."""
    config = load()
    config.setdefault("roles", {})[role] = {"alias": alias, "model": model or ""}
    if effort:
        config["roles"][role]["effort"] = effort
    if role == "review":
        config.pop("reviewer", None)
    save(config)


def forget_role(role, reviewer=None):
    """Drop a saved role choice. For review, reviewer="off" turns review off; None makes it follow the planner."""
    config = load()
    (config.get("roles") or {}).pop(role, None)
    if role == "review":
        if reviewer == "off":
            config["reviewer"] = "off"
        else:
            config.pop("reviewer", None)
    save(config)
