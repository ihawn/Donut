"""Optional git safety net around each ticket: record what changed, and commit it on request."""

import os
import subprocess
from pathlib import Path


def _git(project, *args, check=False):
    return subprocess.run(
        ["git", *args], cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace", check=check
    )


def is_repository(project):
    try:
        result = _git(project, "rev-parse", "--is-inside-work-tree")
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


# Folders never searched for nested repositories: dependencies, build output and Donut's own tickets.
SKIP = {"node_modules", "venv", "env", "__pycache__", "dist", "build", "target", "swim_lanes"}
DEPTH = 3


def repositories(project):
    """The git repositories a run in this project can change: [(root, prefix)].

    The repository holding the project (prefix "") comes first, then each repository nested up to three folders
    below it (prefix "services/api/"), so one project folder can hold several repositories.
    """
    project = Path(project)
    found = []
    if is_repository(project):
        found.append((project, ""))
    for folder, dirs, _ in os.walk(project):
        here = Path(folder)
        depth = len(here.relative_to(project).parts)
        if depth and (here / ".git").exists():
            found.append((here, here.relative_to(project).as_posix() + "/"))
            dirs[:] = []
            continue
        dirs[:] = [] if depth >= DEPTH else [d for d in dirs if not d.startswith(".") and d not in SKIP]
    return found


def _repository_changes(root, nested):
    """Changed paths in one repository, relative to root, leaving out the nested repositories themselves."""
    prefix = _git(root, "rev-parse", "--show-prefix").stdout.strip()
    result = _git(root, "status", "--porcelain", "--untracked-files=all", "--", ".")
    if result.returncode:
        return []
    paths = set()
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].split(" -> ")[-1].strip('"')
        if prefix and not path.startswith(prefix):
            continue
        path = path[len(prefix) :]
        if any(path == n.rstrip("/") or path.startswith(n) for n in nested):
            continue
        paths.add(path)
    return sorted(paths)


def changed_files(project):
    """Paths with uncommitted changes (staged, unstaged or untracked), relative to the project, across every
    repository in it; None when the project holds no git repository."""
    repos = repositories(project)
    if not repos:
        return None
    nested = [prefix for _, prefix in repos if prefix]
    files = set()
    for root, prefix in repos:
        inner = [n[len(prefix) :] for n in nested if n != prefix and n.startswith(prefix)]
        files.update(prefix + path for path in _repository_changes(root, inner))
    return sorted(files)


def heads(project):
    """Each repository's current commit, {root: sha}, taken before a ticket so its own commits can be told apart."""
    found = {}
    for root, _ in repositories(project):
        result = _git(root, "rev-parse", "--verify", "--quiet", "HEAD")
        if result.returncode == 0:
            found[root] = result.stdout.strip()
    return found


def committed_files(project, before):
    """Project-relative paths in commits made since before (from heads()), for a worker that committed its work."""
    files = set()
    for root, prefix in repositories(project):
        old = (before or {}).get(root)
        if old:
            names = _git(root, "diff", "--name-only", old, "HEAD").stdout.splitlines()
            files.update(prefix + name for name in names if name)
    return sorted(files)


def group(project, files):
    """Split project-relative paths by repository: [(root, prefix, [paths relative to root])], deepest match wins."""
    repos = sorted(repositories(project), key=lambda repo: len(repo[1]), reverse=True)
    groups = {}
    for path in files:
        for root, prefix in repos:
            if path.startswith(prefix):
                groups.setdefault((root, prefix), []).append(path[len(prefix) :])
                break
    return [(root, prefix, sorted(paths)) for (root, prefix), paths in sorted(groups.items(), key=lambda g: g[0][1])]


def commit(project, message):
    """Commit every working-tree change as one checkpoint in each repository that changed.

    Returns the short hash (several joined by ", " when more than one repository changed), or None when nothing
    changed.
    """
    if not repositories(project):
        raise ValueError(f"{project} is not a git repository; drop --checkpoint or run git init")
    hashes = []
    for root, _, paths in group(project, changed_files(project) or []):
        _git(root, "add", "--all", "--", *paths, check=True)
        if not _git(root, "diff", "--cached", "--quiet").returncode:
            continue
        identity = []
        if not _git(root, "config", "user.email").stdout.strip():
            identity = ["-c", "user.name=Donut", "-c", "user.email=donut@localhost"]
        result = _git(root, *identity, "commit", "--quiet", "--no-verify", "-m", message)
        if result.returncode:
            raise ValueError("git commit failed: " + (result.stderr or result.stdout).strip()[-500:])
        hashes.append(_git(root, "rev-parse", "--short", "HEAD").stdout.strip())
    return ", ".join(hashes) or None


def diff(project, files, limit=30000, before=None):
    """A unified diff of the given paths against HEAD, for a reviewer, one section per repository.

    before (from heads()) is each repository's commit when the ticket started: the diff is taken against it, so
    work the worker committed is still shown. New untracked files are listed by name.
    """
    if not files:
        return ""
    groups = group(project, files)
    text = ""
    for root, prefix, paths in groups:
        if len(groups) > 1 or prefix:
            text += f"\n# Repository {prefix or './'}\n"
        tracked = _git(root, "ls-files", "--", *paths).stdout.split()
        has_head = _git(root, "rev-parse", "--verify", "HEAD").returncode == 0
        base = (before or {}).get(root, "HEAD")
        text += _git(root, "diff", base, "--", *tracked).stdout if tracked and has_head else ""
        new = [prefix + path for path in paths if path not in tracked]
        if new:
            text += "\nNew files (read them directly):\n" + "\n".join("  " + path for path in new) + "\n"
    if len(text) > limit:
        text = text[:limit] + f"\n… diff truncated at {limit} characters; read the files for the rest.\n"
    return text
