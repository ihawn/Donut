import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from donut import effort, providers

CLAUDE_INFO = {
    "models": [
        {
            "value": "opus",
            "displayName": "Opus",
            "supportsEffort": True,
            "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        },
        {
            "value": "sonnet",
            "displayName": "Sonnet",
            "supportsEffort": True,
            "supportedEffortLevels": ["low", "medium", "high", "max"],
        },
        {"value": "haiku", "displayName": "Haiku", "supportsEffort": False},
    ]
}
CODEX_MODELS = [
    {
        "id": "gpt-6.1",
        "model": "gpt-6.1",
        "isDefault": True,
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "Fast responses with lighter reasoning"},
            {"reasoningEffort": "medium", "description": "Balances speed and reasoning depth"},
            {"reasoningEffort": "high", "description": "Greater reasoning depth for complex problems"},
        ],
    }
]


class OptionTests(unittest.TestCase):
    def test_claude_uses_each_models_own_levels_and_words(self):
        term, levels, default = effort.claude_options(CLAUDE_INFO, "opus")
        self.assertEqual((term, default), ("Effort", "high"))
        self.assertEqual([value for value, _ in levels], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(dict(levels)["high"], "Deep reasoning (default)")
        self.assertNotIn("xhigh", dict(effort.claude_options(CLAUDE_INFO, "sonnet")[1]))
        self.assertEqual(effort.claude_options(CLAUDE_INFO, "haiku")[1], [])  # No effort setting.
        self.assertEqual(len(effort.claude_options({}, "claude-opus-5-5")[1]), 5)  # Unlisted: every level.

    def test_codex_uses_its_reasoning_effort_list(self):
        term, levels, default = effort.codex_options(CODEX_MODELS, "gpt-6.1")
        self.assertEqual((term, default), ("Reasoning effort", "medium"))
        self.assertEqual(levels[2], ("high", "Greater reasoning depth for complex problems"))
        self.assertEqual(effort.codex_options(CODEX_MODELS, "")[1], levels)  # Codex's default model.

    def test_workers_get_effort_on_their_command_line(self):
        with patch("shutil.which", return_value="/bin/agent"), patch.dict(os.environ, {"KEY": "x"}):
            claude, _ = providers.command({"engine": "claude", "model": "opus", "effort": "max"}, "task")
            codex, _ = providers.command({"engine": "codex", "model": "gpt-6.1", "effort": "high"}, "task")
            qwen, _ = providers.command({"engine": "ollama", "model": "q", "effort": "high"}, "task")
        self.assertEqual(claude[claude.index("--effort") + 1], "max")
        self.assertEqual(codex[codex.index("-c") + 1], 'model_reasoning_effort="high"')
        self.assertNotIn("--effort", qwen)
        with self.assertRaises(ValueError):
            providers.validate({"engine": "claude", "effort": "high; rm -rf"})


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_changes_effort_in_the_live_session(self):
        from donut.sessions import ClaudeSession

        sent = []

        class Query:
            async def _send_control_request(self, request):
                sent.append(request)

        session = ClaudeSession(Path("."), {"model": "opus"}, lambda *e: None, None)
        session.client = type("Client", (), {"_query": Query()})()
        await session._set_effort("max")
        self.assertEqual(sent, [{"subtype": "apply_flag_settings", "settings": {"effortLevel": "max"}}])

    async def test_codex_sends_effort_on_the_next_turn(self):
        from donut.sessions import CodexSession

        session = CodexSession(Path("."), {"model": "gpt-6.1"}, lambda *e: None, None)
        sent = []

        async def request(method, params=None):
            sent.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "t"}}
            session.turn_done.set_result(None)
            return {}

        session.request = request
        await asyncio.wait_for(session.query("one"), 2)
        await session.set_effort("high")
        await asyncio.wait_for(session.query("two"), 2)
        await asyncio.wait_for(session.query("three"), 2)
        turns = [params for method, params in sent if method == "turn/start"]
        self.assertNotIn("effort", turns[0])
        self.assertEqual(turns[1]["effort"], "high")
        self.assertNotIn("effort", turns[2])  # Codex keeps it for later turns.


class TerminalEffortTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        environment = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "CLAUDE_CONFIG_DIR": str(root / "claude"),
                "CODEX_HOME": str(root / "codex"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        from donut.config import save
        from donut.terminal import Donut

        config = {
            "aliases": {
                "claude": {"engine": "claude", "model": "opus"},
                "codex": {"engine": "codex", "model": "gpt-6.1"},
                "local": {"engine": "ollama", "model": "qwen3.6"},
            },
            "planner": "claude",
            "default_model": "local",
            "roles": {"solo": {"alias": "codex", "model": "gpt-6.1"}},
        }
        save(config)
        Donut.AUTO_REFRESH_QUOTAS = False
        self.patch = patch("donut.terminal.install")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.app = Donut(root)
        self.applied = []
        app = self.app

        class Fake:
            info = CLAUDE_INFO

            def __init__(fake, profile):
                fake.profile = profile

            async def set_effort(fake, level):
                self.applied.append((fake.profile.get("model"), level))

            async def model_details(fake):
                return CODEX_MODELS

            async def close(fake):
                pass

        async def session(alias, profile, purpose=None, **kwargs):
            key = app.session_key(alias, profile, purpose)
            app.sessions.setdefault(key, Fake(profile))
            app.session_profiles[key] = profile
            return app.sessions[key]

        app.session = session

    async def send(self, pilot, text):
        from donut.terminal import Draft

        draft = self.app.query_one(Draft)
        draft.clear()
        draft.insert(text)
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause(0.02)
            if not self.app.busy:
                return

    async def test_effort_in_each_providers_terms(self):
        from donut.config import load
        from donut.terminal import Menu, RichLog

        async with self.app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.send(pilot, "/effort")
            menu = self.app.query_one(Menu)
            self.assertEqual([row[0] for row in menu.visible_rows], ["low", "medium", "high", "xhigh", "max"])
            self.assertIn("Effort for opus", str(menu.border_title))
            await pilot.press("escape")
            await pilot.pause()
            await self.send(pilot, "/effort max")
            self.assertEqual(self.app.profiles["plan"]["effort"], "max")
            self.assertEqual(self.applied, [("opus", "max")])
            self.assertIn("planner opus · max", str(self.app.query_one("#status").render()))
            self.assertEqual(load()["roles"]["plan"]["effort"], "max")
            # Codex: its own term and levels.
            await self.send(pilot, "/solo")
            await self.send(pilot, "/effort")
            menu = self.app.query_one(Menu)
            self.assertIn("Reasoning effort for gpt-6.1 · now medium", str(menu.border_title))
            self.assertEqual([row[0] for row in menu.visible_rows], ["low", "medium", "high"])
            await pilot.press("escape")
            await pilot.pause()
            await self.send(pilot, "/effort xhigh")  # Not a Codex level for this model.
            body = "\n".join(strip.text for strip in self.app.query_one(RichLog).lines)
            self.assertIn("gpt-6.1 takes reasoning effort low, medium, high", body)
            # A local model through Qwen Code has no such setting.
            self.app.selected["solo"], self.app.profiles["solo"] = "local", {"engine": "ollama", "model": "qwen3.6"}
            await self.send(pilot, "/effort")
            body = "\n".join(strip.text for strip in self.app.query_one(RichLog).lines)
            self.assertIn("qwen3.6 has no effort setting Donut can change.", body)


if __name__ == "__main__":
    unittest.main()


class ClaudeLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_sessions_can_switch_to_full_access_later_but_review_cannot(self):
        import claude_agent_sdk
        from donut.sessions import ClaudeSession

        seen = []

        class Client:
            def __init__(self, options):
                seen.append(options)

            async def connect(self):
                pass

            async def get_server_info(self):
                return {}

            async def receive_messages(self):
                return
                yield

        for read_only in (False, True):
            session = ClaudeSession(Path("."), {"model": "opus"}, lambda *e: None, None)
            session.read_only = read_only
            with patch.object(claude_agent_sdk, "ClaudeSDKClient", Client):
                await session._connect()
            session.pump.cancel()
        self.assertEqual(seen[0].extra_args, {"allow-dangerously-skip-permissions": None})
        self.assertEqual(seen[1].extra_args, {})  # Review stays read-only.
        self.assertEqual(seen[1].permission_mode, "plan")


class PermissionErrorTests(TerminalEffortTests):
    test_effort_in_each_providers_terms = None  # Only the setup is shared.

    async def test_a_refused_switch_is_reported_once(self):
        from donut.terminal import RichLog

        class Refuses:
            async def set_permission(self, mode):
                raise RuntimeError("Cannot set permission mode to bypassPermissions")

        async with self.app.run_test() as pilot:
            await pilot.pause()
            self.app.sessions = {("a", "x"): Refuses(), ("b", "y"): Refuses(), ("c", "z"): Refuses()}
            await self.app.set_permission("full")
            body = "\n".join(strip.text for strip in self.app.query_one(RichLog).lines)
            self.assertEqual(body.count("Could not change the provider's permission mode"), 1)
