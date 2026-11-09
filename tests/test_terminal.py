import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from donut.terminal import Donut, Draft, Menu, OptionList, RichLog

# Never start real providers from the background usage refresh during tests.
Donut.AUTO_REFRESH_QUOTAS = False


def setUpModule():
    """Saved /model, /review and permission choices go to a throwaway config folder, never the real one."""
    import os

    global _config_home, _previous_config_home
    _config_home = tempfile.TemporaryDirectory()
    _previous_config_home = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = _config_home.name


def tearDownModule():
    import os

    if _previous_config_home is None:
        os.environ.pop("XDG_CONFIG_HOME", None)
    else:
        os.environ["XDG_CONFIG_HOME"] = _previous_config_home
    _config_home.cleanup()


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
                self.assertEqual(
                    [row[3] for row in menu.visible_rows],
                    [
                        "/work next 1",
                        "/work next 3",
                        "/work next all",
                        "/work next 1 checkpoint",
                        "/work next all checkpoint",
                    ],
                )
                rendered = str(menu.query_one(OptionList).get_option_at_index(1).prompt)
                self.assertIn("Next 3 tickets", rendered)
                self.assertTrue(rendered.rstrip().endswith("/work next 3"))
                self.assertIn("/work next N", menu.border_subtitle)
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
                self.assertIn("82% left 5h · 70% 7d", bar)
                self.assertIn("9% left 5h", bar)
                self.assertIn("━", bar)


