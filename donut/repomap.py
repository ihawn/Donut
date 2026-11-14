"""A compact map of the project for a worker: the symbols near its ticket's files, then the other folders."""

import os
import re
import subprocess
from pathlib import Path

from .checkpoints import SKIP

LIMIT = 4000  # Characters, about a thousand tokens: small enough for a local model's context.
MAX_FILES = 20000
MAX_BYTES = 200_000
# shortcut: one regex for the common declaration keywords, no parser; C-style methods without a keyword are
# not listed. Upgrade to tree-sitter if workers miss symbols.
_SYMBOL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:pub(?:\([a-z]+\))?\s+)?(?:public\s+|private\s+|internal\s+)?"
    r"(?:static\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(class|def|function|func|fn|struct|enum|interface|trait|type|module)\s+([A-Za-z_][A-Za-z0-9_]*)"
)


def files(project):
    """Project-relative paths: what git tracks or would track, else a walk that skips dependency folders."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=project,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return sorted(result.stdout.splitlines())[:MAX_FILES]
    except OSError:
        pass
    found = []
    for folder, dirs, names in os.walk(project):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP)
        here = Path(folder).relative_to(project)
        found += [(here / name).as_posix() for name in sorted(names)]
        if len(found) >= MAX_FILES:
            break
    return found[:MAX_FILES]


def symbols(path):
    """The declarations in one source file, in order: ["class Ticket", "def pick"]."""
    try:
        if path.stat().st_size > MAX_BYTES:
            return []
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return list(dict.fromkeys(f"{m[1]} {m[2]}" for m in map(_SYMBOL.match, text.splitlines()) if m))


def build(project, focus=(), skip=(), limit=LIMIT):
    """The map as text, at most limit characters; "" for an empty project.

    focus is the ticket's paths. Those files come first with their symbols, then the other files of the same
    folders with theirs, then every other folder with its file count. skip is path prefixes to leave out (the
    tickets folder).
    """
    project = Path(project)
    paths = [p for p in files(project) if not any(p.startswith(s) for s in skip)]
    focus = [f.strip("/") for f in focus]
    wanted = [p for p in paths if any(p == f or p.startswith(f + "/") for f in focus)]
    folders = {os.path.dirname(p) for p in wanted}
    near = [p for p in paths if p not in wanted and os.path.dirname(p) in folders]
    lines, used = [], 0
    for path in wanted + near:
        found = symbols(project / path)
        line = path + (": " + ", ".join(found) if found else "")
        if used + len(line) > limit * 3 // 4:  # Keep a quarter for the folder overview.
            line = path
            if used + len(line) > limit * 3 // 4:
                break
        lines.append(line)
        used += len(line) + 1
    counts = {}
    for path in paths:
        folder = os.path.dirname(path)
        if folder not in folders:
            counts[folder] = counts.get(folder, 0) + 1
    for folder, count in sorted(counts.items()):
        line = f"{folder or '.'}/ ({count} file{'' if count == 1 else 's'})"
        if used + len(line) > limit:
            lines.append("… more folders not listed")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
