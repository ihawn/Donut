"""Donut's interactive terminal, backed by provider-owned sessions."""

import asyncio
import copy
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from rich.text import Text
from textual import work
from textual.message import Message
from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Footer, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option
from . import conversations
from .config import forget_role, load, remember_role, role_choice, save
from .highlight import Highlighter
from .summaries import claude_context, codex_context, mcp_servers, render_mcp
from .sessions import ClaudeSession, CodexSession, QwenSession
from .planner_output import ticket_snapshot
from .install_skills import install
from .platform import process_options
from .providers import PERMISSION_CYCLE, PERMISSION_MODES, ollama_base_url
from .ui import COLORS, logo_segments, release_version


def hex_color(name):
    return "#%02x%02x%02x" % COLORS[name]


ORANGE, CORAL, TEAL, NAVY, GRAY = (hex_color(name) for name in ("orange", "coral", "teal", "navy", "gray"))
DIM = "#8a96a3"

# Lines the batch runner prints that belong in the status bar, not the transcript.
RUNNER_PROGRESS = re.compile(r"^\[(?P<label>[^\]]*?) · (?P<seconds>\d+)s elapsed · waiting for agent output\]$")
RUNNER_TICKET = re.compile(r"^\[\d+/(?:\d+|all)\] (?P<ticket>\S+)")
RUNNER_REVIEW = re.compile(r"^\[review \d+/\d+\] (?P<ticket>\S+)")
RUNNER_RECOVER = re.compile(r"^\[recover\] (?P<ticket>\S+)")
# Each mode talks to the model of the role with the same key. /planner, /worker, /reviewer and /soloist pick
# those models; /plan, /work, /review and /solo enter the mode, asking for a model only when none is set.
ROLE_TITLES = {"plan": "planner", "work": "worker", "review": "reviewer", "solo": "soloist"}
ROLE_COMMANDS = {"plan": "/planner", "work": "/worker", "review": "/reviewer", "solo": "/soloist"}
MODE_PLACEHOLDERS = {
    "plan": "Describe what to plan, or type / for commands",
    "work": "Describe what to plan, or type / for commands",
    "review": "Ask the reviewer to check something (it reads, never edits), or type / for commands",
    "solo": "Ask the soloist anything or tell it what to change, or type / for commands",
}
MODE_BADGES = {"plan": ORANGE, "work": CORAL, "review": NAVY, "solo": TEAL}
REVIEW_MODE_PROMPT = (
    "You are the reviewer. Review what is asked below by reading the project; run read-only checks if useful. "
    "Do not edit, create, move or delete files, and do not commit. Point out real problems with file and line, "
    "say how serious each is, and say plainly when something looks right.\n\nReview request:\n"
)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Friendlier names for the activity events providers report.
ACTIVITY_NAMES = {
    "reasoning": "thinking",
    "agentMessage": "writing",
    "commandExecution": "running a command",
    "fileChange": "editing files",
    "mcpToolCall": "calling a tool",
    "webSearch": "searching the web",
    "Bash": "running a command",
    "Read": "reading files",
    "Glob": "searching files",
    "Grep": "searching files",
    "Edit": "editing files",
    "MultiEdit": "editing files",
    "Write": "writing a file",
    "WebFetch": "fetching a page",
    "WebSearch": "searching the web",
    "TodoWrite": "updating its plan",
    "Task": "running a subagent",
    "read_file": "reading files",
    "write_file": "writing a file",
    "edit": "editing files",
    "run_shell_command": "running a command",
}


def usage_key(profile):
    """Which quota a profile draws from: one per Claude login folder, one for Codex, one per local model."""
    engine = profile.get("engine")
    if engine == "claude":
        return ("claude", profile.get("claude_config_dir") or "default")
    if engine == "codex":
        return ("codex",)
    return ("local", profile.get("model") or engine)


def remaining(percent_used):
    return max(0, min(100, round(100 - percent_used)))


def left_color(left):
    """Teal while plenty is left, orange under half, coral under a fifth."""
    return TEAL if left > 50 else ORANGE if left > 20 else CORAL


def usage_bar(percent_used, width=6):
    """A thin bar showing how much of the window is left."""
    left = remaining(percent_used)
    filled = max(0, min(width, round(left / 100 * width)))
    color = left_color(left)
    bar = Text("━" * filled, style=color)
    bar.append("─" * (width - filled), style="#3b4652")
    return bar, color


def countdown(resets_at, now=None):
    """Seconds since the epoch -> "2h10m", "3d4h", "45m"; empty when unknown or past."""
    if not resets_at:
        return ""
    seconds = int(resets_at - (now if now is not None else time.time()))
    if seconds <= 0:
        return ""
    days, hours, minutes = seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{max(minutes, 1)}m"


def pack_entries(entries, width, gap="  "):
    """Join Text entries on one line, starting a new line before an entry that would not fit, so no entry splits."""
    packed, used = Text(), 0
    for entry in entries:
        size = entry.cell_len
        if used and width and used + len(gap) + size > width:
            packed.append("\n")
            used = 0
        elif used:
            packed.append(gap)
            used += len(gap)
        packed.append_text(entry)
        used += size
    return packed


def model_name(profile, alias):
    return profile.get("model") or alias or "provider default"


COMMANDS = [
    ("/plan", "Plan mode: describe work and the planner writes tickets"),
    ("/work", "Run tickets with the worker, one fresh context each"),
    ("/review", "Review mode: ask the reviewer to check code; it reads, never edits"),
    ("/solo", "Solo mode: talk to the soloist directly, like Claude Code"),
    ("/planner", "Choose the planner's account and model"),
    ("/worker", "Choose the worker's account and model"),
    ("/reviewer", "Choose who reviews: same as planner, any model, or off"),
    ("/soloist", "Choose the soloist's account and model"),
    ("/model", "Choose the model for the current mode"),
    ("/resume", "Resume an earlier conversation in its mode and model"),
    ("/permissions", "Choose what the agent may do without asking (Shift+Tab cycles)"),
    ("/auto", "Turn auto mode on or off: the provider's reviewer approves safe actions"),
    ("/usage", "Show usage for each configured connection"),
    ("/plugin", "Browse the provider's plugin commands"),
    ("/mcp", "Show MCP server status"),
    ("/context", "Show context usage for this conversation"),
    ("/clear", "Start a fresh conversation; tickets are kept"),
    ("/copy", "Copy the last answer"),
    ("/native", "Open the provider's own client, then come back"),
    ("/help", "List commands and keys"),
    ("/exit", "Quit Donut"),
]


CLAUDE_TOOL_TITLES = {
    "Bash": ("Bash command", "Bash"),
    "Edit": ("Edit file", "edits"),
    "MultiEdit": ("Edit file", "edits"),
    "Write": ("Write file", "file writes"),
    "NotebookEdit": ("Edit notebook", "notebook edits"),
    "WebFetch": ("Fetch", "fetches"),
    "WebSearch": ("Web search", "web searches"),
}


def approval_title(name, details):
    """(panel title, short phrase for "don't ask again for …") for a Claude tool name or a Codex request method."""
    if "commandExecution" in name:
        return "Codex command", "commands"
    if "fileChange" in name:
        return "Codex file change", "file changes"
    if name in CLAUDE_TOOL_TITLES:
        return CLAUDE_TOOL_TITLES[name]
    if name.startswith("mcp__"):
        server, _, tool = name[5:].partition("__")
        return f"{server} · {tool}", tool or server
    return name, name


def _command_text(details):
    command = details.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return str(command) if command is not None else ""


def _clip(text, width=160):
    text = str(text)
    return text if len(text) <= width else text[: width - 1] + "…"


