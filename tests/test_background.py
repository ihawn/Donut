import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from donut import background, platform


class ParsingTests(unittest.TestCase):
    def test_wait_line_and_notes(self):
        answer = "Started the deploy.\n\nDONUT-WAIT: `gh run watch 42`\n"
        self.assertEqual(background.wait_request(answer), "gh run watch 42")
        self.assertIsNone(background.wait_request("No waiting here. DONUT-WAIT: inside a sentence"))
        self.assertIn("exited with code 3", background.wake_prompt("make", 3, "boom"))
        self.assertIn("timed out", background.wake_prompt("make", None, ""))
        note = background.shell_note([("git status", 0, "clean")])
        self.assertIn("$ git status\nclean\n(exit 0)", note)
        self.assertTrue(background.tail("x" * 5000).startswith("…"))

    def test_native_clipboard_tools(self):
        with patch("donut.platform.subprocess.run") as run:
            self.assertTrue(platform.copy_native("hé", "darwin", lambda name: name == "pbcopy"))
            self.assertEqual(run.call_args.args[0], ["pbcopy"])
            self.assertEqual(run.call_args.kwargs["input"], "hé".encode())
            self.assertTrue(platform.copy_native("hé", "win32", lambda name: name == "clip"))
            self.assertEqual(run.call_args.kwargs["input"], "hé".encode("utf-16-le"))
            self.assertTrue(platform.copy_native("x", "linux", lambda name: name == "xclip"))
            self.assertEqual(run.call_args.args[0], ["xclip", "-selection", "clipboard"])
            self.assertFalse(platform.copy_native("x", "linux", lambda name: False))


class WaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_returns_code_and_output(self):
        with tempfile.TemporaryDirectory() as temp:
            code, output = await background.wait_for(f'"{sys.executable}" -c "print(41 + 1)"', temp)
            self.assertEqual((code, output.strip()), (0, "42"))
            code, _ = await background.wait_for(f'"{sys.executable}" -c "import time; time.sleep(5)"', temp, 0.3)
            self.assertIsNone(code)


class ClaudeWakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_turns_claude_starts_on_its_own_are_shown_as_wake_ups(self):
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
        from donut.sessions import ClaudeSession

        events = []
        session = ClaudeSession(Path("."), {}, lambda kind, data: events.append((kind, data)), None)
        stream = asyncio.Queue()

        class Client:
            async def receive_messages(self):
                while True:
                    yield await stream.get()

            async def query(self, text):
                await stream.put(AssistantMessage([TextBlock("asked: " + text)], "m"))
                await stream.put(ResultMessage("success", 1, 1, False, 1, "sid"))

        session.client, session.inbox, session.in_turn = Client(), asyncio.Queue(), False
        session.pump = asyncio.create_task(session._pump())
        await asyncio.wait_for(session._query("build it"), 2)
        self.assertEqual(events, [("text", "asked: build it")])
        events.clear()
        # The background build finishes later and Claude Code starts a turn by itself.
        await stream.put(AssistantMessage([TextBlock("The build passed.")], "m"))
        await stream.put(AssistantMessage([TextBlock("Opening the PR.")], "m"))
        await stream.put(ResultMessage("success", 1, 1, False, 1, "sid"))
        for _ in range(50):
            if events and events[-1][0] == "wake_end":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(
            events,
            [
                ("wake_start", ""),
                ("text", "The build passed."),
                ("break", ""),
                ("text", "Opening the PR."),
                ("wake_end", ""),
            ],
        )
        session.pump.cancel()


class ClaudeBackgroundTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_shells_are_reported_while_they_run(self):
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            TaskNotificationMessage,
            TaskStartedMessage,
            TaskUpdatedMessage,
            TextBlock,
        )
        from donut.sessions import ClaudeSession

        events = []
        session = ClaudeSession(Path("."), {}, lambda kind, data: events.append((kind, data)), None)
        stream = asyncio.Queue()

        class Client:
            async def receive_messages(self):
                while True:
                    yield await stream.get()

            async def query(self, text):
                await stream.put(
                    TaskStartedMessage("task_started", {}, "t1", "Poll the build", "u", "s", "b1", "local_bash")
                )
                await stream.put(AssistantMessage([TextBlock("I started the build.")], "m"))
                await stream.put(ResultMessage("success", 1, 1, False, 1, "sid"))

        session.client, session.inbox, session.in_turn = Client(), asyncio.Queue(), False
        session.pump = asyncio.create_task(session._pump())
        await asyncio.wait_for(session._query("run it"), 2)
        self.assertIn(
            ("task", {"id": "t1", "state": "running", "kind": "shell", "description": "Poll the build"}), events
        )
        events.clear()
        # The turn is over; the shell finishes later.
        await stream.put(TaskUpdatedMessage("task_updated", {}, "t1", {"status": "completed"}))
        await stream.put(
            TaskNotificationMessage("task_notification", {}, "t1", "completed", "/tmp/o", "Run succeeded", "u", "s")
        )
        for _ in range(50):
            if len(events) >= 3:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(events[0], ("task", {"id": "t1", "state": "completed"}))
        self.assertIn(("background", "Run succeeded"), events)
        session.pump.cancel()


if __name__ == "__main__":
    unittest.main()
