"""Tickets side by side: each worker gets its own git worktree, and its changes go back to the project as a patch."""

import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from .checkpoints import _git, repositories
from .tickets import LANES
from .ui import Progress

# What Donut installs that a project may not commit. A worktree only holds committed files, so these are copied.
DONUT_FILES = (".donut/skills", ".qwen/skills", "skills", "QWEN.md", "CLAUDE.md", "AGENTS.md")


def check(project):
    """Refuse a project that worktrees cannot copy: it must be the top folder of exactly one git repository."""
    repos = repositories(project)
    top = len(repos) == 1 and not repos[0][1] and not _git(project, "rev-parse", "--show-prefix").stdout.strip()
    if not top:
        raise ValueError(
            f"{project} must be the top folder of one git repository to run tickets in parallel; "
            "drop --parallel, or run it from the repository's top folder"
        )


def base(project):
    """The commit worktrees start from, or None in a repository with no commit yet."""
    result = _git(project, "rev-parse", "--verify", "--quiet", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def overlap(first, second):
    """Whether two tickets' file lists share a path, or one lists a folder that holds a path of the other."""
    return any(
        a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/") for a in first for b in second
    )


def add(project, lanes, folder, commit):
    """A detached worktree of commit in folder, with the session's tickets and Donut's uncommitted files copied in.

    Returns (worktree, copied): copied are the project-relative paths that came from the project and not from
    git, which merge() leaves out of the patch.
    """
    project, folder = Path(project), Path(folder)
    folder.parent.mkdir(parents=True, exist_ok=True)
    result = _git(project, "worktree", "add", "--detach", str(folder), commit)
    if result.returncode:
        raise ValueError("git worktree add failed: " + (result.stderr or result.stdout).strip()[-500:])
    tickets = Path(lanes).relative_to(project)
    shutil.rmtree(folder / tickets, ignore_errors=True)  # The project's copy is newer than the committed one.
    shutil.copytree(lanes, folder / tickets)
    copied = [tickets.as_posix()]
    for name in DONUT_FILES:
        source, target = project / name, folder / name
        if source.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
            copied.append(name)
    return folder, copied


def merge(project, worktree, commit, copied):
    """Apply what the worker changed in its worktree to the project's working tree. Returns whether it changed
    anything; raises ValueError, with the project untouched, when the patch does not apply."""
    paths = [".", *(f":(exclude){path}" for path in copied)]
    _git(worktree, "add", "--all", "--", *paths, check=True)
    patch = subprocess.run(
        ["git", "diff", "--cached", "--binary", commit, "--", *paths], cwd=worktree, capture_output=True, check=True
    ).stdout
    if not patch.strip():
        return False
    result = subprocess.run(
        ["git", "apply", "--binary", "--whitespace=nowarn", "-"], cwd=project, input=patch, capture_output=True
    )
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip()[-500:] or "git apply failed")
    return True


def take_ticket(lanes, worked, name):
    """Move the project's ticket to the lane its worker left it in, with the worker's notes."""
    found = [Path(worked) / lane / name for lane in LANES if (Path(worked) / lane / name).exists()]
    if len(found) != 1:
        return
    for lane in LANES:
        (Path(lanes) / lane / name).unlink(missing_ok=True)
    (Path(lanes) / found[0].parent.name).mkdir(exist_ok=True)
    shutil.copy2(found[0], Path(lanes) / found[0].parent.name / name)


def remove(project, worktree):
    _git(project, "worktree", "remove", "--force", str(worktree))
    shutil.rmtree(worktree, ignore_errors=True)
    _git(project, "worktree", "prune")


class Output:
    """sys.stdout during a parallel run: a worker thread prints to its own file, every other thread to the terminal."""

    def __init__(self, real):
        self.real, self.local = real, threading.local()

    def _target(self):
        return getattr(self.local, "file", None) or self.real

    def write(self, text):
        return self._target().write(text)

    def flush(self):
        self._target().flush()

    def isatty(self):
        return self._target().isatty()

    def __getattr__(self, name):
        return getattr(self.real, name)


def run(jobs, stop):
    """Run every job at once. jobs is [(name, console file, call)]; returns each call's result, in order.

    What a job prints goes to its console file, so the tickets' output does not mix. stop() ends the running
    agents on Ctrl-C.
    """
    router = Output(sys.stdout)
    sys.stdout = router

    def one(console, call):
        with console.open("w", encoding="utf-8") as file:
            router.local.file = file
            try:
                return call()
            finally:
                router.local.file = None

    progress = Progress()
    try:
        with ThreadPoolExecutor(len(jobs)) as pool:
            futures = {pool.submit(one, console, call): name for name, console, call in jobs}
            pending = set(futures)
            try:
                while pending:
                    finished, pending = wait(pending, timeout=0.2)
                    progress.clear()
                    for future in finished:
                        print(f"[parallel] {futures[future]}: worker finished", flush=True)
                    if pending:
                        progress.tick(f"{len(pending)} in parallel")
            except KeyboardInterrupt:
                stop()
                raise
            finally:
                progress.clear()
        return [future.result() for future in futures]
    finally:
        sys.stdout = router.real
