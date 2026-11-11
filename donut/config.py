import json
import os
from pathlib import Path
import re
import tempfile
from .providers import validate


# What selects a provider account. A named profile starts without them, so it cannot inherit the shell's account:
# set them in that profile's Claude login folder (settings.json "env") instead.
PROVIDER_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
)


def profile_name():
    """The Donut profile in use: "default", or the name given with --profile (or DONUT_PROFILE)."""
    return os.environ.get("DONUT_PROFILE") or "default"


def use_profile(name):
    """Select the profile for this process and the windows and workers it starts.

    Each profile has its own configuration, conversations, logs and tickets. A named profile also drops the
    shell's provider variables, so its accounts come only from its own configuration.
    """
    if name in (None, "", "default"):
        os.environ.pop("DONUT_PROFILE", None)
        return
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("A profile name may contain letters, numbers, underscores and hyphens.")
    os.environ["DONUT_PROFILE"] = name
    for key in PROVIDER_ENV:
        os.environ.pop(key, None)


def config_path():
    folder = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "donut"
    name = profile_name()
    return folder / "config.json" if name == "default" else folder / "profiles" / f"{name}.json"


def state_dir():
    """Where this profile's conversations and logs are kept."""
    folder = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "donut"
    name = profile_name()
    return folder if name == "default" else folder / "profiles" / name


def check_project(project):
    """Refuse a project that does not allow this profile.

    .donut/config.json in the project, {"profiles": ["default"]}, lists the profiles that may open it. It guards
    against opening a project with the wrong account by mistake; it does not stop a model that edits the file.
    """
    path = Path(project) / ".donut" / "config.json"
    if not path.exists():
        return
    try:
        allowed = json.loads(path.read_text(encoding="utf-8")).get("profiles")
    except (OSError, ValueError, AttributeError):
        allowed = False
    if allowed is None:
        return
    if not isinstance(allowed, list) or not all(isinstance(name, str) for name in allowed):
        raise ValueError(f'{path} must hold {{"profiles": ["NAME", ...]}}. Correct it before Donut opens this project.')
    if profile_name() not in allowed:
        raise ValueError(
            f"This project allows only the {' and '.join(allowed) or 'no'} profile, not {profile_name()} "
            f'({path}). Open another folder with -p PATH, or add "{profile_name()}" to that file\'s list.'
        )


def load():
    path = config_path()
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"aliases": {}}
    for profile in config.get("aliases", {}).values():
        model = profile.get("model")
        # Claude IDs spell versions with a hyphen: "claude-opus-5.5" is a common slip for "claude-opus-5-5".
        if profile.get("engine") == "claude" and isinstance(model, str) and model.startswith("claude-"):
            profile["model"] = re.sub(r"(\d)\.(\d)", r"\1-\2", model)
        own_login(profile)
    return config


def own_login(profile):
    """Refuse a named profile's Claude alias that has no login folder: it would use ~/.claude, the default's."""
    if profile_name() != "default" and profile.get("engine") == "claude" and not profile.get("claude_config_dir"):
        raise ValueError(
            f'Every Claude alias in the {profile_name()} profile needs "claude_config_dir" in {config_path()}, '
            "so it does not share the default profile's login, skills and history."
        )


def save(config):
    for alias, profile in config["aliases"].items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", alias):
            raise ValueError("Aliases may contain letters, numbers, underscores and hyphens.")
        validate(profile)
        own_login(profile)
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
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
