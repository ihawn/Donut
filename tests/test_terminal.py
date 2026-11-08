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
                self.assertEqual(app.mode, "work")

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
                self.assertIn("_.-~~~~", lines[0])
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

    async def test_tool_approval_is_inline(self):
        with tempfile.TemporaryDirectory() as temp:
            app = await self.run_app(temp)
            async with app.run_test() as pilot:
                await pilot.pause()
                answer = asyncio.ensure_future(app.approve("Bash", {"command": "ls"}))
                await pilot.pause()
                menu = app.query_one(Menu)
                self.assertTrue(menu.is_open)
                self.assertEqual([row[0] for row in menu.visible_rows], ["Allow once", "Deny"])
                await pilot.press("down", "enter")
                await pilot.pause()
                self.assertFalse(await answer)
                answer = asyncio.ensure_future(app.approve("Bash", {"command": "ls"}))
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                self.assertTrue(await answer)
