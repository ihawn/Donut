"""Where a session's tickets live: .donut/<session>/{to_do,in_progress,on_hold,blocked,done}.

Each Donut terminal session gets its own folder, so two lines of work in one project never share a queue. Older
projects keep working from swim_lanes/ until they start a session.
"""

import re
import time
from pathlib import Path

ROOT = ".donut"
LEGACY = "swim_lanes"
LANES = ("to_do", "in_progress", "on_hold", "blocked", "done")
RESERVED = {"skills"}  # .donut/skills holds the worker skill.


def lanes_dir(project, session=None):
    """The folder holding the five lanes: .donut/<session>/, or swim_lanes/ when there is no session."""
    return Path(project) / ROOT / session if session else Path(project) / LEGACY


def relative(project, lanes):
    """The lanes folder as the agents see it, relative to the project with a trailing slash."""
    return Path(lanes).relative_to(project).as_posix() + "/"


def valid(name):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name or "")) and name not in RESERVED


def new_name(project, now=None, taken=()):
    """A fresh session name from the time it started, such as 20261109-1712 (or 20261109-1712-2).

    taken holds names already in use whose folders may not exist yet.
    """
    base = time.strftime("%Y%m%d-%H%M", time.localtime(now))
    name, number = base, 1
    while name in taken or (Path(project) / ROOT / name).exists():
        number += 1
        name = f"{base}-{number}"
    return name


def create(project, session):
    """Make the session's five lanes (each with a .gitkeep, so an empty lane can be committed)."""
    if session and not valid(session):
        raise ValueError(f"Invalid session name {session!r}: use letters, numbers, '.', '_' or '-'")
    lanes = lanes_dir(project, session)
    for lane in LANES:
        (lanes / lane).mkdir(parents=True, exist_ok=True)
        (lanes / lane / ".gitkeep").touch(exist_ok=True)
    return lanes


def sessions(project):
    """This project's sessions with tickets folders, most recently changed first."""
    root = Path(project) / ROOT
    if not root.is_dir():
        return []
    found = [path for path in root.iterdir() if path.is_dir() and valid(path.name) and (path / "to_do").is_dir()]

    def changed(path):
        return max((p.stat().st_mtime for p in [path, *path.iterdir()] if p.is_dir()), default=0)

    return [path.name for path in sorted(found, key=changed, reverse=True)]


def default(project):
    """The session a command-line run uses when none is named: the latest one, then swim_lanes/, then a new one."""
    existing = sessions(project)
    if existing:
        return existing[0]
    if (Path(project) / LEGACY / "to_do").is_dir():
        return None
    return new_name(project)


def count(project, session):
    """How many tickets each lane of a session holds."""
    lanes = lanes_dir(project, session)
    return {lane: len(list((lanes / lane).glob("[0-9]*_*.md"))) for lane in LANES}


def slug(name):
    """A session name made safe for a folder: "fix dashboard pr" -> "fix-dashboard-pr"."""
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", (name or "").strip()).strip("-.")
    text = re.sub(r"-{2,}", "-", text)[:64].rstrip("-.")
    return text if valid(text) else ""


def rename(project, old, new_name):
    """Move a session's tickets folder to a folder named after new_name. Returns the session's new name.

    A name already taken gets -2, -3 and so on. A folder that does not exist yet just takes the new name.
    """
    new = slug(new_name)
    if not new:
        raise ValueError(f"Cannot make a folder name from {new_name!r}")
    if new == old:
        return new
    root = Path(project) / ROOT
    base, number = new, 1
    while (root / new).exists():
        number += 1
        new = f"{base}-{number}"
    source = root / old if old else None
    if source is not None and source.is_dir():
        source.rename(root / new)
    return new