def approval_summary(details):
    """One short line for the transcript after a decision."""
    if "command" in details:
        return _clip(_command_text(details).splitlines()[0] if _command_text(details) else "", 100)
    for key in ("file_path", "notebook_path", "path", "url", "query", "grantRoot"):
        if details.get(key):
            return _clip(details[key], 100)
    return ""


def approval_details(details):
    """What the agent wants to do, shaped like Claude Code's permission prompt rather than raw JSON."""
    body = Text()

    def lines(text, prefix, style, limit):
        rows = str(text).splitlines() or [""]
        for row in rows[:limit]:
            body.append(prefix + _clip(row) + "\n", style=style)
        if len(rows) > limit:
            body.append(f"  … {len(rows) - limit} more lines\n", style=DIM)

    if "command" in details:
        command = _command_text(details)
        body.append("$ ", style=DIM)
        lines(command, "", f"bold {ORANGE}", 6)
        if details.get("description"):
            body.append(_clip(details["description"]) + "\n", style=DIM)
        if details.get("cwd"):
            body.append("in " + _clip(details["cwd"]) + "\n", style=DIM)
    path = details.get("file_path") or details.get("notebook_path") or details.get("path")
    if path:
        body.append(_clip(path) + "\n", style=f"bold {ORANGE}")
    edits = details.get("edits") if isinstance(details.get("edits"), list) else None
    if "old_string" in details or "new_string" in details:
        lines(details.get("old_string", ""), "- ", CORAL, 5)
        lines(details.get("new_string", ""), "+ ", TEAL, 5)
    elif edits:
        body.append(f"{len(edits)} edits\n", style=GRAY)
        for edit in edits[:2]:
            if isinstance(edit, dict):
                lines(edit.get("old_string", ""), "- ", CORAL, 3)
                lines(edit.get("new_string", ""), "+ ", TEAL, 3)
    elif "content" in details and path:
        lines(details["content"], "  ", GRAY, 8)
    for key, label in (("url", ""), ("query", ""), ("prompt", ""), ("grantRoot", "write access to "), ("reason", "")):
        if details.get(key) and not (key == "prompt" and "url" not in details):
            body.append(label + _clip(details[key]) + "\n", style=GRAY if key != "reason" else DIM)
    if not body.plain.strip():
        shown = 0
        for key, value in details.items():
            if shown == 6:
                break
            if isinstance(value, (str, int, float, bool)) and str(value).strip():
                body.append(f"{key}: ", style=DIM)
                body.append(_clip(value) + "\n", style=GRAY)
                shown += 1
        if not shown:
            body.append(_clip(json.dumps(details, ensure_ascii=False), 300) + "\n", style=GRAY)
    body.append("\nDo you want to proceed?", style=f"bold {GRAY}")
    return body


class Menu(Vertical):
    """An inline list under the transcript, driven from the prompt like Claude Code's menus.

    Focus never leaves the prompt: typing filters the rows, Up/Down move the highlight, Enter or Tab
    choose, and Escape closes. It serves slash-command completion, pickers and tool approvals.
    """

    DEFAULT_CSS = f"""
    Menu {{height: auto; max-height: 14; display: none; padding: 0 1; margin: 0 1;
           border: round {CORAL}; border-title-color: {ORANGE}; border-title-style: bold;
           border-subtitle-color: {DIM};}}
    Menu.-open {{display: block;}}
    Menu OptionList {{border: none; padding: 0; background: transparent; max-height: 12;}}
    Menu OptionList > .option-list--option-highlighted {{background: #3a2a10;}}
    Menu OptionList > .option-list--option-hover {{background: {NAVY};}}
    Menu #menu-empty {{color: {DIM}; display: none;}}
    Menu #menu-details {{display: none; height: auto; max-height: 16; padding: 0 1 1 1;}}
    Menu.-approve {{max-height: 26; border: round {ORANGE}; border-subtitle-color: {DIM};}}
    Menu.-approve #menu-details {{display: block;}}
    """

    class Chosen(Message):
        """row is the highlighted (label, value, description) or None when dismissed; key is how."""

        def __init__(self, row, key="enter"):
            super().__init__()
            self.row, self.key = row, key

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.rows = []
        self.visible_rows = []
        self.kind = None

    def compose(self):
        yield Static(id="menu-details")
        options = OptionList(id="menu-options")
        options.can_focus = False
        yield options
        yield Static("No matches", id="menu-empty")

    @property
    def is_open(self):
        return self.has_class("-open")

    def open(self, kind, title, rows, term="", details=None, subtitle=""):
        """rows: (label, value), (label, value, description) or (label, value, description, command) tuples.

        command is shown as a third column: the typed command that does the same thing.

        kind is "slash", "pick" (typing filters) or "approve" (fixed rows, number keys choose, details shown).
        """
        self.kind = kind
        self.border_title = title
        self.border_subtitle = subtitle
        self.rows = [tuple(row) + ("",) * (4 - len(row)) for row in rows]
        self.set_class(kind == "approve", "-approve")
        self.query_one("#menu-details", Static).update(details or "")
        self.add_class("-open")
        self.filter(term)

    def close(self):
        self.kind = None
        self.remove_class("-open", "-approve")

    def filter(self, term):
        term = term.strip().casefold()
        starts = [row for row in self.rows if row[0].casefold().startswith(term)]
        contains = [row for row in self.rows if term in row[0].casefold() and row not in starts]
        self.visible_rows = starts + contains
        self.render_rows(0)
        self.query_one("#menu-empty").display = not self.visible_rows
        return bool(self.visible_rows)

    def render_rows(self, highlighted):
        """Redraw the rows; the highlighted one gets a pointer and brighter text, like Claude Code."""
        width = max((len(row[0]) for row in self.visible_rows), default=0)
        described = max((len(row[2]) for row in self.visible_rows), default=0)
        prompts = []
        for index, (label, _, description, command) in enumerate(self.visible_rows):
            chosen = index == highlighted
            prompt = Text("› " if chosen else "  ", style=f"bold {CORAL}")
            if self.kind == "approve":
                prompt.append(f"{index + 1}. ", style=ORANGE if chosen else DIM)
            prompt.append(label.ljust(width), style=f"bold {ORANGE}" if chosen else ORANGE)
            if description or command:
                prompt.append("   " + description.ljust(described), style="#ffffff" if chosen else DIM)
            if command:
                prompt.append("   " + command, style=f"bold {TEAL}" if chosen else TEAL)
            prompts.append(Option(prompt))
        options = self.query_one(OptionList)
        options.set_options(prompts)
        if prompts:
            options.highlighted = highlighted

    def move(self, step):
        if self.visible_rows:
            index = self.query_one(OptionList).highlighted or 0
            self.render_rows((index + step) % len(self.visible_rows))

    def current(self):
        index = self.query_one(OptionList).highlighted
        if index is None or index >= len(self.visible_rows):
            return None
        return self.visible_rows[index]

    def on_option_list_option_selected(self, event):
        event.stop()
        self.render_rows(event.option_index)
        self.post_message(self.Chosen(self.current(), "enter"))


