import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from donut.terminal import Donut, Draft

Donut.AUTO_REFRESH_QUOTAS = False


class Provider:
    """Stands in for a provider session: answers, records prompts, and can switch models when allowed."""

    made = []
    info = {}

    def __init__(self, project, profile, emit, approve):
        self.profile, self.emit = dict(profile), emit
        self.prompts, self.switched, self.closed = [], [], False
        Provider.made.append(self)

    async def connect(self):
        pass

    async def query(self, text):
        self.prompts.append(text)
        self.emit("text", f"{self.profile.get('model')} answered")
        await asyncio.sleep(0)

    async def switch_model(self, model):
        self.switched.append(model)
        self.profile["model"] = model

    async def set_permission(self, mode):
        pass

    async def close(self):
        self.closed = True

    async def models(self):
        return []


class FakeClaude(Provider):
    pass


class FakeCodex(Provider):
    pass


class FakeQwen(Provider):
    pass  # Donut never switches Qwen in place; it hands the conversation over.


class SwitchSetup(unittest.IsolatedAsyncioTestCase):
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
        self.aliases = {
            "claude": {"engine": "claude", "model": "opus"},
            "codex": {"engine": "codex", "model": "gpt-6.1"},
            "local": {"engine": "ollama", "model": "qwen3.6"},
        }
        config = {"aliases": self.aliases, "planner": "claude"}
        Provider.made = []
        for item in (
            patch("donut.terminal.load", return_value=config),
            patch("donut.terminal.install"),
            patch("donut.terminal.ClaudeSession", FakeClaude),
            patch("donut.terminal.CodexSession", FakeCodex),
            patch("donut.terminal.QwenSession", FakeQwen),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.app = Donut(root)

    async def send(self, pilot, text):
        draft = self.app.query_one(Draft)
        draft.clear()
        draft.insert(text)
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.02)
            if not self.app.busy:
                return

    def use(self, alias, model=None):
        profile = dict(self.aliases[alias], **({"model": model} if model else {}))
        self.app.selected["plan"], self.app.profiles["plan"] = alias, profile


class ModelSwitchTests(SwitchSetup):
    async def test_a_switched_model_keeps_the_whole_conversation(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await self.send(pilot, "remember the number 7")
            claude = Provider.made[-1]
            # Another Claude model on the same login: the same live session switches, with its full context.
            self.use("claude", "sonnet")
            await self.send(pilot, "what number did I say?")
            self.assertIs(Provider.made[-1], claude)
            self.assertEqual(claude.switched, ["sonnet"])
            self.assertNotIn("<conversation>", claude.prompts[-1])
            # Another provider: Donut hands the conversation so far to the new model, then retires the old one.
            self.use("codex")
            await self.send(pilot, "and now?")
            codex = Provider.made[-1]
            self.assertIsInstance(codex, FakeCodex)
            handoff = codex.prompts[0]
            self.assertIn("This conversation started before you joined it", handoff)
            self.assertIn("[plan mode, opus]\nUser: remember the number 7\nAssistant: opus answered", handoff)
            self.assertIn("[plan mode, sonnet]\nUser: what number did I say?\nAssistant: sonnet answered", handoff)
            self.assertLess(handoff.index("</conversation>"), handoff.index("and now?"))  # History, then the request.
            self.assertTrue(claude.closed)
            self.assertNotIn(claude, self.app.sessions.values())
            await self.send(pilot, "one more")
            self.assertNotIn("<conversation>", codex.prompts[-1])  # Handed over once.
            # And on to a local model, which gets everything, including what Codex said.
            self.use("local")
            await self.send(pilot, "local now")
            qwen = Provider.made[-1]
            self.assertIsInstance(qwen, FakeQwen)
            self.assertIn("[plan mode, gpt-6.1]\nUser: and now?\nAssistant: gpt-6.1 answered", qwen.prompts[0])
            self.assertIn("User: remember the number 7", qwen.prompts[0])

    async def test_every_mode_sees_what_the_others_did(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await self.send(pilot, "plan a thing")
            planner = Provider.made[-1]
            self.app.selected["review"], self.app.profiles["review"] = "codex", dict(self.aliases["codex"])
            self.app.reviewer = "chosen"
            await self.send(pilot, "/review")
            await self.send(pilot, "check the login change")
            reviewer = Provider.made[-1]
            self.assertIn("[plan mode, opus]\nUser: plan a thing", reviewer.prompts[0])
            self.app.selected["solo"], self.app.profiles["solo"] = "local", dict(self.aliases["local"])
            await self.send(pilot, "/solo")
            await self.send(pilot, "one sentence on what was just checked")
            solo = Provider.made[-1]
            self.assertIn("[review mode, gpt-6.1]\nUser: check the login change", solo.prompts[0])
            self.assertIn("[plan mode, opus]", solo.prompts[0])
            # Back in plan mode, the planner is told only what it missed, never its own turn again.
            await self.send(pilot, "/plan")
            await self.send(pilot, "carry on")
            latest = planner.prompts[-1]
            self.assertIn("since your last turn", latest)
            self.assertIn("check the login change", latest)
            self.assertIn("one sentence on what was just checked", latest)
            self.assertNotIn("plan a thing", latest)
            await self.send(pilot, "and again")
            self.assertNotIn("<conversation>", planner.prompts[-1])  # Nothing new since.


class RealCodexThroughDonutTests(SwitchSetup):
    async def test_a_codex_reviewer_starts_its_own_thread(self):
        """Donut's own bookkeeping must never touch the Codex thread id (it once set .thread to "review")."""
        from donut import sessions

        sent = []

        async def request(session, method, params=None):
            sent.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "0b6f5c1e-1d4e-4c62-9a8e-2f1f6f6d7a10"}}
            session.turn_done.set_result(None)
            return {}

        async def connect(session):
            pass

        with (
            patch("donut.terminal.CodexSession", sessions.CodexSession),
            patch.object(sessions.CodexSession, "connect", connect),
            patch.object(sessions.CodexSession, "request", request),
        ):
            self.app.selected["review"], self.app.profiles["review"] = "codex", dict(self.aliases["codex"])
            self.app.reviewer = "chosen"
            async with self.app.run_test() as pilot:
                await pilot.pause()
                await self.send(pilot, "/review")
                await self.send(pilot, "https://github.com/x/y/pull/7")
                await self.send(pilot, "and again")
        methods = [method for method, _ in sent]
        self.assertEqual(methods.count("thread/start"), 1)
        turns = [params for method, params in sent if method == "turn/start"]
        self.assertEqual({turn["threadId"] for turn in turns}, {"0b6f5c1e-1d4e-4c62-9a8e-2f1f6f6d7a10"})


class CodexSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_switches_model_on_the_next_turn_of_the_same_thread(self):
        from donut.sessions import CodexSession

        session = CodexSession(Path("."), {"model": "gpt-6.1"}, lambda *event: None, None)
        sent = []

        async def request(method, params=None):
            sent.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "t"}}
            session.turn_done.set_result(None)
            return {}

        session.request = request
        await asyncio.wait_for(session.query("one"), 2)
        await session.switch_model("gpt-6.1-mini")
        await asyncio.wait_for(session.query("two"), 2)
        await asyncio.wait_for(session.query("three"), 2)
        turns = [params for method, params in sent if method == "turn/start"]
        self.assertNotIn("model", turns[0])
        self.assertEqual(turns[1]["model"], "gpt-6.1-mini")
        self.assertEqual(turns[1]["threadId"], "t")  # The same thread, so its context stays.
        self.assertNotIn("model", turns[2])  # Codex keeps an override for later turns.


if __name__ == "__main__":
    unittest.main()
