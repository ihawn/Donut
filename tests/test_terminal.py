import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from donut.terminal import Donut, Picker


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_commands_and_mode_picker(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {'aliases': {'claude': {'engine': 'claude', 'model': 'sonnet'}, 'claude-personal': {'engine': 'claude', 'model': 'sonnet'}}, 'planner': 'claude', 'default_model': 'claude-personal'}
            with patch('donut.terminal.load', return_value=config): app = Donut(Path(temp))
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(app.selected['plan'], 'claude')
                self.assertEqual(app.selected['work'], 'claude-personal')
                app.execute('/help')
                await pilot.pause()
                self.assertFalse(app.busy)
                app.execute('/work')
                await pilot.pause()
                self.assertIsInstance(app.screen, Picker)
                app.screen.dismiss(None)
                await pilot.pause()
                self.assertEqual(app.mode, 'work')

    async def test_model_picker_keeps_account_and_model_together(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {'aliases': {'claude': {'engine': 'claude'}, 'claude-personal': {'engine': 'claude'}}}
            with patch('donut.terminal.load', return_value=config): app = Donut(Path(temp))
            fake = AsyncMock()
            fake.models.return_value = [{'id': 'sonnet', 'name': 'Sonnet 5.5'}]
            app.session = AsyncMock(return_value=fake)
            async with app.run_test() as pilot:
                app.execute('/model')
                await pilot.pause()
                self.assertEqual([row[0] for row in app.screen.rows], ['claude  Sonnet 5.5', 'claude-personal  Sonnet 5.5'])
                app.screen.dismiss(app.screen.rows[1][1])
                await pilot.pause()
                self.assertEqual(app.selected['plan'], 'claude-personal')
                self.assertEqual(app.profiles['plan']['model'], 'sonnet')
                self.assertEqual(app.selected['work'], 'claude')
