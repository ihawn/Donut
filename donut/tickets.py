"""Read swim-lane tickets tolerantly and explain why queued work is not runnable."""

import re
from pathlib import Path

LANES = ("to_do", "in_progress", "on_hold", "blocked", "done")
RUNNABLE_LANES = ("in_progress", "to_do")
HELD_LANES = ("on_hold", "blocked")

_FIELD = r"^\s*(?:[-*]\s*)?\*\*{name}:\*\*\s*(.*)$"
_NONE = re.compile(r"(?:none|n/a|-)?\.?\s*(?:\(.*\))?", re.I)
_SLUG_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_PULL_REQUEST = re.compile(r"^\s*(?:[-*]\s*)?PR:\s*<?(https?://[^\s>)]+)", re.M)


def pull_requests(text):
    """The pull requests a ticket records: its `PR: <url>` lines, in order, each once."""
    return list(dict.fromkeys(_PULL_REQUEST.findall(text or "")))


def add_pull_requests(path, urls):
    """Record pull requests Donut opened in the ticket, as `PR: <url>` lines, so /prev can list them."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
        new = [url for url in urls if url not in pull_requests(text)]
        if new:
            path.write_text(text.rstrip("\n") + "\n" + "".join(f"PR: {url}\n" for url in new), encoding="utf-8")
    except OSError:
        pass  # The pull request exists either way; only its line in the ticket is missing.


def tasks(project, lane, lanes=None):
    """A lane's tickets; lanes is the session's lanes folder (default: the project's swim_lanes/)."""
    return sorted((Path(lanes or Path(project) / "swim_lanes") / lane).glob("[0-9]*_*.md"))


def field(text, name):
    """Return the value of a `**Name:** value` line, or None when the line is absent."""
    match = re.search(_FIELD.format(name=re.escape(name)), text, re.M)
    return match[1].strip() if match else None


def filename_slug(path):
    """Fallback identity for tickets without Slug metadata: `012_20240101-1200_my-slug.md` -> `my-slug`."""
    return re.sub(r"^\d+_(?:\d{8}-\d{4}_)?", "", Path(path).stem)


def parse_dependencies(value):
    """Accept backtick-quoted slugs, bare comma-separated slugs, or None."""
    if value is None or _NONE.fullmatch(value):
        return []
    quoted = re.findall(r"`([^`]+)`", value)
    if quoted:
        return [name.strip() for name in quoted if name.strip()]
    return [token for token in _SLUG_TOKEN.findall(value) if token.lower() != "none"]


class Ticket:
    """A ticket file plus the metadata the runner needs. Never raises for malformed files."""

    def __init__(self, path, lane):
        self.path = Path(path)
        self.lane = lane
        self.warnings = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as error:
            text = ""
            self.warnings.append(f"unreadable ({error.strerror or error})")
        self.pull_requests = pull_requests(text)
        # The heading, and the short description under it, for the /next and /prev tables.
        lines = [line.strip() for line in text.splitlines()]
        heading = next((line for line in lines if line.startswith("# ")), "")
        self.title = heading[2:].strip()
        after = lines[lines.index(heading) + 1 :] if heading else []
        self.summary = next((line for line in after if line and not line.startswith(("#", "**", "- **", "* **"))), "")
        slug = field(text, "Slug")
        self.slug = slug.strip("` ") if slug else ""
        if not self.slug:
            self.slug = filename_slug(self.path)
            self.warnings.append(f"missing **Slug:**; using `{self.slug}` from the filename")
        depends = field(text, "Depends on")
        if depends is None:
            self.warnings.append("missing **Depends on:**; assuming none")
        self.dependencies = parse_dependencies(depends)
        verify = field(text, "Verify")
        self.verify = verify.strip("` ") if verify else ""
        if self.verify and _NONE.fullmatch(self.verify):
            self.verify = ""

    @property
    def name(self):
        return self.path.name


def table(tickets):
    """A Markdown table of tickets: number, title, lane, overview and pull requests."""

    def cell(text, width):
        text = " ".join((text or "").replace("|", "/").split())
        return text if len(text) <= width else text[: width - 1] + "…"

    rows = ["| # | Ticket | Status | Overview | PR |", "|---|---|---|---|---|"]
    for ticket in tickets:
        number = re.match(r"\d+", ticket.name)
        rows.append(
            f"| {number[0] if number else ''} | {cell(ticket.title or ticket.slug, 60)} "
            f"| {ticket.lane.replace('_', ' ')} | {cell(ticket.summary, 160) or '-'} "
            f"| {' '.join(ticket.pull_requests) or '-'} |"
        )
    return "\n".join(rows)


def upcoming(project, lanes=None, count=None):
    """Tickets still to do, in the order they run: in progress first, then to do. count keeps the first few."""
    found = [ticket for ticket in load(project, lanes) if ticket.lane in RUNNABLE_LANES]
    found.sort(key=lambda ticket: RUNNABLE_LANES.index(ticket.lane))
    return found[:count] if count else found


def finished(project, lanes=None, count=None):
    """Tickets already worked (done or blocked), the latest number first. count keeps the latest few."""
    found = [ticket for ticket in load(project, lanes) if ticket.lane in ("done", "blocked")]
    found.sort(key=lambda ticket: ticket.name, reverse=True)
    return found[:count] if count else found


def load(project, lanes=None):
    """Every ticket in every lane, in lane order then number order."""
    return [Ticket(path, lane) for lane in LANES for path in tasks(project, lane, lanes)]


def _index(tickets):
    done = {t.slug for t in tickets if t.lane == "done"}
    held = {t.slug for t in tickets if t.lane in HELD_LANES}
    return done, held


def blockers(ticket, tickets, done, held):
    """Human-readable reasons a runnable-lane ticket cannot start right now."""
    reasons = []
    if ticket.slug in held:
        reasons.append(f"slug `{ticket.slug}` is also in on_hold/ or blocked/")
    known = {t.slug for t in tickets}
    for dependency in ticket.dependencies:
        if dependency in done and dependency not in held:
            continue
        if dependency in held:
            reasons.append(f"depends on `{dependency}`, which is on hold or blocked")
        elif dependency in known:
            reasons.append(f"waiting for `{dependency}` to reach done/")
        else:
            reasons.append(f"depends on unknown slug `{dependency}`")
    return reasons


def pick(project, attempted, lanes=None):
    """The next runnable ticket: in-progress work first, then the lowest numbered eligible ticket."""
    tickets = load(project, lanes)
    done, held = _index(tickets)
    for lane in RUNNABLE_LANES:
        for ticket in tickets:
            if ticket.lane != lane or ticket.name in attempted:
                continue
            if not blockers(ticket, tickets, done, held):
                return ticket.path
    return None


def queue_report(project, attempted=(), lanes=None):
    """Lines explaining every unrun ticket in to_do/ and in_progress/, plus metadata warnings in any lane."""
    tickets = load(project, lanes)
    done, held = _index(tickets)
    lines = []
    for ticket in tickets:
        for warning in ticket.warnings:
            lines.append(f"{ticket.lane}/{ticket.name}: {warning}")
    for ticket in tickets:
        if ticket.lane not in RUNNABLE_LANES or ticket.name in attempted:
            continue
        reasons = blockers(ticket, tickets, done, held)
        if reasons:
            lines.append(f"{ticket.lane}/{ticket.name}: " + "; ".join(reasons))
    return lines
