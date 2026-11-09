import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from donut import plugins, ponytail


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


REVIEW_SKILL = """---
name: ponytail-review
description: >
  Quality review of a change: is the logic right,
  is it safe.
---

Review a change.
"""


def install_claude_ponytail(claude, enabled=True):
    """A Claude login with Ponytail installed the way /plugin install leaves it."""
    plugin = claude / "plugins" / "cache" / "ponytail" / "ponytail" / "5.3.3"
    write(plugin / "skills" / "ponytail-review" / "SKILL.md", REVIEW_SKILL)
    write(plugin / "skills" / "ponytail" / "SKILL.md", "---\nname: ponytail\ndescription: Lazy senior dev mode.\n---\n")
    write(
        claude / "plugins" / "installed_plugins.json",
        json.dumps({"version": 2, "plugins": {"ponytail@ponytail": [{"scope": "user", "installPath": str(plugin)}]}}),
    )
    write(claude / "settings.json", json.dumps({"enabledPlugins": {"ponytail@ponytail": enabled}}))


class PluginCommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.claude, self.codex = self.root / "claude", self.root / "codex"
        environment = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.codex)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_claude_plugin_skills_are_listed_before_any_session_opens(self):
        install_claude_ponytail(self.claude)
        write(self.claude / "commands" / "deploy.md", "---\ndescription: Ship it\n---\n")
        found = {c["name"]: c for c in plugins.commands({"engine": "claude"}, self.root)}
        self.assertEqual(
            found["/ponytail-review"]["description"], "Quality review of a change: is the logic right, is it safe."
        )
        self.assertEqual(found["/ponytail-review"]["send"], "/ponytail-review")
        self.assertEqual(found["/deploy"]["description"], "Ship it")
        self.assertTrue(plugins.installed({"engine": "claude"}, "ponytail", self.root))
        # A login folder chosen in setup is read instead of the default one.
        self.assertEqual(plugins.commands({"engine": "claude", "claude_config_dir": str(self.root / "other")}), [])

    def test_disabled_claude_plugins_are_left_out(self):
        install_claude_ponytail(self.claude, enabled=False)
        self.assertEqual(plugins.commands({"engine": "claude"}), [])
        self.assertFalse(plugins.installed({"engine": "claude"}, "ponytail"))

    def test_codex_plugin_skills_are_sent_in_codex_syntax(self):
        plugin = self.codex / "plugins" / "cache" / "ponytail" / "ponytail" / "5.3.3"
        write(plugin / ".codex-plugin" / "plugin.json", json.dumps({"name": "ponytail", "skills": "./skills/"}))
        write(plugin / "skills" / "ponytail-review" / "SKILL.md", REVIEW_SKILL)
        available = plugins.commands({"engine": "codex"}, self.root)
        self.assertEqual([c["name"] for c in available], ["/ponytail-review"])
        self.assertEqual(plugins.translate("/ponytail-review staged", available), "$ponytail:ponytail-review staged")
        self.assertEqual(plugins.translate("/ponytail-review", available), "$ponytail:ponytail-review")
        self.assertEqual(plugins.translate("/other thing", available), "/other thing")
        self.assertTrue(plugins.installed({"engine": "codex"}, "ponytail"))


class TaskTelemetryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")})
        environment.start()
        self.addCleanup(environment.stop)

    def claude_usage(self, cost, output=100):
        return {
            "tokens": {"input_tokens": 1000, "output_tokens": output, "cache_read_input_tokens": 900},
            "reported_cost_usd": cost,
        }

    def test_claude_turns_add_up_cost_tokens_and_time(self):
        task = ponytail.Task(self.root / "my-app", "claude", "claude-opus-5-5")
        task.turn("Add rate limiting\nto the API", 30, self.claude_usage(0.25))
        task.turn("and tests", 90, self.claude_usage(0.40))
        task.turn("after a resume", 10, self.claude_usage(0.05))  # A new provider process counts from zero.
        self.assertEqual((task.turns, round(task.cost, 2), task.total, task.output), (3, 0.45, 6000, 300))
        self.assertEqual(task.name, "Add rate limiting to the API")
        self.assertEqual(
            dict(task.band()),
            {"ponytail": "off", "cost": "$0.45", "turns": "3", "agent": "2m 10s", "tokens": "6k", "tests": "not run"},
        )

    def test_codex_reports_thread_totals(self):
        task = ponytail.Task(self.root, "codex", "gpt-6.1")
        task.turn("one", 5, {"total": {"totalTokens": 5000, "outputTokens": 700}})
        task.turn("two", 5, {"total": {"totalTokens": 9000, "outputTokens": 900}})
        self.assertEqual((task.total, task.output), (9000, 900))
        self.assertNotIn("cost", dict(task.band()))

    def test_test_runs_fill_the_tests_cell(self):
        task = ponytail.Task(self.root, "claude", "m")
        task.command("a", "ls -la")
        task.command_output("a", "3 passed")
        self.assertEqual(task.tests, "not run")  # Not a test command.
        task.command("b", "python -m pytest -q")
        task.command_output("b", "===== 12 passed in 0.3s =====")
        self.assertEqual(task.tests, "12 passed")
        task.command("c", ".venv/bin/python -m unittest discover -s tests")
        task.command_output("c", "Ran 9 tests in 0.1s\n\nFAILED (failures=2)")
        self.assertEqual(task.tests, "2 failed")
        task.command("d", "npm test")
        task.command_output("d", "Ran 9 tests in 0.1s\n\nOK")
        self.assertEqual(task.tests, "9 passed")

    def test_ledger_row_matches_ponytails_format_and_is_replaced_each_turn(self):
        other = self.root / "config" / "ponytail" / "band" / "my-app.csv"
        write(
            other,
            ponytail.HEADER
            + "\nclaude-1234,2026-11-01 09:00,my-app,from claude code,1.00,2,1.0,10,5,,0,0,0,not run,full,m\n",
        )
        task = ponytail.Task(self.root / "my-app", "claude", "claude-opus-5-5")
        task.level = "full"
        task.turn('Say "hi", then stop', 60, self.claude_usage(0.10))
        ponytail.record(task)
        task.turn("again", 60, self.claude_usage(0.30))
        path = ponytail.record(task)
        lines = path.read_text().splitlines()
        self.assertEqual(lines[0], ponytail.HEADER)
        self.assertEqual(len(lines), 3)  # Claude Code's row is kept; Donut's row is replaced, not repeated.
        self.assertTrue(lines[1].startswith("claude-1234,"))
        import csv

        row = dict(zip(ponytail.HEADER.split(","), next(csv.reader([lines[2]]))))
        self.assertEqual(row["name"], 'Say "hi", then stop')
        self.assertEqual((row["cost_usd"], row["turns"], row["agent_minutes"]), ("0.30", "2", "2.0"))
        self.assertEqual((row["level"], row["model"], row["project"]), ("full", "claude-opus-5-5", "my-app"))

    def test_level_reads_the_project_flag_before_the_shared_one(self):
        import hashlib

        claude, project = self.root / "claude", self.root / "app"
        profile = {"engine": "claude", "claude_config_dir": str(claude)}
        self.assertEqual(ponytail.level(profile, project), "off")
        write(claude / ".ponytail-active", "ultra\n")
        self.assertEqual(ponytail.level(profile, project), "ultra")
        digest = hashlib.sha256(os.path.normpath(str(project)).encode()).hexdigest()
        write(claude / "ponytail-modes" / digest, "lite")
        self.assertEqual(ponytail.level(profile, project), "lite")


class TerminalPonytailTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "my-app"
        self.project.mkdir()
        claude = self.root / "claude"
        install_claude_ponytail(claude)
        write(claude / ".ponytail-active", "full")
        environment = patch.dict(
            os.environ,
            {
                "CLAUDE_CONFIG_DIR": str(claude),
                "CODEX_HOME": str(self.root / "codex"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        config = {"aliases": {"claude": {"engine": "claude", "model": "claude-opus-5-5"}}, "planner": "claude"}
        self.saved = []
        for item in (
            patch("donut.terminal.load", return_value=config),
            patch("donut.terminal.save", side_effect=self.saved.append),
            patch("donut.terminal.install"),
        ):
            item.start()
            self.addCleanup(item.stop)

    def make_app(self):
        from donut.terminal import Donut

        Donut.AUTO_REFRESH_QUOTAS = False
        app = Donut(self.project)
        self.calls = []

        class Fake:
            usage = None

            async def query(fake, text):
                self.calls.append(text)
                app.event("command", {"id": "t1", "command": "pytest -q"})
                app.event("command_output", {"id": "t1", "output": "4 passed"})
                app.event("text", "done")
                fake.usage = {"tokens": {"input_tokens": 2000, "output_tokens": 500}, "reported_cost_usd": 0.12}
                await asyncio.sleep(0)

            async def set_permission(fake, mode):
                pass

            async def close(fake):
                pass

        async def session(alias, profile, purpose=None, resume=None, conversation=None, **kwargs):
            key = app.session_key(alias, profile, purpose)
            if key not in app.sessions:
                fake = Fake()
                fake.task = ponytail.Task(app.project, profile.get("engine"), profile.get("model"))
                app.sessions[key] = fake
                app.session_profiles[key] = profile
            return app.sessions[key]

        app.session = session
        return app

    async def send(self, pilot, app, text):
        from donut.terminal import Draft

        draft = app.query_one(Draft)
        draft.clear()
        draft.insert(text)
        await pilot.press("enter")
        await pilot.pause(0.3)

    async def test_ponytail_commands_band_and_ledger_inside_donut(self):
        from donut.terminal import Draft, Menu

        app = self.make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            band = app.query_one("#band")
            self.assertTrue(band.display)
            self.assertIn("ponytail full", str(band.render()))
            app.query_one(Draft).insert("/ponytail-r")
            await pilot.pause()
            menu = app.query_one(Menu)
            self.assertEqual(menu.visible_rows[0][0], "/ponytail-review")  # Listed before any session is open.
            await self.send(pilot, app, "/ponytail-review staged")
            self.assertEqual(self.calls, ["/ponytail-review staged"])
            text = str(band.render())
            for piece in ("$0.12", "turns 1", "tokens 2.5k", "tests 4 passed"):
                self.assertIn(piece, text)
            ledger = self.root / "config" / "ponytail" / "band" / "my-app.csv"
            self.assertIn("/ponytail-review staged", ledger.read_text())
            await self.send(pilot, app, "/ponytail band off")
            self.assertFalse(band.display)
            self.assertEqual(self.calls, ["/ponytail-review staged"])  # The band switch stays inside Donut.
            await self.send(pilot, app, "/ponytail band")
            self.assertTrue(band.display)


if __name__ == "__main__":
    unittest.main()
