"""Ponytail's task telemetry inside Donut.

Ponytail (github.com/DietrichGebert/ponytail) is a Claude Code and Codex plugin. Its skills and hooks already run
inside the provider sessions Donut opens, but its band, the line of task numbers above the prompt, is a Claude Code
UI module that Donut cannot load. So Donut keeps the same numbers per conversation (cost, turns, agent time, tokens,
tests, level, model), shows them in its own band, and writes the same row to the same ledger,
~/.config/ponytail/band/<project>.csv, so Ponytail's board lists Donut's tasks next to Claude Code's.
"""

import csv
import hashlib
import io
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from .plugins import claude_dir

LEVELS = ("lite", "full", "ultra")
HEADER = (
    "key,started,project,name,cost_usd,turns,agent_minutes,tokens_total,tokens_output,"
    "reuse_pct,reused,new,callers_open,tests,level,model"
)

# The same runners Ponytail's band recognizes, plus Python's unittest.
TEST_COMMAND = re.compile(
    r"\b(npm (run )?test|pnpm (run )?test|yarn test|bun test|npx (jest|vitest)|jest|vitest|pytest|python3? -m pytest"
    r"|python3? -m unittest|unittest discover|go test|cargo test|mvn test|gradle test|rspec|phpunit|dotnet test"
    r"|node --test|deno test|make test)\b"
)


def level(profile, project):
    """The live Ponytail level for this Claude login and project, as its hooks wrote it; "off" when none."""
    base = claude_dir(profile)
    digest = hashlib.sha256(os.path.normpath(str(project)).encode()).hexdigest()
    for flag in (base / "ponytail-modes" / digest, base / ".ponytail-active"):
        try:
            value = flag.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        return value if value in LEVELS else "off"
    return "off"


def test_counts(output):
    """(passed, failed) from common test runner summaries; None for a count it could not find."""

    def number(*patterns):
        for pattern in patterns:
            match = re.search(pattern, output)
            if match:
                return int(match.group(1))
        return None

    passed = number(r"(\d+) (?:passed|passing)\b", r"(?:ℹ |# )pass (\d+)")
    failed = number(r"(\d+) (?:failed|failing)\b", r"(?:ℹ |# )fail (\d+)", r"FAILED \((?:failures|errors)=(\d+)")
    ran = number(r"Ran (\d+) tests?\b")  # unittest: "Ran 12 tests ... OK" or "FAILED (failures=1)".
    if ran is not None and passed is None:
        passed = ran - (failed or 0)
    return passed, failed


def duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def tokens(count):
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 10_000:
        return f"{count / 1000:.0f}k"
    if count >= 1000:
        return f"{count / 1000:.1f}".removesuffix(".0") + "k"
    return str(count)


class Task:
    """One conversation's numbers, the row Ponytail's band keeps for a task. A /clear or /new starts a new one."""

    def __init__(self, project, engine, model):
        self.project = Path(project).resolve().name
        self.engine, self.model = engine, model or ""
        self.started = time.time()
        self.key = f"donut-{uuid.uuid4().hex[:8]}-{int(self.started * 1000)}"
        self.name = ""
        self.turns = 0
        self.cost = 0.0
        self.total = 0
        self.output = 0
        self.agent_seconds = 0.0
        self.tests = "not run"
        self.level = "off"
        self.reported_cost = None
        self.commands = {}

    def turn(self, prompt, seconds, usage):
        """Add one finished turn. usage is the session's own report: Claude's per-turn tokens and running cost,
        Codex's thread totals, or Qwen's per-turn tokens."""
        self.turns += 1
        self.agent_seconds += seconds
        if not self.name:
            self.name = " ".join(prompt.split())[:80]
        usage = usage or {}
        if self.engine == "codex":
            total = usage.get("total") or {}
            self.total = max(self.total, int(total.get("totalTokens") or 0))
            self.output = max(self.output, int(total.get("outputTokens") or 0))
            return
        turn = usage.get("tokens", usage) if self.engine == "claude" else usage
        if isinstance(turn, dict):
            output = int(turn.get("output_tokens") or 0)
            self.output += output
            self.total += output + sum(
                int(turn.get(key) or 0)
                for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
        reported = usage.get("reported_cost_usd")
        if isinstance(reported, (int, float)):
            # Claude reports the session's running cost; a lower figure means a new provider process started over.
            previous = self.reported_cost or 0.0
            self.cost += reported - previous if reported >= previous else reported
            self.reported_cost = reported

    def command(self, identity, text):
        if TEST_COMMAND.search(text or ""):
            self.commands[identity] = text

    def command_output(self, identity, output):
        """Turn a test command's output into the band's tests cell."""
        if self.commands.pop(identity, None) is None:
            return
        passed, failed = test_counts(output or "")
        if failed:
            self.tests = f"{failed} failed"
        elif passed is not None:
            self.tests = f"{passed} passed"

    def row(self):
        return [
            self.key,
            time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started)),
            self.project,
            self.name,
            f"{self.cost:.2f}",
            self.turns,
            f"{self.agent_seconds / 60:.1f}",
            self.total,
            self.output,
            "",  # reuse_pct, reused, new and callers_open come from Claude Code's edit hooks, which Donut lacks.
            0,
            0,
            0,
            self.tests,
            self.level,
            self.model,
        ]

    def band(self):
        """(label, value) pairs for Donut's band, in the order Ponytail's band shows them."""
        pairs = [("ponytail", self.level)]
        if self.engine == "claude":
            pairs.append(("cost", f"${self.cost:.2f}"))
        pairs += [
            ("turns", str(self.turns)),
            ("agent", duration(self.agent_seconds)),
            ("tokens", tokens(self.total)),
            ("tests", self.tests),
        ]
        return pairs


def ledger_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ponytail" / "band"


def record(task):
    """Write the task's row into its project's ledger, replacing its earlier row. Returns the ledger path."""
    folder = ledger_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (re.sub(r"[^\w.-]", "_", task.project) + ".csv")
    try:
        existing = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        existing = []
    rows = [line for line in existing if line and not line.startswith(("key,", task.key + ","))]
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(task.row())
    text = "\n".join([HEADER, *rows, buffer.getvalue()]) + "\n"
    fd, temp = tempfile.mkstemp(dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return path
