"""Slash commands from the plugins and skills each provider has installed, found on disk.

Donut lists them in its / menu before any provider session is open, and rewrites them into the provider's own
syntax: Claude Code takes /name as typed, and Codex invokes a plugin skill as $plugin:skill.
"""

import json
import os
from pathlib import Path


def claude_dir(profile):
    """The Claude configuration folder this profile logs in with, as Claude Code resolves it."""
    if profile.get("claude_config_dir"):
        return Path(profile["claude_config_dir"]).expanduser()
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_dir():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def frontmatter(path):
    """name and description from a SKILL.md or command file's YAML header; folded (>) values are joined."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    fields, key = {}, None
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        if line[:1].isspace() and key:
            fields[key] = (fields[key] + " " + line.strip()).strip()
        elif ":" in line:
            key, value = line.split(":", 1)
            key, value = key.strip(), value.strip()
            fields[key] = "" if value in (">", "|", ">-", "|-") else value.strip("'\"")
    return fields


def _skills(folder):
    """(name, description) for every skills/<name>/SKILL.md under folder."""
    found = []
    for skill in sorted(Path(folder).glob("*/SKILL.md")):
        meta = frontmatter(skill)
        found.append((meta.get("name") or skill.parent.name, meta.get("description", "")))
    return found


def _command_files(folder):
    return [(path.stem, frontmatter(path).get("description", "")) for path in sorted(Path(folder).glob("*.md"))]


def claude_plugins(profile, project=None):
    """(plugin name, install folder) for each plugin installed and enabled in this Claude login."""
    base = claude_dir(profile)
    enabled = {}
    sources = [base / "settings.json"]
    if project:
        sources += [Path(project) / ".claude" / "settings.json", Path(project) / ".claude" / "settings.local.json"]
    for source in sources:
        for key, value in (_read_json(source).get("enabledPlugins") or {}).items():
            enabled[key] = bool(value)
    installed = _read_json(base / "plugins" / "installed_plugins.json")
    entries = installed.get("plugins", installed) if isinstance(installed, dict) else {}
    plugins = []
    for key, records in entries.items() if isinstance(entries, dict) else ():
        if not enabled.get(key):
            continue
        records = records if isinstance(records, list) else [records]
        for record in records:
            path = record.get("installPath") if isinstance(record, dict) else None
            if path and Path(path).is_dir():
                plugins.append((key.split("@")[0], Path(path)))
                break
    return plugins


def codex_plugins():
    """(plugin name, install folder) for each plugin in the Codex plugin cache."""
    root = codex_dir() / "plugins"
    plugins = {}
    for folder, dirs, _ in os.walk(root):
        if len(Path(folder).relative_to(root).parts) > 6:
            dirs[:] = []
            continue
        manifest = Path(folder) / ".codex-plugin" / "plugin.json"
        if manifest.is_file():
            name = _read_json(manifest).get("name") or Path(folder).name
            plugins.setdefault(name, Path(folder))
            dirs[:] = []
    return sorted(plugins.items())


def commands(profile, project=None):
    """The provider's own slash commands: [{"name": "/x", "description": ..., "send": "how to invoke it"}]."""
    engine = profile.get("engine")
    found = []
    if engine == "claude":
        base = claude_dir(profile)
        folders = [base]
        if project:
            folders.append(Path(project) / ".claude")
        for _, path in claude_plugins(profile, project):
            folders.append(path)
        for folder in folders:
            for name, description in _skills(folder / "skills") + _command_files(folder / "commands"):
                found.append({"name": "/" + name, "description": description, "send": "/" + name})
    elif engine == "codex":
        for plugin, path in codex_plugins():
            manifest = _read_json(path / ".codex-plugin" / "plugin.json")
            for name, description in _skills(path / (manifest.get("skills") or "skills")):
                found.append({"name": "/" + name, "description": description, "send": f"${plugin}:{name}"})
        for folder in [codex_dir() / "skills"] + ([Path(project) / ".agents" / "skills"] if project else []):
            for name, description in _skills(folder):
                found.append({"name": "/" + name, "description": description, "send": "$" + name})
    unique = {}
    for command in found:
        unique.setdefault(command["name"], command)
    return list(unique.values())


def installed(profile, plugin, project=None):
    if profile.get("engine") == "claude":
        return any(name == plugin for name, _ in claude_plugins(profile, project))
    if profile.get("engine") == "codex":
        return any(name == plugin for name, _ in codex_plugins())
    return False


def translate(text, available):
    """Rewrite '/name args' into the provider's syntax when name is one of its commands; otherwise unchanged."""
    name, _, rest = text.partition(" ")
    for command in available:
        if command["name"] == name:
            return (command["send"] + " " + rest).strip() if rest else command["send"]
    return text
