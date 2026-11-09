import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from donut import checkpoints, providers
from donut.tickets import Ticket, parse_dependencies, pick, queue_report


class TicketParsingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        for lane in ("to_do", "in_progress", "on_hold", "blocked", "done"):
            (self.project / "swim_lanes" / lane).mkdir(parents=True)

    def ticket(self, name, text, lane="to_do"):
        path = self.project / "swim_lanes" / lane / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_dependency_forms(self):
        self.assertEqual(parse_dependencies(None), [])
        self.assertEqual(parse_dependencies("None"), [])
        self.assertEqual(parse_dependencies("none."), [])
        self.assertEqual(parse_dependencies("None (first step)"), [])
        self.assertEqual(parse_dependencies("`a-slug`, `b.slug`"), ["a-slug", "b.slug"])
        self.assertEqual(parse_dependencies("a-slug, b_slug"), ["a-slug", "b_slug"])

    def test_missing_metadata_falls_back_and_warns(self):
        path = self.ticket("007_20240101-1200_fix-login.md", "# Fix login\n\nNo metadata at all.\n")
        ticket = Ticket(path, "to_do")
        self.assertEqual(ticket.slug, "fix-login")
        self.assertEqual(ticket.dependencies, [])
        self.assertEqual(len(ticket.warnings), 2)
        self.assertEqual(pick(self.project, set()), path)

    def test_bulleted_metadata_and_verify_field(self):
        path = self.ticket("001_a.md", "- **Slug:** `a`\n- **Depends on:** None\n- **Verify:** `pytest -q`\n")
        ticket = Ticket(path, "to_do")
        self.assertEqual((ticket.slug, ticket.verify), ("a", "pytest -q"))
        self.assertEqual(Ticket(self.ticket("002_b.md", "**Slug:** `b`\n**Verify:** none\n"), "to_do").verify, "")

    def test_malformed_ticket_does_not_stop_other_work(self):
        self.ticket("001_broken.md", "**Slug:** `broken`\n**Depends on:** `ghost`\n")
        self.ticket("002_held.md", "**Slug:** `held`\n**Depends on:** None\n", lane="on_hold")
        self.ticket("003_waits.md", "**Slug:** `waits`\n**Depends on:** `held`\n")
        self.ticket("004_later.md", "**Slug:** `later`\n**Depends on:** `fine`\n")
        fine = self.ticket("005_fine.md", "**Slug:** `fine`\n**Depends on:** None\n")
        self.ticket("006_nometa.md", "nothing\n", lane="done")
        self.assertEqual(pick(self.project, set()), fine)
        report = "\n".join(queue_report(self.project))
        self.assertIn("001_broken.md: depends on unknown slug `ghost`", report)
        self.assertIn("003_waits.md: depends on `held`, which is on hold or blocked", report)
        self.assertIn("004_later.md: waiting for `fine` to reach done/", report)
        self.assertIn("done/006_nometa.md: missing **Slug:**", report)
        self.assertNotIn("005_fine.md", report)
        self.assertNotIn("002_held.md:", report.replace("done/006", ""))


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)

    def test_outside_git_is_reported_not_raised(self):
        self.assertFalse(checkpoints.is_repository(self.project))
        self.assertIsNone(checkpoints.changed_files(self.project))
        with self.assertRaises(ValueError):
            checkpoints.commit(self.project, "x")

    def test_commit_records_changes(self):
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        env = dict(
            os.environ,
            GIT_CONFIG_GLOBAL=str(self.project / "no-gitconfig"),
            GIT_CONFIG_SYSTEM=str(self.project / "no-gitconfig"),
        )
        with patch.dict(os.environ, env):
            self.assertIsNone(checkpoints.commit(self.project, "nothing"))
            (self.project / "a.txt").write_text("one\n")
            self.assertEqual(checkpoints.changed_files(self.project), ["a.txt"])
            first = checkpoints.commit(self.project, "donut: done 001_a.md")
            self.assertTrue(first)
            self.assertEqual(checkpoints.changed_files(self.project), [])
            log = subprocess.run(["git", "log", "--format=%s"], cwd=self.project, capture_output=True, text=True)
        self.assertEqual(log.stdout.split("\n")[0], "donut: done 001_a.md")


