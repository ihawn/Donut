import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from donut.terminal import Donut, Draft, Menu, OptionList, RichLog


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_commands_and_mode_picker(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {
                "aliases": {
                    "claude": {"engine": "claude", "model": "sonnet"},
                    "claude-personal": {"engine": "claude", "model": "sonnet"},
                },
                "planner": "claude",
                "default_model": "claude-personal",
            }
            with patch("donut.terminal.load", return_value=config):
                app = Donut(Path(temp))
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(app.selected["plan"], "claude")
                self.assertEqual(app.selected["work"], "claude-personal")
                app.execute("/help")
                await pilot.pause()
                self.assertFalse(app.busy)
                app.execute("/work")
                await pilot.pause()
                menu = app.query_one(Menu)
                self.assertTrue(menu.is_open)
                self.assertEqual(menu.kind, "pick")
                self.assertEqual(len(app.screen_stack), 1)  # Inline, not a new screen.
                self.assertIs(app.focused, app.query_one(Draft))
                await pilot.press("escape")
                await pilot.pause()
                self.assertFalse(menu.is_open)
                self.assertFalse(app.busy)
                self.assertEqual(app.mode, "plan")  # /work returns to planning so follow-ups are accepted.

    async def test_model_picker_keeps_account_and_model_together(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {"aliases": {"claude": {"engine": "claude"}, "claude-personal": {"engine": "claude"}}}
            with patch("donut.terminal.load", return_value=config):
                app = Donut(Path(temp))
            fake = AsyncMock()
            fake.models.return_value = [{"id": "sonnet", "name": "Sonnet 5.5"}]
            app.session = AsyncMock(return_value=fake)
            async with app.run_test() as pilot:
                app.execute("/model")
                await pilot.pause()
                menu = app.query_one(Menu)
                self.assertEqual(
                    [row[0] for row in menu.visible_rows], ["claude  Sonnet 5.5", "claude-personal  Sonnet 5.5"]
                )
                await pilot.press("down", "enter")
                await pilot.pause()
                self.assertEqual(app.selected["plan"], "claude-personal")
                self.assertEqual(app.profiles["plan"]["model"], "sonnet")
                self.assertEqual(app.selected["work"], "claude")


class DraftTests(unittest.IsolatedAsyncioTestCase):
    def app(self, temp):
        config = {"aliases": {"claude": {"engine": "claude"}}, "planner": "claude"}
        with patch("donut.terminal.load", return_value=config):
            return Donut(Path(temp))

    async def test_enter_sends_and_newline_keys_insert(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            sent = []
            app.execute = lambda text: sent.append(text)
            async with app.run_test() as pilot:
                await pilot.pause()
                draft = app.query_one(Draft)
                draft.insert("first")
                await pilot.press("ctrl+j")
                draft.insert("second")
                await pilot.press("shift+enter")
                draft.insert("third")
                await pilot.pause()
                self.assertEqual(draft.text, "first\nsecond\nthird")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(sent, ["first\nsecond\nthird"])
                self.assertEqual(draft.text, "")

    async def test_ctrl_c_reaches_cancel_while_editing(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            app.action_cancel = AsyncMock()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one(Draft).insert("draft")
                await pilot.press("ctrl+c")
                await pilot.pause()
            app.action_cancel.assert_awaited()

    async def test_welcome_logo_opens_the_transcript(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            async with app.run_test() as pilot:
                await pilot.pause()
                lines = [strip.text for strip in app.query_one(RichLog).lines]
                self.assertIn("_.------", lines[0])
                self.assertTrue(any("do the thing." in line for line in lines[:3]))
                self.assertTrue(any("DONUT" in line for line in lines))


class SlashMenuTests(unittest.IsolatedAsyncioTestCase):
    async def run_app(self, temp):
        config = {"aliases": {"claude": {"engine": "claude"}}, "planner": "claude"}
        with patch("donut.terminal.load", return_value=config):
            app = Donut(Path(temp))
        self.sent = []
        app.execute = lambda text: self.sent.append(text)
        return app

    async def test_typing_slash_filters_inline_and_enter_runs_the_top_match(self):
        with tempfile.TemporaryDirectory() as temp:
            app = await self.run_app(temp)
            async with app.run_test() as pilot:
                await pilot.press("/")
                await pilot.pause()
                menu = app.query_one(Menu)
                self.assertTrue(menu.is_open)
                self.assertEqual(len(app.screen_stack), 1)
                self.assertEqual(menu.visible_rows[0][0], "/plan")
                await pilot.press("m", "o")
                await pilot.pause()
                self.assertEqual([row[0] for row in menu.visible_rows], ["/model"])
                self.assertEqual(app.query_one(OptionList).highlighted, 0)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(self.sent, ["/model"])
                self.assertFalse(menu.is_open)
                self.assertEqual(app.query_one(Draft).text, "")

    async def test_arrows_move_tab_completes_and_escape_closes(self):
        with tempfile.TemporaryDirectory() as temp:
            app = await self.run_app(temp)
            async with app.run_test() as pilot:
                draft = app.query_one(Draft)
                menu = app.query_one(Menu)
                await pilot.press("/", "c")
                await pilot.pause()
                self.assertEqual([row[0] for row in menu.visible_rows][:3], ["/context", "/clear", "/copy"])
                await pilot.press("down", "tab")
                await pilot.pause()
                self.assertEqual(draft.text, "/clear ")
                self.assertFalse(menu.is_open)
                self.assertEqual(self.sent, [])
                draft.clear()
                await pilot.press("/", "p")
                await pilot.pause()
                self.assertTrue(menu.is_open)
                await pilot.press("escape")
                await pilot.pause()
                self.assertFalse(menu.is_open)
                self.assertEqual(draft.text, "/p")

    async def test_arguments_close_the_menu_so_enter_sends_the_line(self):
        with tempfile.TemporaryDirectory() as temp:
            app = await self.run_app(temp)
            async with app.run_test() as pilot:
                await pilot.press("/", "w", "o", "r", "k", "space", "n")
                await pilot.pause()
                self.assertFalse(app.query_one(Menu).is_open)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(self.sent, ["/work n"])

    async def test_unknown_command_hides_the_menu(self):
        with tempfile.TemporaryDirectory() as temp:
            app = await self.run_app(temp)
            async with app.run_test() as pilot:
                await pilot.press("/", "z", "z")
                await pilot.pause()
                self.assertFalse(app.query_one(Menu).is_open)

    async def test_tool_approval_is_an_inline_panel_with_number_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            app = await self.run_app(temp)
            async with app.run_test() as pilot:
                await pilot.pause()
                answer = asyncio.ensure_future(app.approve("Bash", {"command": "ls -la", "description": "List files"}))
                await pilot.pause()
                menu = app.query_one(Menu)
                self.assertTrue(menu.is_open)
                self.assertEqual(menu.kind, "approve")
                self.assertEqual(menu.border_title, "Bash command")
                self.assertEqual([row[1] for row in menu.visible_rows], ["yes", "always", "no"])
                details = menu.query_one("#menu-details").render()
                self.assertIn("$ ls -la", str(details))
                self.assertNotIn("{", str(details))  # Not a raw JSON dump.
                await pilot.press("3")
                await pilot.pause()
                self.assertEqual(await answer, "no")
                answer = asyncio.ensure_future(app.approve("Bash", {"command": "ls"}))
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(await answer, "yes")
                answer = asyncio.ensure_future(app.approve("Bash", {"command": "pwd"}))
                await pilot.pause()
                await pilot.press("2")
                await pilot.pause()
                self.assertEqual(await answer, "always")
                self.assertEqual(await app.approve("Bash", {"command": "whoami"}), "always")  # No second prompt.
                self.assertFalse(menu.is_open)
                lines = [strip.text for strip in app.query_one(RichLog).lines]
                self.assertTrue(any("Denied Bash command" in line and "ls -la" in line for line in lines))

    async def test_escape_denies_and_keeps_the_draft(self):
        with tempfile.TemporaryDirectory() as temp:
            app = await self.run_app(temp)
            async with app.run_test() as pilot:
                draft = app.query_one(Draft)
                draft.insert("half-typed follow-up")
                answer = asyncio.ensure_future(app.approve("Write", {"file_path": "a.txt", "content": "x"}))
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(await answer, "no")
                self.assertEqual(draft.text, "half-typed follow-up")


class FakeSession:
    def __init__(self, app, delay=0.3):
        self.app, self.delay, self.calls, self.modes = app, delay, [], []

    async def query(self, text):
        self.calls.append(text)
        self.app.event("text", f"answer {len(self.calls)}")
        await asyncio.sleep(self.delay)

    async def set_permission(self, mode):
        self.modes.append(mode)

    async def interrupt(self):
        pass

    async def close(self):
        pass


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = {"aliases": {"claude": {"engine": "claude"}}, "planner": "claude"}
        self.saved = []
        patches = [
            patch("donut.terminal.load", return_value=config),
            patch("donut.terminal.save", side_effect=lambda c: self.saved.append(dict(c))),
            patch("donut.terminal.install"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.app = Donut(Path(self.temp.name))
        self.fake = FakeSession(self.app)

        async def session(alias, profile):
            self.app.sessions[(alias, profile.get("model"))] = self.fake
            return self.fake

        self.app.session = session

    async def wait_for(self, pilot, condition, timeout=5):
        for _ in range(int(timeout / 0.02)):
            if condition():
                return
            await pilot.pause(0.02)
        self.fail("condition not reached")

    async def send(self, pilot, text):
        draft = self.app.query_one(Draft)
        draft.clear()
        draft.insert(text)
        await pilot.press("enter")

    async def test_follow_ups_reach_the_same_session(self):
        async with self.app.run_test() as pilot:
            await self.send(pilot, "first")
            await pilot.pause(0.5)
            await self.send(pilot, "second")
            await pilot.pause(0.5)
            self.assertEqual(len(self.fake.calls), 2)
            self.assertTrue(self.fake.calls[1].endswith("second"))

    async def test_messages_sent_during_a_turn_are_queued_then_run(self):
        self.fake.delay = 3
        async with self.app.run_test() as pilot:
            await self.send(pilot, "first")
            await self.wait_for(pilot, lambda: len(self.fake.calls) == 1)
            self.assertTrue(self.app.busy)
            await self.send(pilot, "follow-up while busy")
            await self.wait_for(pilot, lambda: self.app.queued == ["follow-up while busy"])
            self.assertEqual(self.app.query_one(Draft).text, "")
            self.fake.delay = 0
            await self.wait_for(pilot, lambda: len(self.fake.calls) == 2 and not self.app.busy, timeout=8)
            self.assertTrue(self.fake.calls[1].endswith("follow-up while busy"))
            self.assertEqual(self.app.queued, [])

    async def test_cancel_returns_queued_messages_to_the_prompt(self):
        self.fake.delay = 3
        async with self.app.run_test() as pilot:
            await self.send(pilot, "first")
            await self.wait_for(pilot, lambda: len(self.fake.calls) == 1)
            await self.send(pilot, "keep me")
            await self.wait_for(pilot, lambda: self.app.queued == ["keep me"])
            await pilot.press("ctrl+c")
            await pilot.pause(0.05)
            self.assertEqual(self.app.queued, [])
            self.assertEqual(self.app.query_one(Draft).text, "keep me")

    async def test_free_text_in_work_mode_goes_to_the_planner(self):
        async with self.app.run_test() as pilot:
            self.app.mode = "work"
            await self.send(pilot, "what about tests?")
            await pilot.pause(0.5)
            self.assertEqual(len(self.fake.calls), 1)
            self.assertEqual(self.app.mode, "plan")

    async def test_permissions_picker_auto_toggle_and_shift_tab(self):
        async with self.app.run_test() as pilot:
            await self.send(pilot, "first")
            await pilot.pause(0.5)
            self.assertEqual(self.app.permission, "ask")
            await self.send(pilot, "/permissions")
            await pilot.pause()
            menu = self.app.query_one(Menu)
            self.assertEqual([row[1] for row in menu.visible_rows], ["ask", "accept_edits", "auto", "full"])
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            self.assertEqual(self.app.permission, "auto")
            self.assertEqual(self.fake.modes, ["auto"])
            self.assertEqual(self.saved[-1]["permission_mode"], "auto")
            await self.send(pilot, "/auto")
            await pilot.pause()
            self.assertEqual(self.app.permission, "ask")
            await pilot.press("shift+tab")
            await pilot.pause()
            self.assertEqual(self.app.permission, "accept_edits")
            await pilot.press("shift+tab")
            await pilot.pause()
            self.assertEqual(self.app.permission, "auto")
            self.assertEqual(self.fake.modes, ["auto", "ask", "accept_edits", "auto"])
            status = str(self.app.query_one("#status").render())
            self.assertIn("auto on", status)

    async def test_saved_permission_mode_is_restored(self):
        config = {"aliases": {"claude": {"engine": "claude"}}, "planner": "claude", "permission_mode": "auto"}
        with patch("donut.terminal.load", return_value=config):
            app = Donut(Path(self.temp.name))
        self.assertEqual(app.permission, "auto")


class WorkOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_updates_the_status_bar_and_output_is_highlighted(self):
        import sys

        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "runner.py"
            script.write_text(
                "import sys\n"
                "lines = ['Preparing local model\u2026', '\u280b', '\u2819', '\u2839',"
                " '[1/1] 047_sandbox-wall.md \u2014 fresh context',"
                " '[qwen3 \u00b7 Connected \u00b7 preparing project context]',"
                " '[Working \u00b7 15s elapsed \u00b7 waiting for agent output]',"
                " '[Working \u00b7 30s elapsed \u00b7 waiting for agent output]',"
                " 'Tests: 12 passed', '\\x1b[32mcolored\\x1b[0m']\n"
                "print('\\n'.join(lines))\n"
            )
            config = {"aliases": {"local": {"engine": "ollama", "model": "qwen3"}}, "default_model": "local"}
            with patch("donut.terminal.load", return_value=config):
                app = Donut(Path(temp))
            turns = []
            original_start = app.start_turn

            def start_turn(role, detail="", activity="thinking"):
                turns.append((role, detail))
                original_start(role, detail, activity)

            app.start_turn = start_turn
            launch = asyncio.create_subprocess_exec

            async def fake_exec(*args, **kwargs):
                return await launch(sys.executable, str(script), **kwargs)

            async with app.run_test() as pilot:
                await pilot.pause()
                with patch("donut.terminal.asyncio.create_subprocess_exec", side_effect=fake_exec):
                    await app.work_tickets("next 1")
                lines = [strip.text.rstrip() for strip in app.query_one(RichLog).lines]
            body = "\n".join(lines)
            self.assertNotIn("elapsed", body)
            self.assertFalse(any(line.strip() in ("\u280b", "\u2819", "\u2839") for line in lines))
            self.assertIn(("work", "047_sandbox-wall.md"), turns)
            self.assertIn("Tests: 12 passed", body)
            self.assertIn("colored", body)
            self.assertNotIn("\x1b", body)


class ActiveModelAndUsageTests(unittest.IsolatedAsyncioTestCase):
    def app(self, temp):
        config = {
            "aliases": {
                "claude-personal": {"engine": "claude", "model": "claude-opus-5-5", "claude_config_dir": "/x"},
                "codex": {"engine": "codex", "model": "gpt-6"},
                "local": {"engine": "ollama", "model": "qwen3.6:35b"},
                "other-local": {"engine": "ollama", "model": "llama"},
            },
            "planner": "claude-personal",
            "default_model": "local",
        }
        with patch("donut.terminal.load", return_value=config):
            return Donut(Path(temp))

    async def test_spinner_and_status_name_the_running_model(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            async with app.run_test() as pilot:
                await pilot.pause()
                idle = str(app.query_one("#status").render())
                self.assertIn("planner claude-opus-5-5", idle)
                self.assertIn("worker qwen3.6:35b", idle)
                self.assertFalse(app.query_one("#thinking").has_class("-busy"))
                app.busy = True
                app.start_turn("work", "047_wall.md", "working")
                app.event("activity", "Bash")
                await pilot.pause(0.2)
                spinner = str(app.query_one("#thinking").render())
                self.assertIn("qwen3.6:35b", spinner)
                self.assertIn("worker", spinner)
                self.assertIn("is running a command", spinner)
                self.assertIn("047_wall.md", spinner)
                self.assertIn("ctrl+c to cancel", spinner)
                self.assertTrue(app.query_one("#thinking").has_class("-busy"))
                status = app.query_one("#status").render()
                bold_spans = [str(status)[span.start : span.end] for span in status.spans if "bold" in str(span.style)]
                self.assertIn("qwen3.6:35b", bold_spans)
                self.assertNotIn("claude-opus-5-5", bold_spans)

    async def test_spinner_timer_tolerates_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            async with app.run_test() as pilot:
                await pilot.pause()
                app.busy = True
                app.start_turn("plan")
            # The 0.1s timer can still fire once the widgets are gone.
            app.tick()
            app.status("writing")

    async def test_usage_bar_shows_each_account_once(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            async with app.run_test() as pilot:
                await pilot.pause()
                bar = str(app.query_one("#usage").render())
                self.assertIn("claude-personal", bar)
                self.assertIn("codex", bar)
                self.assertIn("qwen3.6 local", bar)
                self.assertNotIn("llama", bar)  # Only local models in use are listed.
                app.event(
                    "usage", [{"window": "5h", "percent": 18.0}, {"window": "7d", "percent": 30.0}], ("claude", "/x")
                )
                app.event("usage", [{"window": "5h", "percent": 91.0}], ("codex",))
                await pilot.pause()
                bar = str(app.query_one("#usage").render())
                self.assertIn("18% 5h · 30% 7d", bar)
                self.assertIn("91% 5h", bar)
                self.assertIn("━", bar)