class Draft(TextArea):
    """The prompt editor. Enter sends; Shift+Enter, Alt+Enter or Ctrl+J insert a newline.

    Plain terminals send the same bytes for Enter and Ctrl+Enter, so Enter has to be the send key to work
    everywhere. Ctrl+Enter still sends in terminals that distinguish it (kitty, WezTerm, Ghostty, Windows Terminal).
    """

    SEND_KEYS = ("enter", "ctrl+enter")
    NEWLINE_KEYS = ("shift+enter", "alt+enter", "ctrl+j")

    class Submitted(Message):
        pass

    async def _on_key(self, event):
        menu = self.app.query_one(Menu)
        if menu.is_open and event.key in ("up", "down", "enter", "tab", "escape", "ctrl+enter"):
            event.stop()
            event.prevent_default()
            if event.key in ("up", "down"):
                menu.move(-1 if event.key == "up" else 1)
            elif event.key == "escape":
                self.post_message(Menu.Chosen(None, "escape"))
            else:
                self.post_message(Menu.Chosen(menu.current(), event.key))
            return
        if menu.kind == "approve" and event.key.isdigit() and 1 <= int(event.key) <= len(menu.visible_rows):
            event.stop()
            event.prevent_default()
            menu.render_rows(int(event.key) - 1)
            self.post_message(Menu.Chosen(menu.current(), "enter"))
            return
        if event.key in self.SEND_KEYS:
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted())
        elif event.key in self.NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            self.insert("\n")
        else:
            await super()._on_key(event)