class UsageCommandTests(unittest.IsolatedAsyncioTestCase):
    def app(self, temp):
        config = {
            "aliases": {
                "claude": {"engine": "claude"},
                "codex": {"engine": "codex"},
                "api": {"engine": "claude", "claude_config_dir": "/api"},
            },
            "planner": "claude",
        }
        with patch("donut.terminal.load", return_value=config):
            return Donut(Path(temp))

    async def test_usage_command_shows_what_is_left_for_each_account(self):
        import time

        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            soon = time.time() + 2 * 3600 + 600
            answers = {
                "claude": [{"window": "5h", "percent": 18.0, "resets_at": soon}, {"window": "7d", "percent": 30.0}],
                "codex": None,
                "api": [],
            }

            async def session(alias, profile):
                fake = AsyncMock()
                fake.quota.return_value = answers[alias]
                app.sessions[(alias, profile.get("model"))] = fake
                app.session_profiles[(alias, profile.get("model"))] = profile
                return fake

            app.session = session
            async with app.run_test() as pilot:
                await pilot.pause()
                app.execute("/usage")
                await pilot.pause(0.3)
                body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
                bar = str(app.query_one("#usage").render())
            self.assertNotIn("Error", body)
            self.assertIn("82% left  5h  resets in 2h", body)
            self.assertIn("70% left  7d", body)
            self.assertIn("codex   usage unavailable", body)
            self.assertIn("api   no plan limits (API billing)", body)
            self.assertIn("82% left 5h ↻2h", bar)
            self.assertIn("api no plan limit", bar)

    async def test_refresh_without_connect_only_asks_open_sessions(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            app.session = AsyncMock(side_effect=AssertionError("must not start a session"))
            async with app.run_test() as pilot:
                await pilot.pause()
                await app.refresh_quotas(connect=False)
                self.assertEqual(app.quotas, {})


class QuotaFormattingTests(unittest.TestCase):
    def test_countdown_and_remaining(self):
        from donut.terminal import countdown, remaining

        self.assertEqual(countdown(1000 + 2 * 3600 + 600, now=1000), "2h10m")
        self.assertEqual(countdown(1000 + 3 * 86400 + 4 * 3600, now=1000), "3d4h")
        self.assertEqual(countdown(1000 + 30, now=1000), "1m")
        self.assertEqual(countdown(500, now=1000), "")
        self.assertEqual(countdown(None), "")
        self.assertEqual((remaining(18.4), remaining(120), remaining(-5)), (82, 0, 100))


class ReviewerTests(unittest.IsolatedAsyncioTestCase):
    def app(self, temp, **extra):
        config = {
            "aliases": {
                "claude": {"engine": "claude", "model": "claude-opus-5-5"},
                "codex": {"engine": "codex", "model": "gpt-6.1"},
                "local": {"engine": "ollama", "model": "qwen3.6"},
            },
            "planner": "claude",
            "default_model": "local",
            **extra,
        }
        self.saved = []
        patcher = patch("donut.terminal.save", side_effect=lambda c: self.saved.append(dict(c)))
        patcher.start()
        self.addCleanup(patcher.stop)
        with patch("donut.terminal.load", return_value=config):
            return Donut(Path(temp))

    def fake_models(self, app):
        fake = AsyncMock()
        fake.models.return_value = [{"id": "sonnet-5-5", "name": "Sonnet 5.5"}, {"id": "opus-5-5", "name": "Opus 5.5"}]
        app.session = AsyncMock(return_value=fake)

    def saved_roles(self):
        from donut.config import load

        return load().get("roles", {}), load().get("reviewer")

    async def pick(self, pilot, app, command, label):
        app.execute(command)
        await pilot.pause()
        rows = [row[0] for row in app.query_one(Menu).visible_rows]
        for _ in range(rows.index(label)):
            await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

    def roles(self, app):
        return (
            (app.selected["plan"], app.profiles["plan"].get("model")),
            (app.selected["review"], app.profiles["review"].get("model")) if app.profiles["review"] else None,
        )

    async def test_reviewer_follows_the_planner_until_chosen_then_they_stay_independent(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            self.fake_models(app)
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertIn("reviewer claude-opus-5-5 (= planner)", str(app.query_one("#status").render()))
                # Default: changing the planner moves the reviewer with it.
                await self.pick(pilot, app, "/planner", "claude  Sonnet 5.5")
                self.assertEqual(self.roles(app), (("claude", "sonnet-5-5"), ("claude", "sonnet-5-5")))
                # Choosing a reviewer, down to the model, leaves the planner alone.
                await self.pick(pilot, app, "/reviewer", "codex  Opus 5.5")
                self.assertEqual(self.roles(app), (("claude", "sonnet-5-5"), ("codex", "opus-5-5")))
                self.assertIn("reviewer opus-5-5", str(app.query_one("#status").render()))
                # And changing the planner now leaves the chosen reviewer alone.
                await self.pick(pilot, app, "/planner", "claude  Opus 5.5")
                self.assertEqual(self.roles(app), (("claude", "opus-5-5"), ("codex", "opus-5-5")))
                roles, reviewer = self.saved_roles()
                self.assertEqual(roles["plan"], {"alias": "claude", "model": "opus-5-5"})
                self.assertEqual(roles["review"], {"alias": "codex", "model": "opus-5-5"})
                app.execute("/reviewer off")
                await pilot.pause()
                self.assertIn("reviewer off", str(app.query_one("#status").render()))
                self.assertEqual(self.saved_roles()[1], "off")
                app.execute("/reviewer planner")
                await pilot.pause()
                self.assertEqual(self.roles(app)[1], ("claude", "opus-5-5"))
                self.assertEqual(self.saved_roles(), ({"plan": {"alias": "claude", "model": "opus-5-5"}}, None))

    async def test_choices_stick_across_restarts(self):
        from donut.config import load, remember_role, save

        with tempfile.TemporaryDirectory() as temp:
            base = {
                "aliases": {"claude": {"engine": "claude", "model": "claude-opus-5-5"}, "codex": {"engine": "codex"}},
                "planner": "claude",
                "default_model": "claude",
            }
            save(base)
            remember_role("plan", "claude", "sonnet-5-5")
            remember_role("review", "codex", "gpt-6.1")
            with patch("donut.terminal.load", side_effect=load):
                app = Donut(Path(temp))
            self.assertEqual(self.roles(app), (("claude", "sonnet-5-5"), ("codex", "gpt-6.1")))
            self.assertEqual(app.reviewer, "chosen")

    async def test_work_passes_the_reviewer_and_the_spinner_follows_review_rounds(self):
        import sys

        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp, reviewer="codex")
            script = Path(temp) / "runner.py"
            script.write_text(
                "import json, os, pathlib, time\n"
                "config = json.loads((pathlib.Path(os.environ['XDG_CONFIG_HOME']) / 'donut/config.json').read_text())\n"
                "print('CONFIG', config.get('reviewer'), config['aliases'].get(config.get('reviewer'), {}).get('model'), flush=True)\n"
                "print('[1/1] 001_jump.md \\u2014 fresh context', flush=True)\n"
                "print('[review 1/3] 001_jump.md \\u2014 gpt-6.1, fresh context', flush=True)\n"
                "print('RECOVERY', config.get('recovery'), config['aliases'].get(config.get('recovery'), {}).get('model'), flush=True)\n"
                "print('[recover] 001_jump.md \\u2014 claude-opus-5-5, fresh context', flush=True)\n"
                "time.sleep(0.6)\n"
                "print('VERDICT: APPROVED', flush=True)\n"
                "print('Review: approved', flush=True)\n"
            )
            turns = []
            original = app.start_turn
            app.start_turn = lambda role, detail="", activity="thinking": (
                turns.append((role, activity)),
                original(role, detail, activity),
            )
            launch = asyncio.create_subprocess_exec

            async def fake_exec(*args, **kwargs):
                return await launch(sys.executable, str(script), **kwargs)

            async with app.run_test() as pilot:
                await pilot.pause()
                with patch("donut.terminal.asyncio.create_subprocess_exec", side_effect=fake_exec):
                    await app.work_tickets("next 1")
                lines = [strip.text.rstrip() for strip in app.query_one(RichLog).lines]
            self.assertIn("CONFIG donut-reviewer gpt-6.1", lines)
            self.assertIn(("review", "reviewing"), turns)
            self.assertIn("RECOVERY donut-planner claude-opus-5-5", lines)
            self.assertIn(("plan", "recovering a blocked ticket"), turns)
            self.assertLess(turns.index(("work", "working")), turns.index(("review", "reviewing")))


class ModeAndRoleTests(unittest.IsolatedAsyncioTestCase):
    """/planner, /worker, /reviewer and /soloist pick models; /plan, /work, /review and /solo enter the mode."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = {
            "aliases": {
                "claude": {"engine": "claude", "model": "claude-opus-5-5"},
                "local": {"engine": "ollama", "model": "qwen3.6"},
            },
            "planner": "claude",
            "default_model": "local",
        }
        for item in (patch("donut.terminal.load", return_value=config), patch("donut.terminal.install")):
            item.start()
            self.addCleanup(item.stop)
        self.app = Donut(Path(self.temp.name))
        self.sessions, self.purposes = {}, []

        async def session(alias, profile, purpose=None):
            self.purposes.append(purpose)
            key = self.app.session_key(alias, profile, purpose)
            if key not in self.sessions:
                fake = FakeSession(self.app, delay=0)
                fake.models = AsyncMock(return_value=[{"id": "claude-sonnet-5-5", "name": "Sonnet 5.5"}])
                self.sessions[key] = fake
            return self.sessions[key]

        self.app.session = session

    async def send(self, pilot, text):
        draft = self.app.query_one(Draft)
        draft.clear()
        draft.insert(text)
        await pilot.press("enter")
        await pilot.pause(0.2)

    def menu_open(self):
        return self.app.query_one(Menu).is_open

    async def test_solo_asks_for_a_soloist_once_then_talks_to_it_directly(self):
        from donut.config import load

        async with self.app.run_test() as pilot:
            self.assertEqual(self.app.profiles["solo"], {})
            await self.send(pilot, "/solo")
            self.assertTrue(self.menu_open())  # No soloist yet, so it asks.
            self.assertIn("soloist", self.app.query_one(Menu).border_title)
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual(self.app.mode, "solo")
            self.assertEqual(self.app.profiles["solo"]["model"], "claude-sonnet-5-5")
            self.assertEqual(self.app.profiles["work"]["model"], "qwen3.6")  # The worker is untouched.
            self.assertEqual(load()["roles"]["solo"], {"alias": "claude", "model": "claude-sonnet-5-5"})
            status = str(self.app.query_one("#status").render())
            self.assertIn("SOLO", status)
            self.assertIn("soloist claude-sonnet-5-5", status)
            self.assertIn("planner claude-opus-5-5", status)  # Every role stays visible in solo mode.
            await self.send(pilot, "rename jump() to leap()")
            self.assertEqual(self.sessions[("claude@solo", "claude-sonnet-5-5")].calls, ["rename jump() to leap()"])
            await self.send(pilot, "/plan")
            self.assertEqual(self.app.mode, "plan")
            self.assertFalse(self.menu_open())  # A planner is set, so /plan does not ask.
            await self.send(pilot, "/solo")
            self.assertFalse(self.menu_open())  # Chosen once; not asked again.
            self.assertEqual(self.app.mode, "solo")

    async def test_role_commands_only_change_their_own_role(self):
        async with self.app.run_test() as pilot:
            await self.send(pilot, "/worker")
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual(self.app.profiles["work"]["model"], "claude-sonnet-5-5")
            self.assertEqual(self.app.profiles["plan"]["model"], "claude-opus-5-5")
            self.assertEqual(self.app.mode, "plan")  # Picking a model does not change the mode.
            await self.send(pilot, "/soloist local")
            self.assertEqual(self.app.profiles["solo"]["model"], "qwen3.6")
            self.assertEqual(self.app.profiles["work"]["model"], "claude-sonnet-5-5")
            await self.send(pilot, "/planner local")
            self.assertEqual(self.app.profiles["plan"]["model"], "qwen3.6")
            self.assertEqual(self.app.profiles["solo"]["model"], "qwen3.6")
            self.assertEqual(self.app.profiles["review"]["model"], "qwen3.6")  # The default reviewer follows.

    async def test_review_mode_talks_to_the_reviewer_read_only(self):
        async with self.app.run_test() as pilot:
            await self.send(pilot, "/review")
            self.assertFalse(self.menu_open())  # The reviewer defaults to the planner, so it is set.
            self.assertEqual(self.app.mode, "review")
            self.assertIn("REVIEW", str(self.app.query_one("#status").render()))
            await self.send(pilot, "check the wall jump for off-by-one errors")
            review = self.sessions[("claude@review", "claude-opus-5-5")]
            self.assertTrue(review.calls[0].startswith("You are the reviewer."))
            self.assertTrue(review.calls[0].endswith("check the wall jump for off-by-one errors"))
            self.assertIn("review", self.purposes)

    async def test_every_role_is_shown_in_every_mode(self):
        async with self.app.run_test() as pilot:
            await self.send(pilot, "/soloist local")
            for mode in ("plan", "work", "review", "solo"):
                self.app.set_mode(mode)
                await pilot.pause()
                status = str(self.app.query_one("#status").render())
                for expected in (
                    mode.upper(),
                    "planner claude-opus-5-5",
                    "worker qwen3.6",
                    "reviewer claude-opus-5-5 (= planner)",
                    "soloist qwen3.6",
                ):
                    self.assertIn(expected, status, mode)
            await self.send(pilot, "/reviewer off")
            self.assertIn("reviewer off", str(self.app.query_one("#status").render()))

    async def test_an_unset_soloist_says_so(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            self.assertIn("soloist not set", str(self.app.query_one("#status").render()))

    async def test_review_asks_for_a_reviewer_when_review_is_off(self):
        async with self.app.run_test() as pilot:
            await self.send(pilot, "/reviewer off")
            await self.send(pilot, "/review")
            self.assertTrue(self.menu_open())
            self.assertNotIn("off", [row[1] for row in self.app.query_one(Menu).visible_rows])
            await pilot.press("escape")
            await pilot.pause(0.2)
            self.assertEqual(self.app.mode, "plan")

    async def test_f3_cycles_plan_work_review_and_solo(self):
        async with self.app.run_test() as pilot:
            await self.send(pilot, "/soloist claude")
            for expected in ("work", "review", "solo", "plan"):
                if self.menu_open():
                    await pilot.press("escape")  # Close the work picker.
                    await pilot.pause(0.1)
                await pilot.press("f3")
                await pilot.pause(0.2)
                self.assertEqual(self.app.mode, expected)
                if expected == "work":
                    await pilot.press("escape")
                    await pilot.pause(0.1)
                    self.app.set_mode("work")


class ReadOnlySessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_mode_sessions_are_read_only(self):
        created = []

        class Recorder:
            def __init__(self, project, profile, emit, approve):
                created.append(self)

            async def connect(self):
                self.connected_read_only = self.read_only

        with tempfile.TemporaryDirectory() as temp:
            config = {"aliases": {"claude": {"engine": "claude"}}, "planner": "claude"}
            with patch("donut.terminal.load", return_value=config):
                app = Donut(Path(temp))
            with patch("donut.terminal.ClaudeSession", Recorder):
                await app.session("claude", {"engine": "claude"}, "review")
                await app.session("claude", {"engine": "claude"}, "solo")
        self.assertEqual([session.connected_read_only for session in created], [True, False])


class PackEntriesTests(unittest.TestCase):
    def test_lines_break_only_between_entries(self):
        from rich.text import Text
        from donut.terminal import pack_entries

        entries = [
            Text("PLAN"),
            Text("planner claude-opus-5-5"),
            Text("reviewer claude-opus-5-5 (= planner)"),
            Text("ask"),
        ]
        packed = pack_entries(entries, 40).plain.split("\n")
        self.assertEqual(packed, ["PLAN  planner claude-opus-5-5", "reviewer claude-opus-5-5 (= planner)", "ask"])
        self.assertEqual(pack_entries(entries, 200).plain.count("\n"), 0)
