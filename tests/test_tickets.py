import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from donut import checkpoints, providers
from donut import runner, tickets
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

    def test_next_and_prev_list_tickets_with_overview_and_pull_requests(self):
        lanes = self.project / "swim_lanes"
        body = "# {title}\n\n{about}\n\n**Slug:** `{slug}`\n**Depends on:** None\n\n## Goal\nx\n\n## Completion notes\n"
        for number, lane in ((1, "done"), (2, "blocked"), (3, "done"), (4, "in_progress"), (5, "to_do"), (6, "to_do")):
            text = body.format(title=f"Ticket {number}", about=f"Does thing {number} | safely.", slug=f"t{number}")
            self.ticket(f"00{number}_t{number}.md", text, lane)
        done = lanes / "done" / "003_t3.md"
        done.write_text(done.read_text() + "- PR: https://github.com/o/r/pull/7\n")  # Written by the worker.
        tickets.add_pull_requests(done, ["https://github.com/o/r/pull/7", "https://github.com/o/r/pull/8"])
        self.assertEqual(
            Ticket(done, "done").pull_requests, ["https://github.com/o/r/pull/7", "https://github.com/o/r/pull/8"]
        )
        self.assertEqual([t.name[:3] for t in tickets.upcoming(self.project)], ["004", "005", "006"])
        self.assertEqual([t.name[:3] for t in tickets.upcoming(self.project, count=2)], ["004", "005"])
        self.assertEqual([t.name[:3] for t in tickets.finished(self.project)], ["003", "002", "001"])
        self.assertEqual([t.name[:3] for t in tickets.finished(self.project, count=1)], ["003"])
        table = tickets.table(tickets.finished(self.project, count=2)).splitlines()
        self.assertEqual(table[0], "| # | Ticket | Status | Overview | PR |")
        self.assertEqual(
            table[2],
            "| 003 | Ticket 3 | done | Does thing 3 / safely. "
            "| https://github.com/o/r/pull/7 https://github.com/o/r/pull/8 |",
        )
        self.assertEqual(table[3], "| 002 | Ticket 2 | blocked | Does thing 2 / safely. | - |")

    def test_a_worker_may_use_git_when_its_ticket_asks_but_never_on_main(self):
        path = self.ticket("001_t1.md", "# T\n\n**Slug:** `t1`\n**Depends on:** None\n")
        with patch("donut.runner.worker_skill", return_value=self.project / "skills"):
            prompt = runner.worker_prompt(self.project, path)
        self.assertNotIn("Do not work another ticket, delegate, commit, push", prompt)
        self.assertIn("only when the ticket asks for it, you may create a branch, commit", prompt)
        self.assertIn("Never commit on or push to the default branch (main or master)", prompt)
        self.assertIn("`PR: <url>`", prompt)

    def test_the_review_sees_work_the_worker_committed(self):
        def git(*args):
            return checkpoints._git(self.project, *args)

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        (self.project / "app.py").write_text("print(1)\n")
        git("add", "-A")
        git("commit", "-q", "-m", "start")
        before = checkpoints.heads(self.project)
        git("switch", "-q", "-c", "feature/two")
        (self.project / "app.py").write_text("print(2)\n")
        git("commit", "-q", "-am", "two")  # Nothing is left uncommitted.
        self.assertEqual(checkpoints.changed_files(self.project), [])
        self.assertEqual(checkpoints.committed_files(self.project, before), ["app.py"])
        self.assertIn("+print(2)", checkpoints.diff(self.project, ["app.py"], before=before))
        self.assertEqual(checkpoints.diff(self.project, ["app.py"]), "")  # Against HEAD alone it would be empty.

    def test_a_ticket_with_unticked_checks_is_not_plain_done_and_others_files_are_not_its_own(self):
        text = (
            "# T\n\n**Slug:** `t1`\n**Depends on:** None\n\n## Files\n\n"
            "- Modify: `api/app.py`, `api/tests/test_app.py` (in the worktree).\n"
            "- Do not touch: `api/legacy.py`\n\n## Acceptance criteria\n\n"
            "- [x] Unit tests pass.\n- [ ] Live check passes.\n- [ ] **Human check**: looks right.\n\n"
            "## Completion notes\n- NEEDS HUMAN: allow a second login today?\n"
        )
        ticket = Ticket(self.ticket("001_t1.md", text, "blocked"), "blocked")
        self.assertEqual(ticket.scope, ["api/app.py", "api/tests/test_app.py"])
        self.assertEqual(ticket.unticked, ["Live check passes."])  # The Human check stays the user's.
        self.assertEqual(ticket.needs_human, ["allow a second login today?"])
        for repo in ("api", "other"):
            (self.project / repo).mkdir()
            checkpoints._git(self.project / repo, "init", "-q")
        changed = ["api/app.py", "api/helper.py", "api/__pycache__/app.cpython-313.pyc", "other/notes.py"]
        mine, others = runner.ticket_files(self.project, ticket.scope, changed)
        self.assertEqual(mine, ["api/app.py", "api/helper.py"])  # Same repository: the worker's, though unlisted.
        self.assertEqual(others, ["other/notes.py"])  # Another repository: someone else's. Bytecode is dropped.
        self.assertEqual(runner.ticket_files(self.project, ["nothing/matches.py"], changed)[1], [])
        self.assertEqual(
            runner.ticket_files(self.project, [], changed)[0], ["api/app.py", "api/helper.py", "other/notes.py"]
        )
        prompt = runner.review_prompt(self.project, ticket.path, mine, "", others)
        self.assertIn("outside the ticket's listed files:\n  other/notes.py", prompt)
        self.assertIn("not part of this submission", prompt)

    def test_the_planner_can_finish_a_small_remainder_itself(self):
        log = self.project / "recover.log"
        log.write_text(
            "I reran the test from the root.\nRECOVERY: FIXED: reran the tests from the repo root, 12 pass.\n"
        )
        self.assertEqual(runner.read_recovery(log), ("fixed", "reran the tests from the repo root, 12 pass"))
        path = self.ticket("001_t1.md", "# T\n\n**Slug:** `t1`\n**Depends on:** None\n", "blocked")
        with patch("donut.runner.worker_skill", return_value=self.project / "skills"):
            prompt = runner.recovery_prompt(self.project, path, "why", "output", [])
            worker = runner.worker_prompt(self.project, path)
        self.assertIn("RECOVERY: FIXED", prompt)
        self.assertIn("consents.md", prompt)
        self.assertIn("consents.md", worker)
        self.assertIn("Never open a pull request while a ticket is in swim_lanes/blocked/", worker)

    def test_long_completion_notes_are_shortened_in_the_run_records(self):
        self.assertEqual(runner.brief("short notes"), "short notes")
        long = "BLOCKED: the login failed. " + "x" * 9000 + " Latest addition."
        cut = runner.brief(long)
        self.assertLess(len(cut), 2600)
        self.assertTrue(cut.startswith("BLOCKED: the login failed."))  # The reason is at the start.
        self.assertTrue(cut.endswith("Latest addition."))
        self.assertIn("notes shortened", cut)

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