class OllamaTests(unittest.TestCase):
    def test_base_url_follows_ollama_host(self):
        for value, expected in (
            ("", "http://127.0.0.1:11434/v1"),
            ("0.0.0.0", "http://0.0.0.0:11434/v1"),
            ("gpu-box:11435", "http://gpu-box:11435/v1"),
            ("https://ollama.example.com:443/", "https://ollama.example.com:443/v1"),
        ):
            with patch.dict(os.environ, {"OLLAMA_HOST": value}):
                self.assertEqual(providers.ollama_base_url(), expected)

    def test_ollama_spinner_never_reaches_output_and_errors_are_readable(self):
        import subprocess

        with patch("donut.providers.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "\u280b \u2819 \u2839 ")
            providers.activate({"engine": "ollama", "model": "m"})
            self.assertTrue(run.call_args.kwargs["capture_output"])
            run.return_value = subprocess.CompletedProcess(
                [], 1, "", "\u280b \x1b[?25l\u2819 \nError: model 'm' not found\n"
            )
            with self.assertRaises(ValueError) as caught:
                providers.activate({"engine": "ollama", "model": "m"})
        self.assertEqual(str(caught.exception), "ollama run failed: Error: model 'm' not found")

    def test_other_models_are_only_unloaded_when_asked(self):
        with patch("donut.providers.subprocess.run") as run:
            run.return_value.stdout = "NAME ID\nother:7b x\nmine:7b y\n"
            run.return_value.returncode = 0
            providers.activate({"engine": "ollama", "model": "mine:7b"})
            commands = [call.args[0][:2] for call in run.call_args_list]
            self.assertEqual(commands, [["ollama", "run"]])
            run.reset_mock()
            providers.activate({"engine": "ollama", "model": "mine:7b", "unload_others": True})
            commands = [call.args[0][:2] for call in run.call_args_list]
            self.assertEqual(commands, [["ollama", "ps"], ["ollama", "stop"], ["ollama", "run"]])
            self.assertEqual(run.call_args_list[1].args[0], ["ollama", "stop", "other:7b"])


if __name__ == "__main__":
    unittest.main()


class PermissionModeTests(unittest.TestCase):
    def build(self, profile, permission=None):
        with patch("shutil.which", return_value="/bin/agent"), patch.dict(os.environ, {"KEY": "x"}):
            args, _ = providers.command(profile, "task", permission=permission)
        return args

    def test_each_mode_uses_the_providers_own_mechanism(self):
        claude = {"engine": "claude", "model": "x"}
        codex = {"engine": "codex", "model": "x"}
        qwen = {"engine": "ollama", "model": "x"}
        self.assertIn("auto", self.build(claude, "auto")[self.build(claude, "auto").index("--permission-mode") + 1])
        self.assertIn("--dangerously-skip-permissions", self.build(claude, "full"))
        args = self.build(claude, "accept_edits")
        self.assertEqual(args[args.index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("--approve-for-me", self.build(codex, "auto"))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", self.build(codex, "full"))
        args = self.build(codex, "accept_edits")
        self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
        args = self.build(qwen, "auto")
        self.assertEqual(args[args.index("--approval-mode") + 1], "auto")
        self.assertIn("--yolo", self.build(qwen, "full"))

    def test_unattended_runs_never_wait_for_a_prompt(self):
        self.assertEqual(providers.batch_permission({"permission_mode": "ask"}), "accept_edits")
        self.assertEqual(providers.batch_permission({"auto_approve": True}), "full")
        self.assertEqual(providers.batch_permission({}), "accept_edits")
        self.assertEqual(providers.batch_permission({"permission_mode": "auto", "auto_approve": True}), "auto")
        with self.assertRaises(ValueError):
            providers.validate({"engine": "claude", "permission_mode": "yolo"})


class CodexPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_thread_and_turn_carry_native_policies(self):
        import asyncio
        from donut.sessions import CodexSession

        session = CodexSession(Path("."), {}, lambda *event: None, None)
        sent = []

        async def request(method, params=None):
            sent.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "t"}}
            session.turn_done.set_result(None)
            return {}

        session.request = request
        session.permission = "auto"
        await asyncio.wait_for(session.query("one"), 2)
        start = sent[0][1]
        self.assertEqual(
            (start["approvalPolicy"], start["sandbox"], start["approvalsReviewer"]),
            ("on-request", "workspace-write", "auto_review"),
        )
        self.assertNotIn("approvalPolicy", sent[1][1])
        await session.set_permission("full")
        await asyncio.wait_for(session.query("two"), 2)
        turn = sent[2][1]
        self.assertEqual(turn["approvalPolicy"], "never")
        self.assertEqual(turn["sandboxPolicy"], {"type": "dangerFullAccess"})
        self.assertEqual(turn["approvalsReviewer"], "user")
        await asyncio.wait_for(session.query("three"), 2)
        self.assertNotIn("approvalPolicy", sent[3][1])

    async def test_session_wide_approval_maps_to_codex_decision(self):
        from donut.sessions import CodexSession

        replies = []

        async def approve(name, details):
            return "always"

        session = CodexSession(Path("."), {}, lambda *event: None, approve)

        async def send(message):
            replies.append(message)

        session.send = send
        await session.handle_request(
            {"id": 7, "method": "item/commandExecution/requestApproval", "params": {"command": "ls"}}
        )
        self.assertEqual(replies, [{"id": 7, "result": {"decision": "acceptForSession"}}])


class ReadOnlyProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_sessions_use_each_providers_read_only_mode(self):
        from donut.sessions import ClaudeSession, CodexSession, QwenSession

        codex = CodexSession(Path("."), {}, lambda *e: None, None)
        codex.read_only = True
        codex.permission = "full"
        self.assertEqual(codex.policy(), ("on-request", "read-only", "user"))
        await codex.set_permission("full")
        self.assertFalse(codex.permission_changed)
        claude = ClaudeSession(Path("."), {}, lambda *e: None, None)
        claude.read_only = True
        await claude.set_permission("full")  # Ignored: no live switch away from plan mode.
        qwen = QwenSession(Path("."), {"engine": "ollama", "model": "m"}, lambda *e: None, None)
        qwen.read_only = True
        with patch("shutil.which", return_value="/bin/qwen"):
            args, _ = providers.command(
                qwen.profile, "x", permission=qwen.effective_permission(), review=qwen.read_only
            )
        self.assertEqual(args[args.index("--approval-mode") + 1], "plan")
