"""Donut's interactive terminal, backed by provider-owned sessions."""

import asyncio
import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from rich.text import Text
from textual import work
from textual.message import Message
from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, LoadingIndicator, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option
from .config import load
from .sessions import ClaudeSession, CodexSession, QwenSession
from .planner_output import ticket_snapshot
from .install_skills import install
from .platform import process_options
from .providers import ollama_base_url
from .ui import COLORS, logo_segments, release_version


def hex_color(name):
    return "#%02x%02x%02x" % COLORS[name]


ORANGE, CORAL, TEAL, NAVY, GRAY = (hex_color(name) for name in ("orange", "coral", "teal", "navy", "gray"))
DIM = "#8a96a3"

COMMANDS = [
    ("/plan", "Plan mode: describe work and the planner writes tickets"),
    ("/work", "Run tickets with the worker, one fresh context each"),
    ("/model", "Choose the account and model for this mode"),
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
        options = OptionList(id="menu-options")
        options.can_focus = False
        yield options
        yield Static("No matches", id="menu-empty")

    @property
    def is_open(self):
        return self.has_class("-open")

    def open(self, kind, title, rows, term=""):
        """rows: (label, value) or (label, value, description) tuples."""
        self.kind = kind
        self.border_title = title
        self.rows = [row if len(row) == 3 else (row[0], row[1], "") for row in rows]
        self.add_class("-open")
        self.filter(term)

    def close(self):
        self.kind = None
        self.remove_class("-open")

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
    #thinking {{height: 1; color: {ORANGE};}}
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

    def compose(self):
        yield RichLog(id="transcript", wrap=True, highlight=False, markup=False, auto_scroll=True)
        yield Static(id="response")
        yield LoadingIndicator(id="thinking")
        yield Menu(id="menu")
        yield Draft(
            id="draft",
            soft_wrap=True,
            show_line_numbers=False,
            placeholder="Describe what to plan, or type / for commands",
        )
        yield Static(id="status")
        yield Footer()

    def on_mount(self):
        self.welcome()
        self.status("Ready")
        if not self.config.get("aliases"):
            self.say(Text("No accounts configured. Exit and run donut setup.", style=CORAL))
        self.query_one("#thinking", LoadingIndicator).display = False
        self.query_one(Draft).focus()

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

    def status(self, activity):
        profile = self.profiles[self.mode]
        line = Text()
        line.append(f" {self.mode.upper()} ", style=f"bold #101820 on {ORANGE if self.mode == 'plan' else CORAL}")
        line.append(f"  {self.selected[self.mode] or 'unconfigured'}", style=f"bold {ORANGE}")
        line.append(f"  ·  {profile.get('model') or 'provider default'}", style=GRAY)
        line.append(f"  ·  {activity}", style=CORAL if activity != "Ready" else DIM)
        self.query_one("#status", Static).update(line)

    def say(self, text, style=None):
        self.query_one(RichLog).write(Text(text, style=style) if style and isinstance(text, str) else text)

    async def choose(self, title, rows):
        """Show rows in the inline menu and wait for a choice. Returns the chosen value, or None."""
        if not rows:
            return None
        menu = self.query_one(Menu)
        future = asyncio.get_running_loop().create_future()
        self.picking = future
        self.query_one(Draft).clear()
        menu.open("pick", title, rows)
        try:
            return await future
        finally:
            self.picking = None
            menu.close()
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
        if menu.kind == "pick":
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

    def event(self, kind, text):
        if kind == "text":
            from rich.markdown import Markdown as RenderedMarkdown

            self.turn_text += text
            self.query_one("#response", Static).update(RenderedMarkdown(self.turn_text))
        elif kind == "error":
            self.say("Error: " + str(text), CORAL)
        else:
            self.status(str(text))

    async def approve(self, name, details):
        summary = json.dumps(details, indent=2, ensure_ascii=False)
        if len(summary) > 1500:
            summary = summary[:1500] + "\n…"
        self.say(Text("Permission requested: " + name, style=f"bold {CORAL}"))
        self.say(Text(summary, style=GRAY))
        return bool(await self.choose("Allow " + name + "?", [("Allow once", True), ("Deny", False)]))

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
            session = cls(self.project, profile, self.event, self.approve)
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
        if self.busy:
            self.notify("A turn is running. Cancel it before starting another.", severity="warning")
            return
        editor = self.query_one(TextArea)
        text = editor.text.strip()
        if text:
            editor.clear()
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
        if not self.selected[self.mode] and not text.startswith(("/help", "/exit", "/quit")):
            self.say("Run donut setup to configure an account first.")
            return
        self.busy = True
        self.query_one("#thinking", LoadingIndicator).display = True
        self.status("Thinking…")
        try:
            if text.startswith("/"):
                await self.command(text)
            elif self.mode == "work":
                self.say("Choose tickets with /work. Free-form requests belong in /plan.")
            else:
                await self.plan(text)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.say("Error: " + str(error), CORAL)
        finally:
            self.active = None
            self.busy = False
            self.query_one("#thinking", LoadingIndicator).display = False
            self.status("Ready")

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
            else:
                self.say("Planning mode. Describe what to plan.", ORANGE)
        elif name == "/model":
            await self.models()
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
                "/plan · /work [next COUNT] [checkpoint] · /model · /usage · /plugin · /mcp · /context · /clear · /copy · /native · /exit\nProvider plugin commands are forwarded to the selected session. Use /native for built-ins requiring the provider’s own UI."
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
            self.status("Discovering " + alias)
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
            config["aliases"][alias] = self.profiles["work"]
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
            async for line in self.child.stdout:
                self.say(line.decode("utf-8", errors="replace").rstrip())
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
