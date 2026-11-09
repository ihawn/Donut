"""Shell commands outside a model's turn: `!command` from the user, and waits that wake the model when they end.

Claude Code wakes its own model when a background shell finishes, and Donut keeps listening between turns so
those turns show up. Codex and Qwen have no such wake-up, so Donut offers one: a model ends its reply with
`DONUT-WAIT: <command that exits when the work is done>`, Donut runs it, and when it exits Donut sends the model
its exit code and output so it carries on from there.
"""

import asyncio
import re
from dataclasses import dataclass, field

from .platform import process_options

WAIT_LINE = re.compile(r"^\s*`?DONUT-WAIT:\s*(.+?)`?\s*$", re.M)
OUTPUT_LIMIT = 4000
WAIT_LIMIT_SECONDS = 6 * 3600

WAIT_NOTE = (
    "\n\n(Donut: when you need to wait for something slow, such as a build, a CI run or a deploy, you can stop "
    "instead of polling. Start it in the background, then end your reply with one line "
    "`DONUT-WAIT: <shell command that exits when it is done>`, for example `DONUT-WAIT: gh run watch 123` or "
    "`DONUT-WAIT: while kill -0 4242; do sleep 10; done`. Donut runs that command in the project and, when it "
    "exits, sends you its exit code and output so you can continue.)"
)


def wait_request(answer):
    """The command from the last DONUT-WAIT line of an answer, or None."""
    matches = WAIT_LINE.findall(answer or "")
    return matches[-1].strip().strip("`").strip() if matches else None


def tail(text, limit=OUTPUT_LIMIT):
    return text if len(text) <= limit else "…" + text[-limit:]


def wake_prompt(command, code, output):
    state = "timed out" if code is None else f"exited with code {code}"
    return (
        f"Donut: the background command you asked to wait on {state}.\n$ {command}\n"
        f"Output (end):\n```\n{tail(output) or '(no output)'}\n```\nContinue where you left off."
    )


def shell_note(runs):
    """What the user ran with !command since their last message, for the model's context."""
    blocks = [f"$ {command}\n{tail(output, 2000) or '(no output)'}\n(exit {code})" for command, code, output in runs]
    return "Shell commands I ran in the project before this message:\n```\n" + "\n\n".join(blocks) + "\n```\n\n"


async def start(command, project):
    """Start a shell command in the project in its own process group (so Ctrl+C can stop the whole group)."""
    return await asyncio.create_subprocess_shell(
        command,
        cwd=project,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **process_options(),
    )


async def wait_for(command, project, limit=WAIT_LIMIT_SECONDS):
    """Run a wait command to its end. Returns (exit code, or None when it ran past the limit; output)."""
    process = await start(command, project)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), limit)
    except asyncio.TimeoutError:
        process.kill()
        output, _ = await process.communicate()
        return None, output.decode("utf-8", errors="replace")
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        raise
    return process.returncode, output.decode("utf-8", errors="replace")


@dataclass
class Wake:
    """A turn Donut starts on its own, to give a model the result of the command it was waiting on."""

    session: object
    role: str
    command: str
    code: object
    output: str
    created: float = field(default=0.0)

    @property
    def prompt(self):
        return wake_prompt(self.command, self.code, self.output)
