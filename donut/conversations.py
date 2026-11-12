"""A per-user log of terminal conversations, so /resume can reopen one with its mode and model.

Each record keeps the mode, role, alias and model, the provider's own session or thread id (what actually
resumes the conversation), a title from the first message, and the last few messages for a recap. It lives
with the run logs under $XDG_STATE_HOME/donut, readable only by the user, because it holds project content.
"""

import json
import os
import re
from pathlib import Path
import tempfile
import time
import uuid
from .config import state_dir

KEEP_MESSAGES = 12
MESSAGE_CHARS = 2000
KEEP_RECORDS = 200


def store_path():
    return state_dir() / "conversations.json"


def load_all():
    try:
        records = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [record for record in records if isinstance(record, dict) and record.get("id")]


def transcript_path(record_id):
    return store_path().with_name("transcripts") / f"{record_id}.jsonl"


def transcript(record):
    """Every exchange of a conversation as (you, answer), in full, for /resume to show.

    A conversation saved before transcripts were kept has only its last few exchanges, each cut short.
    """
    try:
        lines = transcript_path(record["id"]).read_text(encoding="utf-8").splitlines()
        return [(turn.get("you", ""), turn.get("answer", "")) for turn in map(json.loads, lines)]
    except (OSError, ValueError, AttributeError):
        messages = record.get("messages", [])
        return [(you.get("text", ""), model.get("text", "")) for you, model in zip(messages[::2], messages[1::2])]


def display_name(record):
    """The name /resume lists a conversation under: the name it was given, else its renamed session, else None."""
    session = record.get("session") or ""
    # A session still named after its start time (20261109-1712) was never renamed.
    return record.get("name") or (session if not re.fullmatch(r"\d{8}-\d{4}(-\d+)?", session) else None) or None


def _write(records):
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for record in records[:-KEEP_RECORDS]:
        transcript_path(record["id"]).unlink(missing_ok=True)  # A conversation that leaves the list takes its text.
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(records[-KEEP_RECORDS:], file, indent=1)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def for_project(project):
    """This project's conversations, most recently used first."""
    project = str(Path(project).resolve())
    records = [record for record in load_all() if record.get("project") == project]
    return sorted(records, key=lambda record: record.get("updated", 0), reverse=True)


def by_session(records):
    """Records grouped into sessions, newest session first, each with its newest record first.

    Each mode (plan, solo, review) keeps a conversation of its own, so one session holds several records.
    /resume lists the session once. A record from before sessions stands alone.
    """
    groups = {}
    for record in sorted(records, key=lambda record: record.get("updated", 0), reverse=True):
        groups.setdefault(record.get("session") or record["id"], []).append(record)
    return list(groups.values())


def get(record_id):
    return next((record for record in load_all() if record["id"] == record_id), None)


def record_turn(
    record_id, project, mode, role, alias, model, provider_id, user_text, answer, session=None, dollars=None
):
    """Add one exchange to a conversation, creating it on its first turn. Returns the record's id."""
    records = load_all()
    record = next((r for r in records if r["id"] == record_id), None) if record_id else None
    # Strictly later than any saved turn, so "most recent first" holds even when the clock is coarse (Windows).
    now = max([time.time()] + [r.get("updated", 0) + 1e-6 for r in records])
    if record is None:
        record = {
            "id": record_id or uuid.uuid4().hex,
            "project": str(Path(project).resolve()),
            "title": " ".join(user_text.split())[:80] or "(untitled)",
            "created": now,
            "turns": 0,
            "messages": [],
        }
        records.append(record)
    record.update(mode=mode, role=role, alias=alias, model=model or "", updated=now)
    if session:
        record["session"] = session  # The tickets folder, .donut/<session>/, that /resume switches back to.
        if "name" not in record:
            # A conversation started in a renamed session is listed under that session's name too.
            named = next(
                (
                    r.get("name")
                    for r in records
                    if r.get("project") == record["project"] and r.get("session") == session and r.get("name")
                ),
                None,
            )
            if named:
                record["name"] = named
    if provider_id:
        record["provider_id"] = provider_id
    record["turns"] = record.get("turns", 0) + 1
    messages = record.setdefault("messages", [])
    messages.append({"from": "you", "text": user_text[:MESSAGE_CHARS]})
    messages.append({"from": "model", "text": (answer or "")[:MESSAGE_CHARS]})
    del messages[:-KEEP_MESSAGES]
    records.sort(key=lambda r: r.get("updated", 0))
    _write(records)
    path = transcript_path(record["id"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The model that answered and what it cost, for donut stats.
    line = {"you": user_text, "answer": answer or "", "model": model or alias, "dollars": dollars}
    with open(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a", encoding="utf-8") as file:
        file.write(json.dumps(line) + "\n")
    return record["id"]


def ago(timestamp, now=None):
    """1700000000 -> "5m ago", "3h ago", "2d ago"."""
    seconds = max(0, int((now if now is not None else time.time()) - (timestamp or 0)))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def projects_path():
    return store_path().with_name("projects.json")


def remember_project(project):
    """Note that Donut opened this project, newest first, for /project."""
    project = str(Path(project).resolve())
    try:
        known = json.loads(projects_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = []
    known = [project] + [p for p in known if isinstance(p, str) and p != project]
    path = projects_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(known[:KEEP_RECORDS], file, indent=1)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def recent_projects():
    """Projects Donut has opened or talked about, most recent first, that still exist."""
    try:
        known = json.loads(projects_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = []
    by_use = sorted(load_all(), key=lambda record: record.get("updated", 0), reverse=True)
    ordered = []
    for project in [p for p in known if isinstance(p, str)] + [r.get("project") for r in by_use]:
        if project and project not in ordered and Path(project).is_dir():
            ordered.append(project)
    return ordered


def rename_session(project, old, new, name=None):
    """Point this project's saved conversations at a renamed session: its tickets folder and the name /resume
    shows for them (the name as typed, such as "fix login bug", where the folder is fix-login-bug)."""
    if not old:
        return  # Conversations from before sessions have none to follow.
    project = str(Path(project).resolve())
    records = load_all()
    changed = False
    for record in records:
        if record.get("project") == project and record.get("session") == old:
            record["session"] = new
            if name:
                record["name"] = name
            changed = True
    if changed:
        _write(records)
