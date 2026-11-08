"""Optional git safety net around each ticket: record what changed, and commit it on request."""

import subprocess


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


def changed_files(project):
    """Paths with uncommitted changes (staged, unstaged or untracked), or None outside a git repository."""
    if not is_repository(project):
        return None
    result = _git(project, "status", "--porcelain", "--untracked-files=all")
    if result.returncode:
        return None
    return sorted({line[3:].split(" -> ")[-1] for line in result.stdout.splitlines() if line.strip()})


def commit(project, message):
    """Commit every working-tree change as one checkpoint. Returns the short hash, or None when nothing changed."""
    if not is_repository(project):
        raise ValueError(f"{project} is not a git repository; drop --checkpoint or run git init")
    if not changed_files(project):
        return None
    _git(project, "add", "--all", check=True)
    identity = []
    if not _git(project, "config", "user.email").stdout.strip():
        identity = ["-c", "user.name=Donut", "-c", "user.email=donut@localhost"]
    result = _git(project, *identity, "commit", "--quiet", "--no-verify", "-m", message)
    if result.returncode:
        raise ValueError("git commit failed: " + (result.stderr or result.stdout).strip()[-500:])
    return _git(project, "rev-parse", "--short", "HEAD").stdout.strip()
