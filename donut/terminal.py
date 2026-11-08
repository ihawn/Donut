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
from .config import load, save
from .highlight import Highlighter
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


def usage_bar(percent, width=8):
    """A thin bar colored by how close the window is to its limit."""
    filled = max(0, min(width, round(percent / 100 * width)))
    color = TEAL if percent < 50 else ORANGE if percent < 80 else CORAL
    bar = Text("━" * filled, style=color)
    bar.append("─" * (width - filled), style="#3b4652")
    return bar, color


def model_name(profile, alias):
    return profile.get("model") or alias or "provider default"


COMMANDS = [
    ("/plan", "Plan mode: describe work and the planner writes tickets"),
    ("/work", "Run tickets with the worker, one fresh context each"),
    ("/model", "Choose the account and model for this mode"),
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
           border: round {CORAL}; border-title-color: {ORANGE}; border-title-style: bold;}}
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
        """rows: (label, value) or (label, value, description) tuples.

        kind is "slash", "pick" (typing filters) or "approve" (fixed rows, number keys choose, details shown).
        """
        self.kind = kind
        self.border_title = title
        self.border_subtitle = subtitle
        self.rows = [row if len(row) == 3 else (row[0], row[1], "") for row in rows]
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
        width = max((len(label) for label, _, _ in self.visible_rows), default=0)
        prompts = []
        for index, (label, _, description) in enumerate(self.visible_rows):
            chosen = index == highlighted
            prompt = Text("› " if chosen else "  ", style=f"bold {CORAL}")
            if self.kind == "approve":
                prompt.append(f"{index + 1}. ", style=ORANGE if chosen else DIM)
            prompt.append(label.ljust(width), style=f"bold {ORANGE}" if chosen else ORANGE)
            if description:
                prompt.append("   " + description, style="#ffffff" if chosen else DIM)
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
    #status {{height: 1; padding: 0 2;}}
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
        self.mode = "plan"
        aliases = self.config.get("aliases", {})
        if alias and alias not in aliases:
            raise ValueError("Unknown model alias: " + alias)
        self.selected = {
            role: alias or self.config.get(key) or next(iter(aliases), None)
            for role, key in (("plan", "planner"), ("work", "default_model"))
        }
        self.profiles = {role: copy.deepcopy(aliases.get(name, {})) for role, name in self.selected.items()}
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
        self.usage = {}  # usage_key -> [{"window", "percent", "resets_at"}]

    def compose(self):
        yield RichLog(id="transcript", wrap=True, highlight=False, markup=False, auto_scroll=True)
        yield Static(id="response")
        yield Static(id="thinking")
        yield Menu(id="menu")
        yield Draft(
            id="draft",
            soft_wrap=True,
            show_line_numbers=False,
            placeholder="Describe what to plan, or type / for commands",
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

    def tick(self):
        if self.busy:
            try:
                self.render_thinking()
            except NoMatches:
                pass  # The timer can fire once more while the app shuts down.

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
            line.append(f"  {'planner' if role == 'plan' else 'worker'}", style=DIM)
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
            ("/plan", "write tickets"),
            ("/work", "build them"),
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
        line = Text()
        line.append(f" {self.mode.upper()} ", style=f"bold #101820 on {ORANGE if self.mode == 'plan' else CORAL}")
        running_role = self.running[0] if self.busy and self.running else None
        for role, title in (("plan", "planner"), ("work", "worker")):
            alias, profile = self.selected.get(role), self.profiles.get(role, {})
            if role == running_role:
                line.append("  ● ", style=f"bold {ORANGE}")
                line.append(title + " ", style=f"bold {ORANGE}")
                line.append(model_name(profile, alias), style=f"bold {ORANGE}")
            else:
                focus = not running_role and role == self.mode
                line.append("  ○ ", style=GRAY if focus else DIM)
                line.append(title + " ", style=DIM)
                line.append(model_name(profile, alias), style=GRAY if focus else DIM)
        label = PERMISSION_MODES[self.permission][0].lower()
        if self.permission == "ask":
            line.append(f"   ⏵ {label}", style=DIM)
        else:
            line.append(f"   ⏵⏵ {label} on", style=f"bold {CORAL if self.permission == 'full' else ORANGE}")
        line.append("  (shift+tab)", style=DIM)
        if self.queued:
            line.append(f"   {len(self.queued)} queued", style=CORAL)
        self.query_one("#status", Static).update(line)

    def usage_groups(self):
        """One entry per quota: every configured Claude login and Codex, plus the local models in use."""
        groups = {}
        selected = {self.selected.get(role) for role in ("plan", "work")}
        for alias, profile in self.config.get("aliases", {}).items():
            key = usage_key(profile)
            if key[0] == "local" and alias not in selected:
                continue
            label = alias if key[0] != "local" else model_name(profile, alias).split(":")[0]
            if key not in groups or alias in selected:
                groups[key] = label
        for role in ("plan", "work"):  # Models picked with /model may not be saved aliases.
            profile = self.profiles.get(role, {})
            if profile and usage_key(profile) not in groups:
                key = usage_key(profile)
                groups[key] = self.selected.get(role) if key[0] != "local" else model_name(profile, "").split(":")[0]
        return groups

    def render_usage(self):
        line = Text()
        running = usage_key(self.running[2]) if self.busy and self.running else None
        for key, label in self.usage_groups().items():
            if line:
                line.append("    ")
            line.append(label, style=f"bold {ORANGE}" if key == running else GRAY)
            line.append(" ")
            if key[0] == "local":
                line.append("local", style=DIM)
                continue
            windows = self.usage.get(key)
            if not windows:
                line.append("────────", style="#3b4652")
                line.append(" –", style=DIM)
                continue
            first, *rest = windows
            bar, color = usage_bar(first["percent"])
            line.append_text(bar)
            line.append(f" {round(first['percent'])}%", style=color)
            if first["window"]:
                line.append(" " + first["window"], style=DIM)
            for window in rest[:1]:
                line.append(f" · {round(window['percent'])}%", style=usage_bar(window["percent"])[1])
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
        session = self.sessions.get((self.selected[self.mode], self.profiles[self.mode].get("model")))
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
                self.usage[key] = text
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
            engine = self.profiles[self.mode].get("engine")
            if mode == "ask" and engine in ("ollama", "openai"):
                line.append(" Qwen can't show approval prompts here, so it edits files without asking.", style=DIM)
            self.say(line)

    def action_cycle_permission(self):
        modes = PERMISSION_CYCLE
        following = modes[(modes.index(self.permission) + 1) % len(modes)] if self.permission in modes else "ask"
        self.run_worker(self.set_permission(following), group="permission")

    async def session(self, alias, profile):
        key = (alias, profile.get("model"))
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
            try:
                await session.connect()
            except BaseException:
                if hasattr(session, "process"):
                    await session.close()
                raise
            self.sessions[key] = session
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
        if not self.busy:
            self.execute("/" + ("work" if self.mode == "plan" else "plan"))

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
        if not self.selected[self.mode] and not text.startswith(("/help", "/exit", "/quit", "/permissions", "/auto")):
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
            else:
                if self.mode == "work":
                    # Conversation always goes to the planner; /work only runs tickets.
                    self.mode = "plan"
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
        elif name in ("/plan", "/work"):
            self.mode = name[1:]
            if self.mode == "work":
                choice = (
                    " ".join(args)
                    if args
                    else await self.choose(
                        "Work tickets",
                        [
                            ("Next ticket", "next 1", "one ticket in a fresh context"),
                            ("Next 3 tickets", "next 3", "up to three, one fresh context each"),
                            ("All eligible tickets", "next all", "until the queue is empty or blocked"),
                            ("Next ticket + checkpoint", "next 1 checkpoint", "git commit after it"),
                            ("All tickets + checkpoints", "next all checkpoint", "git commit after each"),
                        ],
                    )
                )
                if choice:
                    await self.work_tickets(choice)
                # Back to planning, so the next message is a follow-up rather than refused.
                self.mode = "plan"
            else:
                self.say("Planning mode. Describe what to plan.", ORANGE)
        elif name == "/model":
            await self.models()
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
            session = self.sessions.pop((self.selected[self.mode], self.profiles[self.mode].get("model")), None)
            if session:
                await session.close()
            self.say("New conversation. Existing tickets are preserved.")
        elif name == "/copy":
            self.copy_to_clipboard(self.last_answer)
            self.notify("Copied last response (terminal clipboard support required).")
        elif name == "/help":
            self.say(
                "/plan · /work [next COUNT] [checkpoint] · /model · /permissions [ask|accept_edits|auto|full] · /auto · /usage · /plugin · /mcp · /context · /clear · /copy · /native · /exit\nShift+Tab cycles Ask, Accept edits and Auto. Messages sent during a turn are queued. Provider plugin commands are forwarded to the selected session. Use /native for built-ins requiring the provider’s own UI."
            )
        elif name in ("/plugin", "/mcp", "/context"):
            session = await self.session(self.selected[self.mode], self.profiles[self.mode])
            if isinstance(session, ClaudeSession):
                if name == "/plugin":
                    data = session.info.get("commands", [])
                elif name == "/mcp":
                    data = await session.client.get_mcp_status()
                else:
                    data = await session.client.get_context_usage()
            elif isinstance(session, CodexSession):
                method = {"/plugin": "plugin/list", "/mcp": "mcpServerStatus/list", "/context": "thread/read"}[name]
                if name == "/context" and not session.thread:
                    self.say("No active Codex thread yet.")
                    return
                data = await session.request(
                    method, {"threadId": session.thread, "includeTurns": False} if name == "/context" else {}
                )
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
                            rows.append((plugin.get("name", "?") + " · " + str(plugin.get("installed", False)), plugin))
                    selected = await self.choose("Codex plugins · /native for management", rows)
                    if selected:
                        self.say(json.dumps(selected, indent=2, ensure_ascii=False))
            else:
                self.say(json.dumps(data, indent=2, ensure_ascii=False))
        elif name == "/native":
            await self.native()
        else:
            self.active = await self.session(self.selected[self.mode], self.profiles[self.mode])
            self.start_turn(self.mode)
            self.turn_text = ""
            await self.active.query(text)
            self.answer()

    def answer(self):
        self.query_one("#response", Static).update("")
        self.last_answer = self.turn_text
        from rich.markdown import Markdown as RenderedMarkdown

        self.say(RenderedMarkdown(self.turn_text))

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
        created = [path for key, path in ticket_snapshot(self.project).items() if key not in before]
        self.say("Tickets created: " + str(len(created)), CORAL)
        for path in sorted(created):
            self.say("  " + path.relative_to(self.project).as_posix(), GRAY)
        self.say("PLANNING COMPLETE · /work next 1", f"bold {ORANGE}")

    async def models(self):
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
        if rows:
            selected = await self.choose("Account and model for " + self.mode, rows)
            if selected:
                self.selected[self.mode], self.profiles[self.mode] = selected

    async def usage(self):
        for alias, profile in self.config["aliases"].items():
            try:
                session = await self.session(alias, profile)
                data = await session.account_usage()
                self.say(alias + "\n" + json.dumps(data, indent=2))
            except Exception as error:
                self.say(alias + ": usage unavailable: " + str(error))

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
                self.say(highlighter.line(line))
            code = await self.child.wait()
            self.child = None
            if code == 0:
                self.say("WORK COMPLETE", f"bold {ORANGE}")
            else:
                self.say(f"Worker stopped with exit code {code}; inspect results above.", CORAL)

    async def native(self):
        profile = self.profiles[self.mode]
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