class Donut(App):
    TITLE = "Donut"
    QUOTA_REFRESH_SECONDS = 300
    AUTO_REFRESH_QUOTAS = True  # Tests turn this off so no provider is started in the background.
    ENABLE_COMMAND_PALETTE = False  # The inline slash menu replaces Textual's palette.
    CSS = f"""
    Screen {{background: #101820;}}
    #transcript {{height: 1fr; padding: 0 1; background: #101820; scrollbar-size-vertical: 1;
                   scrollbar-background: #101820; scrollbar-color: {CORAL} 50%; scrollbar-color-hover: {ORANGE};}}
    #response {{height: auto; max-height: 20; overflow-y: auto; padding: 0 1;}}
    #thinking {{height: 1; padding: 0 2; display: none;}}
    #thinking.-busy {{display: block;}}
    #usage {{height: 1; padding: 0 2;}}
    #draft {{height: auto; min-height: 3; max-height: 10; margin: 0 1; border: round {CORAL} 60%;
             background: #101820;}}
    #draft:focus {{border: round {ORANGE};}}
    #status {{height: auto; max-height: 3; padding: 0 2;}}
    Footer {{background: #101820;}}
    Footer FooterKey .footer-key--key {{color: {ORANGE}; background: #101820; text-style: bold;}}
    Footer FooterKey .footer-key--description {{color: {DIM};}}
    """
    BINDINGS = [
        Binding("enter", "send", "Send"),
        # priority=True: the editor binds ctrl+c to copy, which would otherwise swallow the cancel key.
        Binding("ctrl+c", "cancel", "Cancel", priority=True),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+l", "clear", "Clear view"),
        Binding("f2", "models", "Models"),
        Binding("f3", "modes", "Mode"),
        # priority=True: the editor would otherwise use shift+tab itself.
        Binding("shift+tab", "cycle_permission", "Permissions", priority=True),
    ]

    def __init__(self, project, alias=None):
        super().__init__()
        self.project = project
        self.config = load()
        self.mode = "plan"  # plan, work, review or solo
        aliases = self.config.get("aliases", {})
        if alias and alias not in aliases:
            raise ValueError("Unknown model alias: " + alias)
        self.selected, self.profiles = {}, {}
        for role, key in (("plan", "planner"), ("work", "default_model")):
            saved = None if alias else role_choice(self.config, role)
            if saved:
                self.selected[role], self.profiles[role] = saved[0], copy.deepcopy(saved[1])
            else:
                name = alias or self.config.get(key) or next(iter(aliases), None)
                self.selected[role], self.profiles[role] = name, copy.deepcopy(aliases.get(name, {}))
        soloist = role_choice(self.config, "solo")  # No default: /solo asks the first time.
        self.selected["solo"], self.profiles["solo"] = (
            (soloist[0], copy.deepcopy(soloist[1])) if soloist else (None, {})
        )
        # Who reviews each finished ticket: "planner" follows the planner (the default), "off" disables review,
        # and "chosen" keeps the alias and model picked with /review whatever the planner becomes.
        roles = self.config.get("roles") or {}
        if self.config.get("reviewer") == "off":
            self.reviewer = "off"
        elif roles.get("review") or self.config.get("reviewer") in aliases:
            self.reviewer = "chosen"
            chosen = role_choice(self.config, "review")
            self.selected["review"], self.profiles["review"] = chosen[0], copy.deepcopy(chosen[1])
        else:
            self.reviewer = "planner"
        self.sync_reviewer()
        self.sessions = {}
        self.busy = False
        self.active = None
        self.child = None
        self.turn_text = ""
        self.last_answer = ""
        self.picking = None
        self.queued = []
        self.always_allow = set()
        mode = self.config.get("permission_mode")
        self.permission = mode if mode in PERMISSION_MODES else "ask"
        self.previous_permission = "ask"
        self.activity = ""
        self.running = None  # (role, alias, profile) of the model doing the current turn
        self.started = time.monotonic()
        self.detail = ""
        self.quotas = {}  # usage_key -> [{"window", "percent", "resets_at"}]; [] means no plan limits apply
        self.session_profiles = {}

    def compose(self):
        yield RichLog(id="transcript", wrap=True, highlight=False, markup=False, auto_scroll=True)
        yield Static(id="response")
        yield Static(id="thinking")
        yield Menu(id="menu")
        yield Draft(
            id="draft",
            soft_wrap=True,
            show_line_numbers=False,
            placeholder=MODE_PLACEHOLDERS["plan"],
        )
        yield Static(id="status")
        yield Static(id="usage")
        yield Footer()

    def on_mount(self):
        self.welcome()
        self.status("Ready")
        if not self.config.get("aliases"):
            self.say(Text("No accounts configured. Exit and run donut setup.", style=CORAL))
        self.query_one(Draft).focus()
        self.set_interval(0.1, self.tick)
        if self.AUTO_REFRESH_QUOTAS:
            self.refresh_quotas_soon(connect=True)
            self.set_interval(self.QUOTA_REFRESH_SECONDS, lambda: self.refresh_quotas_soon(connect=True))

    def refresh_quotas_soon(self, connect=False):
        self.run_worker(self.refresh_quotas(connect), group="quota", exclusive=True)

    async def refresh_quotas(self, connect=False):
        """Ask each account how much of its plan is left, without sending any prompt.

        connect=True starts a session for an account that has none yet; otherwise only open sessions are asked.
        """
        for key, (label, alias, profile) in self.usage_groups().items():
            if key[0] == "local":
                continue
            session = next(
                (s for (a, m), s in self.sessions.items() if usage_key(self.session_profiles.get((a, m), {})) == key),
                None,
            )
            try:
                if session is None and connect:
                    session = await self.session(alias, profile)
                if session is None:
                    continue
                windows = await session.quota()
            except Exception:
                continue  # Not logged in, offline, or the provider is not installed: the bar stays as it was.
            if windows is not None:
                self.quotas[key] = windows
                try:
                    self.status()
                except NoMatches:
                    return

    def on_resize(self, event):
        self.call_after_refresh(self.status)  # Re-pack the status line for the new width.

    def tick(self):
        if self.busy:
            try:
                self.render_thinking()
            except NoMatches:
                pass  # The timer can fire once more while the app shuts down.

    @property
    def role(self):
        """The model role the current mode talks to; each mode has its own."""
        return self.mode

    @property
    def purpose(self):
        """Solo and review keep conversations of their own, apart from the planner's, even on the same model."""
        return self.mode if self.mode in ("solo", "review") else None

    def session_key(self, alias, profile, purpose=None):
        return (f"{alias}@{purpose}" if purpose else alias, profile.get("model"))

    def set_mode(self, mode):
        self.mode = mode
        try:
            self.query_one(Draft).placeholder = MODE_PLACEHOLDERS[mode]
        except NoMatches:
            pass
        self.status()

    def sync_reviewer(self):
        """Follow the planner while the reviewer is the default; a chosen reviewer never changes with it."""
        if self.reviewer == "off":
            self.selected["review"], self.profiles["review"] = None, {}
        elif self.reviewer == "planner":
            self.selected["review"] = self.selected.get("plan")
            self.profiles["review"] = copy.deepcopy(self.profiles.get("plan", {}))

    def start_turn(self, role, detail="", activity="thinking"):
        """Mark which model is now working, for the spinner and the status line."""
        alias = self.selected.get(role)
        self.running = (role, alias, self.profiles.get(role, {}))
        self.detail = detail
        self.activity = activity
        self.started = time.monotonic()
        self.status()

    def render_thinking(self):
        widget = self.query_one("#thinking", Static)
        widget.set_class(self.busy, "-busy")
        if not self.busy:
            return
        elapsed = int(time.monotonic() - self.started)
        frame = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
        line = Text(frame + " ", style=f"bold {ORANGE}")
        if self.running:
            role, alias, profile = self.running
            line.append(model_name(profile, alias), style=f"bold {ORANGE}")
            line.append(f"  {ROLE_TITLES.get(role, role)}", style=DIM)
            line.append(f"  is {self.activity or 'thinking'}", style=GRAY)
            if self.detail:
                detail = self.detail if len(self.detail) <= 40 else self.detail[:39] + "…"
                line.append(" · " + detail, style=TEAL)
        else:
            line.append(self.activity or "working", style=GRAY)
        line.append(f" · {elapsed}s", style=CORAL)
        line.append("   ctrl+c to cancel", style=DIM)
        widget.update(line)

    def welcome(self):
        """The logo and a short orientation, written at the top of the transcript when Donut opens."""
        for runs in logo_segments():
            line = Text()
            for text, color in runs:
                line.append(text, style=hex_color(color) if color else None)
            self.say(line)
        title = Text("  DONUT", style=f"bold {ORANGE}")
        title.append("  /  " + release_version(), style=CORAL)
        title.append("   " + str(self.project), style=DIM)
        self.say(title)
        self.say(Text("  The planner writes the tickets. The worker takes them one at a time.", style=GRAY))
        hint = Text("  ")
        for key, description in (
            ("/plan", "tickets"),
            ("/work", "build"),
            ("/review", "check"),
            ("/solo", "direct"),
            ("/", "commands"),
            ("Shift+Enter", "newline"),
        ):
            hint.append(key, style=f"bold {ORANGE}")
            hint.append(" " + description + "   ", style=DIM)
        self.say(hint)
        self.say("")

    def status(self, activity=None):
        """Redraw the spinner, the role line and the usage bar. activity is what the running model is doing."""
        if activity is not None and activity not in ("Ready", "Thinking…"):
            self.activity = activity
        try:
            self.render_thinking()
            self.render_usage()
        except NoMatches:
            return
        # One entry per piece: the mode badge, every role and what it is set to (in every mode), then the
        # permission mode. The current mode's role is brighter and the role doing a turn gets a filled dot.
        entries = [
            Text(
                f" {self.mode.upper()} ",
                style=f"bold {'#ffffff' if self.mode == 'review' else '#101820'} on {MODE_BADGES[self.mode]}",
            )
        ]
        running_role = self.running[0] if self.busy and self.running else None
        for role, title in ROLE_TITLES.items():
            alias, profile = self.selected.get(role), self.profiles.get(role, {})
            if not profile:
                value = "off" if role == "review" else "not set"
            elif role == "review" and self.reviewer == "planner":
                value = f"{model_name(profile, alias)} (= planner)"
            else:
                value = model_name(profile, alias)
            entry = Text()
            if role == running_role:
                entry.append("● ", style=f"bold {ORANGE}")
                entry.append(title + " ", style=f"bold {ORANGE}")
                entry.append(value, style=f"bold {ORANGE}")
            else:
                focus = not running_role and role == self.role
                entry.append("○ ", style=f"bold {ORANGE}" if focus else DIM)
                entry.append(title + " ", style=GRAY if focus else DIM)
                entry.append(value, style=f"bold {GRAY}" if focus and profile else (GRAY if profile else DIM))
            entries.append(entry)
        label = PERMISSION_MODES[self.permission][0].lower()
        permission = Text()
        if self.permission == "ask":
            permission.append(f"⏵ {label}", style=DIM)
        else:
            permission.append(f"⏵⏵ {label} on", style=f"bold {CORAL if self.permission == 'full' else ORANGE}")
        permission.append("  (shift+tab)", style=DIM)
        if self.queued:
            permission.append(f"   {len(self.queued)} queued", style=CORAL)
        entries.append(permission)
        widget = self.query_one("#status", Static)
        widget.update(pack_entries(entries, widget.content_size.width or self.size.width - 4))

    def usage_groups(self):
        """One entry per quota: every configured Claude login and Codex, plus the local models in use.

        Maps usage_key -> (label, alias, profile).
        """
        groups = {}
        selected = {self.selected.get(role) for role in ("plan", "work")}
        for alias, profile in self.config.get("aliases", {}).items():
            key = usage_key(profile)
            if key[0] == "local" and alias not in selected:
                continue
            label = alias if key[0] != "local" else model_name(profile, alias).split(":")[0]
            if key not in groups or alias in selected:
                groups[key] = (label, alias, profile)
        for role in ("plan", "work", "review", "solo"):  # Models picked by hand may not be saved aliases.
            profile, alias = self.profiles.get(role, {}), self.selected.get(role)
            if profile and usage_key(profile) not in groups:
                key = usage_key(profile)
                label = alias if key[0] != "local" else model_name(profile, "").split(":")[0]
                groups[key] = (label, alias, profile)
        return groups

    def render_usage(self):
        line = Text()
        running = usage_key(self.running[2]) if self.busy and self.running else None
        for key, (label, _, _) in self.usage_groups().items():
            if line:
                line.append("   ")
            line.append(label, style=f"bold {ORANGE}" if key == running else GRAY)
            line.append(" ")
            if key[0] == "local":
                line.append("local", style=DIM)
                continue
            windows = self.quotas.get(key)
            if windows is None:
                line.append("──────", style="#3b4652")
                line.append(" –", style=DIM)
                continue
            if not windows:
                line.append("no plan limit", style=DIM)
                continue
            first, *rest = windows
            bar, color = usage_bar(first["percent"])
            line.append_text(bar)
            line.append(f" {remaining(first['percent'])}% left", style=color)
            if first["window"]:
                line.append(" " + first["window"], style=DIM)
            reset = countdown(first.get("resets_at"))
            if reset:
                line.append(f" ↻{reset}", style=DIM)
            for window in rest[:1]:
                left = remaining(window["percent"])
                line.append(" · ", style=DIM)
                line.append(f"{left}%", style=left_color(left))
                if window["window"]:
                    line.append(" " + window["window"], style=DIM)
        self.query_one("#usage", Static).update(line)

    def say(self, text, style=None):
        self.query_one(RichLog).write(Text(text, style=style) if style and isinstance(text, str) else text)

    async def choose(self, title, rows, kind="pick", details=None, subtitle=""):
        """Show rows in the inline menu and wait for a choice. Returns the chosen value, or None.

        A "pick" uses the prompt as its filter, so the prompt is cleared around it. An "approve" leaves
        whatever the user is drafting alone.
        """
        if not rows:
            return None
        menu = self.query_one(Menu)
        future = asyncio.get_running_loop().create_future()
        self.picking = future
        if kind == "pick":
            self.query_one(Draft).clear()
        menu.open(kind, title, rows, details=details, subtitle=subtitle)
        try:
            return await future
        finally:
            self.picking = None
            menu.close()
            if kind == "pick":
                self.query_one(Draft).clear()

    def slash_rows(self):
        rows = [(name, name, description) for name, description in COMMANDS]
        session = self.sessions.get(self.session_key(self.selected[self.role], self.profiles[self.role], self.purpose))
        if isinstance(session, ClaudeSession):
            for command in session.info.get("commands", []):
                name = command.get("name") if isinstance(command, dict) else command
                if isinstance(name, str) and "/" + name.lstrip("/") not in {row[0] for row in rows}:
                    description = command.get("description", "") if isinstance(command, dict) else ""
                    rows.append(("/" + name.lstrip("/"), "/" + name.lstrip("/"), (description or "")[:80]))
        return rows

    def on_menu_chosen(self, event):
        menu = self.query_one(Menu)
        draft = self.query_one(Draft)
        if menu.kind in ("pick", "approve"):
            if self.picking and not self.picking.done():
                self.picking.set_result(event.row[1] if event.row else None)
            return
        menu.close()
        if event.row is None:
            return
        command = event.row[1]
        if event.key == "tab":
            draft.load_text(command + " ")
            draft.move_cursor(draft.document.end)
            return
        draft.clear()
        self.execute(command)

    def event(self, kind, text, key=None):
        if kind == "text":
            from rich.markdown import Markdown as RenderedMarkdown

            self.turn_text += text
            self.activity = "writing"
            self.query_one("#response", Static).update(RenderedMarkdown(self.turn_text))
        elif kind == "error":
            self.say("Error: " + str(text), CORAL)
        elif kind == "usage":
            if key is not None and isinstance(text, list):
                self.quotas[key] = text
                self.status()
        else:
            name = str(text)
            if name.startswith("mcp__"):
                name = "using " + name[5:].replace("__", " · ")
            self.status(ACTIVITY_NAMES.get(name, name if name.startswith("using ") else "using " + name))

    async def approve(self, name, details):
        """Ask about one tool call. Returns "yes", "always" (for the rest of this session) or "no"."""
        details = details if isinstance(details, dict) else {"input": details}
        title, short = approval_title(name, details)
        if title in self.always_allow:
            return "always"
        rows = [
            ("Yes", "yes"),
            (f"Yes, and don't ask again for {short} this session", "always"),
            ("No", "no"),
        ]
        decision = await self.choose(
            title, rows, kind="approve", details=approval_details(details), subtitle="Enter to choose · Esc to deny"
        )
        decision = decision or "no"
        if decision == "always":
            self.always_allow.add(title)
        summary = approval_summary(details)
        line = Text("  ✓ " if decision != "no" else "  ✗ ", style=TEAL if decision != "no" else CORAL)
        line.append(("Allowed " if decision != "no" else "Denied ") + title, style=GRAY)
        if summary:
            line.append("  " + summary, style=DIM)
        self.say(line)
        return decision

    async def set_permission(self, mode, announce=True):
        """Apply a permission mode to every open session through the provider's own mechanism, and remember it."""
        if mode != self.permission:
            self.previous_permission = self.permission
        self.permission = mode
        self.status("Ready" if not self.busy else "Thinking…")
        try:
            config = load()
            config["permission_mode"] = mode
            save(config)
            self.config["permission_mode"] = mode
        except (ValueError, OSError) as error:
            self.say(f"Permission mode applies to this session only: {error}", DIM)
        for session in list(self.sessions.values()):
            try:
                await session.set_permission(mode)
            except Exception as error:
                self.say(f"Could not change the provider's permission mode: {error}", CORAL)
        if announce:
            label, description = PERMISSION_MODES[mode]
            line = Text(f"  {label} mode", style=f"bold {ORANGE if mode != 'full' else CORAL}")
            line.append("  " + description + ".", style=GRAY)
            engine = self.profiles[self.role].get("engine")
            if mode == "ask" and engine in ("ollama", "openai"):
                line.append(" Qwen can't show approval prompts here, so it edits files without asking.", style=DIM)
            self.say(line)

    def action_cycle_permission(self):
        modes = PERMISSION_CYCLE
        following = modes[(modes.index(self.permission) + 1) % len(modes)] if self.permission in modes else "ask"
        self.run_worker(self.set_permission(following), group="permission")

    async def session(self, alias, profile, purpose=None, resume=None, conversation=None):
        """The persistent provider session for a model and purpose. Review-mode sessions are read-only.

        resume reopens a provider session or thread by its id (from /resume), replacing any open one.
        """
        key = self.session_key(alias, profile, purpose)
        if conversation is not None and key in self.sessions:
            await self.sessions.pop(key).close()
        if key not in self.sessions:
            engine = profile.get("engine")
            cls = (
                ClaudeSession
                if engine == "claude"
                else CodexSession
                if engine == "codex"
                else QwenSession
                if engine in ("ollama", "openai")
                else None
            )
            if not cls:
                raise ValueError(
                    "Persistent sessions for this engine are not implemented yet. Use /native for its full interactive client."
                )
            emit = lambda kind, data, key=usage_key(profile): self.event(kind, data, key)  # noqa: E731
            session = cls(self.project, profile, emit, self.approve)
            session.permission = self.permission
            session.read_only = purpose == "review"
            session.resume_id = resume
            session.conversation_id = conversation
            try:
                await session.connect()
            except BaseException:
                if hasattr(session, "process"):
                    await session.close()
                raise
            self.sessions[key] = session
            self.session_profiles[key] = profile
        return self.sessions[key]

    def on_text_area_changed(self, event):
        menu = self.query_one(Menu)
        text = event.text_area.text
        if menu.kind == "pick":
            menu.filter(text)
        elif menu.kind == "approve":
            return
        elif text.startswith("/") and not any(c.isspace() for c in text) and not self.busy:
            if not menu.is_open:
                menu.open("slash", "Commands", self.slash_rows())
            if not menu.filter(text):
                menu.close()
        elif menu.is_open:
            menu.close()

    def action_send(self):
        if len(self.screen_stack) > 1:
            return
        editor = self.query_one(Draft)
        text = editor.text.strip()
        if not text:
            return
        editor.clear()
        if self.busy:
            # Like Claude Code: a message sent during a turn waits and runs as soon as the turn ends.
            self.queued.append(text)
            self.say(Text("  queued · " + text.splitlines()[0][:100], style=DIM))
            self.status("Thinking…")
            return
        self.execute(text)

    def on_draft_submitted(self, event):
        self.action_send()

    def action_models(self):
        if not self.busy:
            self.execute("/model")

    def action_modes(self):
        """F3 cycles plan, work, review and solo."""
        if not self.busy:
            self.execute("/" + {"plan": "work", "work": "review", "review": "solo", "solo": "plan"}[self.mode])

    def action_clear(self):
        self.query_one(RichLog).clear()

    async def action_cancel(self):
        menu = self.query_one(Menu)
        if self.picking and not self.picking.done():
            self.picking.set_result(None)
        elif menu.is_open:
            menu.close()
        if self.queued:
            # Put queued messages back in the prompt so nothing typed is lost.
            draft = self.query_one(Draft)
            draft.load_text("\n".join(self.queued + ([draft.text] if draft.text.strip() else [])))
            draft.move_cursor(draft.document.end)
            self.queued.clear()
        if self.active:
            await self.active.interrupt()
        if self.child and self.child.returncode is None:
            if os.name == "nt":
                proc = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(self.child.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL
                )
                await proc.wait()
            else:
                os.killpg(self.child.pid, signal.SIGTERM)

    async def action_quit(self):
        await self.action_cancel()
        for session in self.sessions.values():
            await session.close()
        self.exit()

    @work(exclusive=True)
    async def execute(self, text):
        if not self.selected[self.role] and not text.startswith(("/help", "/exit", "/quit", "/permissions", "/auto")):
            self.say("Run donut setup to configure an account first.")
            return
        self.busy = True
        self.running = None
        self.activity = "working"
        self.started = time.monotonic()
        self.status()
        try:
            if text.startswith("/"):
                await self.command(text)
            elif self.mode == "solo":
                await self.solo(text)
            elif self.mode == "review":
                await self.review_chat(text)
            else:
                if self.mode == "work":
                    # Conversation goes to the planner (or the worker in /solo); /work only runs tickets.
                    self.set_mode("plan")
                    self.say("Switched to plan mode for this message.", DIM)
                await self.plan(text)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.say("Error: " + str(error), CORAL)
        finally:
            self.active = None
            self.busy = False
            self.running = None
            if self.AUTO_REFRESH_QUOTAS:
                self.refresh_quotas_soon()
            try:
                self.status("Ready")
                self.query_one(Draft).focus()
            except NoMatches:
                return  # The app is shutting down.
            if self.queued:
                self.call_after_refresh(self.execute, self.queued.pop(0))

    async def command(self, text):
        words = shlex.split(text)
        name, args = words[0], words[1:]
        if name in ("/exit", "/quit"):
            await self.action_quit()
        elif name in ("/planner", "/worker", "/soloist"):
            role = {"/planner": "plan", "/worker": "work", "/soloist": "solo"}[name]
            await self.choose_model(role, args[0] if args else None)
        elif name == "/reviewer":
            await self.choose_reviewer(args[0] if args else None)
        elif name == "/solo":
            if not await self.ensure_role("solo"):
                return
            self.set_mode("solo")
            line = Text("  Solo mode", style=f"bold {TEAL}")
            line.append(f" · {model_name(self.profiles['solo'], self.selected['solo'])}", style=f"bold {ORANGE}")
            self.say(line)
            self.say(
                "  Messages go straight to the soloist, like Claude Code: no planner, tickets or review. "
                "/soloist changes its model; /plan goes back.",
                DIM,
            )
        elif name == "/review":
            if not await self.ensure_role("review"):
                return
            self.set_mode("review")
            line = Text("  Review mode", style=f"bold {ORANGE}")
            line.append(f" · {model_name(self.profiles['review'], self.selected['review'])}", style=f"bold {ORANGE}")
            self.say(line)
            self.say("  Ask the reviewer to check code or changes. It reads the project and never edits it.", DIM)
        elif name == "/plan":
            if not await self.ensure_role("plan"):
                return
            self.set_mode("plan")
            self.say("Planning mode. Describe what to plan.", ORANGE)
        elif name == "/work":
            if not await self.ensure_role("work"):
                return
            self.set_mode("work")
            if self.mode == "work":
                choice = (
                    " ".join(args)
                    if args
                    else await self.choose(
                        "Work tickets",
                        [
                            (label, value, description, "/work " + value)
                            for label, value, description in (
                                ("Next ticket", "next 1", "one ticket in a fresh context"),
                                ("Next 3 tickets", "next 3", "up to three, one fresh context each"),
                                ("All eligible tickets", "next all", "until the queue is empty or blocked"),
                                ("Next ticket + checkpoint", "next 1 checkpoint", "git commit after it"),
                                ("All tickets + checkpoints", "next all checkpoint", "git commit after each"),
                            )
                        ],
                        subtitle="or type /work next N for any number",
                    )
                )
                if choice:
                    await self.work_tickets(choice)
                # Back to planning, so the next message is a follow-up rather than refused.
                self.set_mode("plan")
        elif name == "/resume":
            await self.resume_conversation(args[0] if args else None)
        elif name == "/model":
            if self.role == "review":
                await self.choose_reviewer()
            else:
                await self.choose_model(self.role)
        elif name == "/permissions":
            rows = [(label, mode, description) for mode, (label, description) in PERMISSION_MODES.items()]
            if args and args[0] in PERMISSION_MODES:
                chosen = args[0]
            else:
                current = PERMISSION_MODES[self.permission][0]
                chosen = await self.choose(f"Permission mode · now {current}", rows)
            if chosen:
                await self.set_permission(chosen)
        elif name == "/auto":
            if self.permission == "auto":
                fallback = self.previous_permission if self.previous_permission != "auto" else "ask"
                await self.set_permission(fallback)
            else:
                await self.set_permission("auto")
        elif name == "/usage":
            await self.usage()
        elif name == "/clear":
            session = self.sessions.pop(
                self.session_key(self.selected[self.role], self.profiles[self.role], self.purpose), None
            )
            if session:
                await session.close()
            self.say("New conversation. Existing tickets are preserved.")
        elif name == "/copy":
            self.copy_to_clipboard(self.last_answer)
            self.notify("Copied last response (terminal clipboard support required).")
        elif name == "/help":
            self.say(
                "/plan · /work [next COUNT] [checkpoint] · /review · /solo · /resume · /planner · /worker · /reviewer [ALIAS|planner|off] · /soloist · /model · /permissions [ask|accept_edits|auto|full] · /auto · /usage · /plugin · /mcp · /context · /clear · /copy · /native · /exit\nShift+Tab cycles Ask, Accept edits and Auto. Messages sent during a turn are queued. Provider plugin commands are forwarded to the selected session. Use /native for built-ins requiring the provider’s own UI."
            )
        elif name in ("/plugin", "/mcp", "/context"):
            session = await self.session(self.selected[self.role], self.profiles[self.role], self.purpose)
            provider = self.selected[self.role] or "provider"
            model = self.profiles[self.role].get("model") or ""
            if isinstance(session, ClaudeSession):
                if name == "/plugin":
                    data = session.info.get("commands", [])
                elif name == "/mcp":
                    for line in render_mcp(mcp_servers(await session.client.get_mcp_status()), provider):
                        self.say(line)
                    return
                else:
                    for line in claude_context(await session.client.get_context_usage()):
                        self.say(line)
                    return
            elif isinstance(session, CodexSession):
                if name == "/mcp":
                    for line in render_mcp(mcp_servers(await session.request("mcpServerStatus/list", {})), provider):
                        self.say(line)
                    return
                if name == "/context":
                    for line in codex_context(getattr(session, "usage", None), model):
                        self.say(line)
                    return
                data = await session.request("plugin/list", {})
            else:
                self.say("Use /native for Qwen plugins, MCP and context controls.")
                return
            if name == "/plugin":
                if isinstance(session, ClaudeSession):
                    rows = [
                        (
                            item.get("name", "?") + " · " + (item.get("description") or "")[:100],
                            "/" + item.get("name", ""),
                        )
                        for item in data
                        if isinstance(item, dict)
                    ]
                    selected = await self.choose("Claude plugin commands · /native for management", rows)
                    if selected:
                        self.query_one(TextArea).load_text(selected + " ")
                else:
                    rows = []
                    for market in data.get("marketplaces", []):
                        for plugin in market.get("plugins", []):
                            state = "installed" if plugin.get("installed") else "not installed"
                            description = (plugin.get("description") or "")[:70]
                            rows.append((plugin.get("name", "?"), plugin, f"{state} · {description}".strip(" ·")))
                    selected = await self.choose("Codex plugins · /native for management", rows)
                    if selected:
                        line = Text("  " + selected.get("name", "?"), style=f"bold {ORANGE}")
                        line.append("  installed" if selected.get("installed") else "  not installed", style=GRAY)
                        if selected.get("description"):
                            line.append("  " + selected["description"], style=DIM)
                        self.say(line)
                        self.say("  Install or remove plugins with /native.", DIM)
        elif name == "/native":
            await self.native()
        else:
            self.active = await self.session(self.selected[self.role], self.profiles[self.role], self.purpose)
            self.start_turn(self.role)
            self.turn_text = ""
            await self.active.query(text)
            self.answer()

    def answer(self):
        self.query_one("#response", Static).update("")
        self.last_answer = self.turn_text
        from rich.markdown import Markdown as RenderedMarkdown

        self.say(RenderedMarkdown(self.turn_text))

    async def solo(self, text):
        """Send the message to the soloist as is, in its own conversation, the way Claude Code would."""
        self.active = await self.session(self.selected["solo"], self.profiles["solo"], "solo")
        self.start_turn("solo")
        self.say(Text("> " + text, style=f"bold {TEAL}"))
        self.turn_text = ""
        await self.active.query(text)
        self.answer()
        self.log_turn("solo", "solo", text)

    async def review_chat(self, text):
        """Ask the reviewer to check something, in a read-only conversation of its own."""
        self.active = await self.session(self.selected["review"], self.profiles["review"], "review")
        self.start_turn("review", "", "reviewing")
        self.say(Text("> " + text, style=f"bold {ORANGE}"))
        self.turn_text = ""
        await self.active.query(REVIEW_MODE_PROMPT + text)
        self.answer()
        self.log_turn("review", "review", text)

    def log_turn(self, mode, role, text):
        """Save this exchange so /resume can reopen the conversation in the same mode and model."""
        session = self.active
        if session is None:
            return
        try:
            session.conversation_id = conversations.record_turn(
                getattr(session, "conversation_id", None),
                self.project,
                mode,
                role,
                self.selected.get(role),
                self.profiles.get(role, {}).get("model"),
                getattr(session, "provider_id", None),
                text,
                self.turn_text,
            )
        except OSError as error:
            self.say(f"Conversation not saved for /resume: {error}", DIM)

    async def resume_conversation(self, record_id=None):
        """Reopen an earlier conversation: switch to its mode and model, then continue the provider's session."""
        records = conversations.for_project(self.project)
        if not records:
            self.say("No earlier conversations in this project yet.", DIM)
            return
        if record_id is None:
            rows = [
                (
                    record.get("title", "(untitled)")[:56],
                    record["id"],
                    f"{record.get('mode', '?')} · {record.get('model') or record.get('alias')} · "
                    f"{conversations.ago(record.get('updated'))} · {record.get('turns', 0)} turn"
                    f"{'' if record.get('turns') == 1 else 's'}",
                )
                for record in records[:40]
            ]
            record_id = await self.choose("Resume a conversation", rows)
            if not record_id:
                return
        record = next((r for r in records if r["id"] == record_id), None)
        if record is None:
            raise ValueError("That conversation is not in this project's history.")
        aliases = self.config.get("aliases", {})
        alias, role, mode = record.get("alias"), record.get("role"), record.get("mode")
        if alias not in aliases:
            raise ValueError(f"The account {alias} used by that conversation is no longer configured.")
        if mode not in MODE_PLACEHOLDERS or role not in ROLE_TITLES:
            raise ValueError("That conversation was saved in a mode this version cannot resume.")
        profile = dict(aliases[alias])
        if record.get("model"):
            profile["model"] = record["model"]
        # Match the conversation: its role's model, then its mode.
        self.selected[role], self.profiles[role] = alias, copy.deepcopy(profile)
        if role == "review":
            self.reviewer = "chosen"
        try:
            remember_role(role, alias, profile.get("model"))
        except (ValueError, OSError):
            pass
        self.sync_reviewer()
        self.set_mode(mode)
        purpose = mode if mode in ("solo", "review") else None
        self.status("resuming")
        await self.session(alias, profile, purpose, resume=record.get("provider_id"), conversation=record["id"])
        line = Text("  Resumed", style=f"bold {ORANGE}")
        line.append(f" · {mode.upper()} · {model_name(profile, alias)}", style=f"bold {TEAL}")
        line.append(f" · {record.get('title', '')}", style=GRAY)
        self.say(line)
        if not record.get("provider_id"):
            self.say("  The provider kept no session for it, so this continues in a fresh one.", DIM)
        for message in record.get("messages", [])[-6:]:
            text = " ".join(message.get("text", "").split())
            if message.get("from") == "you":
                self.say(Text("  > " + text[:200], style=ORANGE))
            else:
                self.say(Text("    " + text[:300] + ("…" if len(text) > 300 else ""), style=DIM))
        self.say("  Continue where you left off.", DIM)

    async def ensure_role(self, role):
        """Entering a mode asks for its model only when none is set. Returns whether one is set now."""
        if self.profiles.get(role):
            return True
        if role == "review":
            await self.choose_reviewer(allow_off=False)
        else:
            await self.choose_model(role)
        if self.profiles.get(role):
            return True
        self.say(f"No {ROLE_TITLES[role]} chosen; pick one with {ROLE_COMMANDS[role]}.", DIM)
        return False

    async def plan(self, text):
        # Install outside the display stream; existing project instructions are preserved.
        await asyncio.to_thread(install, self.project, Path.home(), quiet=True)
        before = ticket_snapshot(self.project)
        self.active = await self.session(self.selected["plan"], self.profiles["plan"])
        self.start_turn("plan")
        prompt = (
            "Read skills/donut-planner/SKILL.md. Plan only: research and write scoped tickets; do not implement or commit. Respect on_hold and blocked lanes and reserve their identities. Give a brief overview when finished; Donut lists created tickets. User request:\n"
            + text
        )
        self.say(Text("> " + text, style=f"bold {ORANGE}"))
        self.turn_text = ""
        await self.active.query(prompt)
        self.answer()
        self.log_turn("plan", "plan", text)
        created = [path for key, path in ticket_snapshot(self.project).items() if key not in before]
        self.say("Tickets created: " + str(len(created)), CORAL)
        for path in sorted(created):
            self.say("  " + path.relative_to(self.project).as_posix(), GRAY)
        self.say("PLANNING COMPLETE · /work next 1", f"bold {ORANGE}")

    async def choose_reviewer(self, choice=None, allow_off=True):
        """Pick who reviews each finished ticket: the planner (default), any account and model, or off.

        The choice is saved, and it never changes the planner (nor does /model change a chosen reviewer).
        """
        aliases = self.config.get("aliases", {})
        if choice is None:
            planner = model_name(self.profiles.get("plan", {}), self.selected.get("plan"))
            rows = [("Same as planner (default)", "planner", f"follows the planner · now {planner}")]
            rows += await self.model_rows()
            if allow_off:
                rows.append(("Off", "off", "no review; the worker's result stands"))
            choice = await self.choose("Reviewer · checks each ticket and answers in review mode", rows)
            if not choice:
                return
        elif choice not in ("planner", "off"):
            if choice not in aliases:
                raise ValueError("Use /reviewer planner, /reviewer off, or /reviewer ALIAS with a configured alias.")
            choice = (choice, dict(aliases[choice]))
        try:
            if choice in ("planner", "off"):
                self.reviewer = choice
                forget_role("review", reviewer=choice)
            else:
                self.reviewer = "chosen"
                self.selected["review"], self.profiles["review"] = choice[0], copy.deepcopy(choice[1])
                remember_role("review", choice[0], choice[1].get("model"))
        except (ValueError, OSError) as error:
            self.say(f"Reviewer applies to this session only: {error}", DIM)
        self.sync_reviewer()
        if self.reviewer == "off":
            self.say("Review off. Finished tickets are not reviewed.", ORANGE)
        else:
            who = model_name(self.profiles["review"], self.selected["review"])
            self.say(Text(f"  Reviewer: {who}", style=f"bold {ORANGE}"))
            self.say("  It checks each finished ticket in a fresh context and sends issues back to the worker.", DIM)
        self.status()

    async def choose_model(self, role, alias=None):
        """Pick the account and model for one role (planner, worker or soloist). Only that role changes."""
        aliases = self.config.get("aliases", {})
        if alias is not None:
            if alias not in aliases:
                raise ValueError(
                    f"Use {ROLE_COMMANDS[role]} ALIAS with a configured alias, or {ROLE_COMMANDS[role]} alone to pick."
                )
            selected = (alias, dict(aliases[alias]))
        else:
            rows = await self.model_rows()
            selected = await self.choose("Account and model for the " + ROLE_TITLES[role], rows) if rows else None
        if not selected:
            return
        self.selected[role], self.profiles[role] = selected[0], copy.deepcopy(selected[1])
        try:
            remember_role(role, selected[0], selected[1].get("model"))
        except (ValueError, OSError) as error:
            self.say(f"Model choice applies to this session only: {error}", DIM)
        line = Text(f"  {ROLE_TITLES[role].capitalize()}: ", style=GRAY)
        line.append(model_name(self.profiles[role], self.selected[role]), style=f"bold {ORANGE}")
        self.say(line)
        if role == "plan" and self.reviewer == "planner":
            self.say("  The reviewer follows the planner; choose another with /reviewer.", DIM)
        self.sync_reviewer()
        self.status()

    async def model_rows(self):
        """(label, (alias, profile)) for every model each configured account offers."""
        rows = []
        for alias, profile in self.config["aliases"].items():
            self.status("discovering models for " + alias)
            try:
                if profile["engine"] in ("claude", "codex"):
                    session = await self.session(alias, profile)
                    models = await session.models()
                elif profile["engine"] == "ollama":
                    proc = await asyncio.create_subprocess_exec("ollama", "list", stdout=asyncio.subprocess.PIPE)
                    output, _ = await proc.communicate()
                    if proc.returncode:
                        raise RuntimeError("Ollama model discovery failed")
                    models = [
                        {"id": line.split()[0], "name": line.split()[0]}
                        for line in output.decode().splitlines()[1:]
                        if line.split()
                    ]
                else:
                    from .catalog import endpoint_models

                    models = await asyncio.to_thread(endpoint_models, profile)
                for model in models:
                    rows.append((alias + "  " + model["name"], (alias, dict(profile, model=model["id"]))))
            except Exception as error:
                self.say(f"{alias}: model discovery failed: {error}")
        return rows

    async def usage(self):
        """How much of each plan is left, as the usage bar shows it but with every window and reset time."""
        self.status("checking usage")
        await self.refresh_quotas(connect=True)
        for key, (label, _, _) in self.usage_groups().items():
            line = Text("  " + label, style=f"bold {ORANGE}")
            windows = self.quotas.get(key)
            if key[0] == "local":
                line.append("   local model, no plan limits", style=DIM)
            elif windows is None:
                line.append("   usage unavailable (not signed in, or the provider did not report it)", style=DIM)
            elif not windows:
                line.append("   no plan limits (API billing)", style=DIM)
            self.say(line)
            for window in windows or []:
                left = remaining(window["percent"])
                row = Text("    ")
                row.append_text(usage_bar(window["percent"], width=20)[0])
                row.append(f"  {left}% left", style=left_color(left))
                row.append(f"  {window['window'] or 'window'}", style=GRAY)
                reset = countdown(window.get("resets_at"))
                if reset:
                    row.append(f"  resets in {reset}", style=DIM)
                self.say(row)

    async def work_tickets(self, choice):
        words = choice.split()
        checkpoint = words[-1:] == ["checkpoint"]
        if checkpoint:
            words = words[:-1]
        if (
            len(words) != 2
            or words[0] != "next"
            or not (words[1] == "all" or words[1].isdecimal() and int(words[1]) > 0)
        ):
            raise ValueError("Use /work next COUNT [checkpoint] (COUNT is a positive integer or all).")
        if checkpoint:
            words.append("--checkpoint")
        alias = self.selected["work"]
        if not alias:
            raise ValueError("Run donut setup to configure a worker.")
        with tempfile.TemporaryDirectory(prefix="donut-worker-") as temp:
            config = copy.deepcopy(self.config)
            config["aliases"][alias] = dict(self.profiles["work"], permission_mode=self.permission)
            if self.profiles.get("review"):
                # A dedicated alias, so a reviewer on the same account as the worker keeps its own model.
                config["aliases"]["donut-reviewer"] = copy.deepcopy(self.profiles["review"])
                config["reviewer"] = "donut-reviewer"
            else:
                config["reviewer"] = "off"
            if self.profiles.get("plan"):
                # Blocked or stuck tickets go back to the planner, with the same permission mode as the worker.
                config["aliases"]["donut-planner"] = dict(self.profiles["plan"], permission_mode=self.permission)
                config["recovery"] = "donut-planner"
            folder = Path(temp) / "donut"
            folder.mkdir()
            path = folder / "config.json"
            path.write_text(json.dumps(config))
            path.chmod(0o600)
            env = dict(os.environ, XDG_CONFIG_HOME=temp, PYTHONIOENCODING="utf-8", NO_COLOR="1")
            self.child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "donut",
                "-p",
                str(self.project),
                "-m",
                alias,
                "--work",
                *words,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **process_options(),
            )
            highlighter, ticket = Highlighter(), ""
            self.start_turn("work", "", "starting")
            async for raw in self.child.stdout:
                # Keep only what a terminal would finally show on this line: no escapes, no \r redraws.
                line = ANSI_ESCAPE.sub("", raw.decode("utf-8", errors="replace").rstrip("\r\n")).split("\r")[-1]
                line = line.rstrip()
                if line and all("\u2800" <= c <= "\u28ff" or c.isspace() for c in line):
                    continue  # Stray spinner frames.
                progress = RUNNER_PROGRESS.match(line)
                if progress:
                    continue  # The spinner line already shows the worker and its elapsed time.
                started = RUNNER_TICKET.match(line)
                if started:
                    ticket = started["ticket"]
                    self.start_turn("work", ticket, "working")
                review = RUNNER_REVIEW.match(line)
                if review:
                    self.start_turn("review", review["ticket"], "reviewing")
                if line.startswith("Review: changes requested"):
                    self.start_turn("work", ticket, "addressing review feedback")
                recovering = RUNNER_RECOVER.match(line)
                if recovering:
                    self.start_turn("plan", recovering["ticket"], "recovering a blocked ticket")
                if line.startswith("[repair queue]"):
                    self.start_turn("plan", "", "repairing stuck tickets")
                self.say(highlighter.line(line))
            code = await self.child.wait()
            self.child = None
            if code == 0:
                self.say("WORK COMPLETE", f"bold {ORANGE}")
            else:
                self.say(f"Worker stopped with exit code {code}; inspect results above.", CORAL)

    async def native(self):
        profile = self.profiles[self.role]
        engine = profile["engine"]
        executable = "qwen" if engine in ("ollama", "openai") else engine
        args = [shutil.which(executable) or executable]
        env = os.environ.copy()
        if profile.get("claude_config_dir"):
            env["CLAUDE_CONFIG_DIR"] = profile["claude_config_dir"]
        if profile.get("model"):
            args += ["--model", profile["model"]]
        if engine in ("ollama", "openai"):
            env["OPENAI_API_KEY"] = "ollama" if engine == "ollama" else env.get(profile["key_env"], "")
            args += [
                "--auth-type",
                "openai",
                "--openai-base-url",
                ollama_base_url() if engine == "ollama" else profile["base_url"],
            ]
        with self.suspend():
            code = subprocess.call(args, cwd=self.project, env=env)
        self.say(f"Returned from native {executable} (exit {code}).")


def run_terminal(project, alias=None):
    if not sys.stdin.isatty():
        raise ValueError("Interactive Donut requires a terminal. Use --plan REQUEST or --work next COUNT in scripts.")
    Donut(project, alias).run()
