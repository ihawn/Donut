"""A per-user log of terminal conversations, so /resume can reopen one with its mode and model.

Each record keeps the mode, role, alias and model, the provider's own session or thread id (what actually
resumes the conversation), a title from the first message, and the last few messages for a recap. It lives
with the run logs under $XDG_STATE_HOME/donut, readable only by the user, because it holds project content.
"""

import json
import os
from pathlib import Path
import tempfile
import time
import uuid

KEEP_MESSAGES = 12
MESSAGE_CHARS = 2000
KEEP_RECORDS = 200


def store_path():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "donut" / "conversations.json"


def load_all():
    try:
        records = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [record for record in records if isinstance(record, dict) and record.get("id")]


def _write(records):
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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


def get(record_id):
    return next((record for record in load_all() if record["id"] == record_id), None)


def record_turn(record_id, project, mode, role, alias, model, provider_id, user_text, answer):
    """Add one exchange to a conversation, creating it on its first turn. Returns the record's id."""
    records = load_all()
    record = next((r for r in records if r["id"] == record_id), None) if record_id else None
    now = time.time()
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
    if provider_id:
        record["provider_id"] = provider_id
    record["turns"] = record.get("turns", 0) + 1
    messages = record.setdefault("messages", [])
    messages.append({"from": "you", "text": user_text[:MESSAGE_CHARS]})
    messages.append({"from": "model", "text": (answer or "")[:MESSAGE_CHARS]})
    del messages[:-KEEP_MESSAGES]
    records.sort(key=lambda r: r.get("updated", 0))
    _write(records)
    return record["id"]


def ago(timestamp, now=None):
    """1700000000 -> "5m ago", "3h ago", "2d ago"."""
    seconds = max(0, int((now if now is not None else time.time()) - (timestamp or 0)))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"
