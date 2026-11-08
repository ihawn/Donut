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
from textual import work
from textual.message import Message
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    TextArea,
    LoadingIndicator,
)
from .config import load
from .sessions import ClaudeSession, CodexSession, QwenSession
from .planner_output import ticket_snapshot
from .install_skills import install
from .platform import process_options
from .providers import ollama_base_url


class Picker(ModalScreen):
    CSS = "Picker {align: center middle;} Picker > Vertical {width: 85%; height: 75%; border: round #17b8be; padding: 1;} Picker ListView {height: 1fr;}"

    def __init__(self, title, rows):
        super().__init__()
        self.title_text, self.rows = title, rows

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text)
            yield Input(placeholder="Search…", id="search")
            yield ListView(id="choices")
            yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.filter("")

    def filter(self, term):
        choices = self.query_one("#choices", ListView)
        choices.clear()
        for label, value in self.rows:
            if term.casefold() in label.casefold():
                item = ListItem(Label(label))
                item.value = value
                choices.append(item)

    def on_input_changed(self, event):
        self.filter(event.value)

    def on_list_view_selected(self, event):
        self.dismiss(event.item.value)

    def on_button_pressed(self, event):
        self.dismiss(None)

    def key_escape(self):
        self.dismiss(None)


class Approval(ModalScreen):
    CSS = "Approval {align: center middle;} Approval > Vertical {width: 80%; height: 70%; border: round #ff5e4e; padding: 1;} Approval RichLog {height: 1fr;}"

    def __init__(self, title, details):
        super().__init__()
        self.title_text, self.details = title, details

    def compose(self):
        with Vertical():
            yield Label(self.title_text)
            yield RichLog(id="details", wrap=True)
            yield Button("Allow once", id="allow", variant="success")
            yield Button("Deny", id="deny", variant="error")

    def on_mount(self):
        self.query_one(RichLog).write(json.dumps(self.details, indent=2, ensure_ascii=False))

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "allow")

    def key_escape(self):
        self.dismiss(False)


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
    CSS = """Screen {background: #101820;} #status {height: 2; color: #17b8be; padding-left: 1;} #transcript {height: 1fr;} #draft {height: 6; border: round #17b8be;} #response {height: auto; max-height: 20; overflow-y: auto;} #thinking {height: 1;} #send {dock: bottom; height: 3;}"""
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

    def compose(self):
        yield Header()
        yield Static(id="status")
        yield RichLog(id="transcript", wrap=True, highlight=False, markup=False, auto_scroll=True)
        yield Static(id="response")
        yield LoadingIndicator(id="thinking")
        yield Draft(id="draft", soft_wrap=True, show_line_numbers=False)
        yield Button("Send · Enter", id="send")
        yield Footer()

    def on_mount(self):
        self.status("Ready")
        self.say("Your planning model writes the tickets. Your worker model takes them one at a time. Do the thing.")
        self.say(
            "Use /plan or /work to choose a mode. /help lists commands. Enter sends; Shift+Enter, Alt+Enter or Ctrl+J inserts a newline."
        )
        if not self.config.get("aliases"):
            self.say("No accounts configured. Exit and run donut setup.")
        self.query_one("#thinking", LoadingIndicator).display = False
        self.query_one(TextArea).focus()

    def status(self, activity):
        profile = self.profiles[self.mode]
        self.query_one("#status", Static).update(
            f"{self.mode.upper()}  ·  {self.selected[self.mode] or 'unconfigured'}  ·  {profile.get('model') or 'provider default'}  ·  {activity}"
        )

    def say(self, text):
        self.query_one(RichLog).write(text)

    def event(self, kind, text):
        if kind == "text":
            from rich.markdown import Markdown as RenderedMarkdown

            self.turn_text += text
            self.query_one("#response", Static).update(RenderedMarkdown(self.turn_text))
        elif kind == "error":
            self.say("Error: " + str(text))
        else:
            self.status(str(text))

    async def approve(self, name, details):
        answer = asyncio.get_running_loop().create_future()

        def respond(value):
            if not answer.done():
                answer.set_result(bool(value))

        self.push_screen(Approval(name, details), respond)
        return await answer

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
        if event.text_area.text == "/" and not self.busy:
            self.run_worker(self.slash_menu(), group="slash-menu", exclusive=True)

    async def slash_menu(self):
        names = [
            "/plan",
            "/work",
            "/model",
            "/usage",
            "/plugin",
            "/mcp",
            "/context",
            "/clear",
            "/copy",
            "/native",
            "/help",
            "/exit",
        ]
        try:
            session = await self.session(self.selected[self.mode], self.profiles[self.mode])
            if isinstance(session, ClaudeSession):
                for command in session.info.get("commands", []):
                    name = command.get("name") if isinstance(command, dict) else command
                    if isinstance(name, str):
                        names.append("/" + name.lstrip("/"))
        except Exception:
            pass
        selected = await self.push_screen_wait(Picker("Commands", [(name, name) for name in dict.fromkeys(names)]))
        if selected:
            self.query_one(TextArea).load_text(selected + " ")

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

    def on_button_pressed(self, event):
        if event.button.id == "send":
            self.action_send()

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
        if isinstance(self.screen, Approval):
            self.screen.dismiss(False)
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
            self.say("Error: " + str(error))
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
                    else await self.push_screen_wait(
                        Picker(
                            "Work tickets",
                            [
                                ("Next ticket", "next 1"),
                                ("Next 3 tickets", "next 3"),
                                ("All eligible tickets", "next all"),
                                ("Next ticket, git checkpoint after it", "next 1 checkpoint"),
                                ("All eligible tickets, git checkpoint after each", "next all checkpoint"),
                            ],
                        )
                    )
                )
                if choice:
                    await self.work_tickets(choice)
            else:
                self.say("Planning mode. Describe what to plan.")
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
                    selected = await self.push_screen_wait(
                        Picker("Claude plugin commands · /native for management", rows)
                    )
                    if selected:
                        self.query_one(TextArea).load_text(selected + " ")
                else:
                    rows = []
                    for market in data.get("marketplaces", []):
                        for plugin in market.get("plugins", []):
                            rows.append((plugin.get("name", "?") + " · " + str(plugin.get("installed", False)), plugin))
                    selected = await self.push_screen_wait(Picker("Codex plugins · /native for management", rows))
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
        self.say("You: " + text)
        self.turn_text = ""
        await self.active.query(prompt)
        self.answer()
        created = [path for key, path in ticket_snapshot(self.project).items() if key not in before]
        self.say("Tickets created: " + str(len(created)))
        for path in sorted(created):
            self.say("  " + path.relative_to(self.project).as_posix())
        self.say("PLANNING COMPLETE · /work next 1")

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
            selected = await self.push_screen_wait(Picker("Select account and model for " + self.mode, rows))
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
            self.say("WORK COMPLETE" if code == 0 else f"Worker stopped with exit code {code}; inspect results above.")

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
