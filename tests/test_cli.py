import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch
from donut.cli import main
from donut.config import save, config_path
from donut.tickets import Ticket, queue_report


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")})
        env.start()
        self.addCleanup(env.stop)
        save({"aliases": {"worker": {"engine": "codex", "model": ""}}})

    def test_version_flags_need_no_project_or_config(self):
        for flag in ("-v", "--version"):
            output = io.StringIO()
            with (
                patch("sys.argv", ["donut", flag]),
                patch("donut.cli.release_version", return_value="1.2.3"),
                patch("donut.cli.load") as load,
                contextlib.redirect_stdout(output),
                self.assertRaises(SystemExit) as result,
            ):
                main()
            self.assertEqual(result.exception.code, 0)
            self.assertEqual(output.getvalue(), "donut 1.2.3\n")
            load.assert_not_called()

    def test_model_and_project_flags_before_or_after_command(self):
        variants = [
            ["donut", "-m", "worker", "-p", str(self.root), "do", "next", "3"],
            ["donut", "do", "next", "3", "--model", "worker", "--project", str(self.root)],
        ]
        for args in variants:
            with (
                self.subTest(args=args),
                patch("sys.argv", args),
                patch("donut.cli.run_batch", return_value=0) as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(), 0)
                self.assertEqual(run.call_args.args[1:], (self.root.resolve(), "3"))

    def test_local_planner_uses_default_or_explicit_alias_and_model_lifecycle(self):
        profile = {"engine": "ollama", "model": "local-model", "auto_approve": True}
        save({"aliases": {"local": profile}, "planner": "local"})
        for args in (["donut", "plan", "a small task"], ["donut", "-m", "local", "plan", "a small task"]):
            with (
                self.subTest(args=args),
                patch("sys.argv", args),
                patch("donut.cli.install"),
                patch("donut.cli.activate") as activate,
                patch("donut.cli.run_agent", return_value=0) as run,
                patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(), 0)
                self.assertEqual(activate.call_count, 2)
                self.assertEqual(run.call_args.args[0], profile)
                self.assertIn("Planning only", run.call_args.args[2])

    def test_plan_loop_creates_lanes_and_preserves_followup_context(self):
        profile = {"engine": "codex", "model": "planner-model"}
        save({"aliases": {"planner": profile}, "planner": "planner"})
        output = io.StringIO()
        with (
            patch("sys.argv", ["donut", "--plan", "-p", str(self.root)]),
            patch("builtins.input", side_effect=["", "Add a menu", "Make that keyboard accessible", "/exit"]),
            patch("donut.cli.activate"),
            patch("donut.cli.run_agent", return_value=0) as run,
            patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(), 0)
        for lane in ("to_do", "in_progress", "done"):
            self.assertTrue((self.root / "swim_lanes" / lane).is_dir())
        self.assertTrue((self.root / "skills/donut-planner/SKILL.md").is_file())
        self.assertEqual(run.call_count, 2)
        second = run.call_args.args[2]
        self.assertIn("Add a menu", second)
        self.assertIn("Make that keyboard accessible", second)
        self.assertIn("001.log", second)
        self.assertNotEqual(run.call_args_list[0].args[3], run.call_args_list[1].args[3])
        self.assertFalse(run.call_args.kwargs.get("summary", False))
        self.assertTrue(run.call_args.kwargs["planner"])
        self.assertEqual(output.getvalue().count("PLANNING COMPLETE"), 2)
        self.assertEqual(output.getvalue().count("Try exiting the shell and running `donut --work next 1`"), 2)

    def test_interactive_planning_hides_thinking_and_lists_created_tickets(self):
        save({"aliases": {"planner": {"engine": "codex"}}, "planner": "planner"})
        script = self.root / "fake_planner.py"
        script.write_text(
            "import sys, json\nfrom pathlib import Path\nprompt = sys.stdin.read()\n"
            "assert 'swim_lanes/to_do/' in prompt\n"
            "Path('swim_lanes/to_do/001_menu.md').write_text('**Slug:** `menu`\\n**Depends on:** None\\n')\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'Checking menu scope'}}))\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Created the menu ticket'}}))\n"
        )
        output = io.StringIO()
        with (
            patch("sys.argv", ["donut", "--plan", "-p", str(self.root)]),
            patch("builtins.input", side_effect=["Plan a menu", "/exit"]),
            patch("donut.runner.command", return_value=([sys.executable, str(script)], os.environ.copy())),
            patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(), 0)
        self.assertTrue((self.root / "swim_lanes/to_do/001_menu.md").is_file())
        self.assertNotIn("Checking menu scope", output.getvalue())
        logs = list((self.root / "state").glob("donut/plan-*/001.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("Checking menu scope", logs[0].read_text())
        self.assertIn("Tickets created: 1", output.getvalue())
        self.assertIn("swim_lanes/to_do/001_menu.md", output.getvalue())
        self.assertIn("Created the menu ticket", output.getvalue())
        self.assertIn("PLANNING COMPLETE", output.getvalue())

    def test_new_modes_select_defaults_and_explicit_overrides(self):
        profiles = {
            "planner": {"engine": "codex", "model": "planning"},
            "worker": {"engine": "codex", "model": "working"},
            "override": {"engine": "claude", "model": "chosen"},
        }
        save({"aliases": profiles, "planner": "planner", "default_model": "worker"})
        cases = [
            (["--plan"], "planner", "plan"),
            (["--plan", "-m", "override"], "override", "plan"),
            (["--model", "override", "--plan"], "override", "plan"),
            (["--work"], "worker", "work"),
            (["--work", "next", "1"], "worker", "work"),
            (["--work", "next", "3", "-m", "override"], "override", "work"),
            (["-m", "override", "--work", "next", "all"], "override", "work"),
            (["--work", "3", "--model", "override"], "override", "work"),
            (["-m", "override", "--work", "1"], "override", "work"),
        ]
        for argv, alias, mode in cases:
            with (
                self.subTest(argv=argv),
                patch("sys.argv", ["donut", *argv, "-p", str(self.root)]),
                patch("donut.cli.plan_session", return_value=0) as plan,
                patch("donut.cli.run_batch", return_value=0) as work,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(), 0)
                called = plan if mode == "plan" else work
                self.assertEqual(called.call_args.args[0], profiles[alias])
                if argv == ["--work"]:
                    self.assertEqual(work.call_args.args[2], "all")
                if "next" in argv:
                    self.assertEqual(work.call_args.args[2], argv[argv.index("next") + 1])

    def test_planning_failure_does_not_print_completion(self):
        save({"aliases": {"planner": {"engine": "codex"}}, "planner": "planner"})
        output = io.StringIO()
        with (
            patch("sys.argv", ["donut", "--plan", "Add a menu", "-p", str(self.root)]),
            patch("donut.cli.install"),
            patch("donut.cli.activate"),
            patch("donut.cli.run_agent", return_value=7),
            patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(), 7)
        self.assertIn("PLANNING FAILED", output.getvalue())
        self.assertNotIn("PLANNING COMPLETE", output.getvalue())

    def test_interactive_eof_starts_no_model(self):
        save({"aliases": {"planner": {"engine": "codex"}}, "planner": "planner"})
        with (
            patch("sys.argv", ["donut", "--plan", "-p", str(self.root)]),
            patch("builtins.input", side_effect=EOFError),
            patch("donut.cli.install"),
            patch("donut.cli.run_agent") as run,
            patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 0)
        run.assert_not_called()

    def test_invalid_next_counts_do_not_launch_worker(self):
        for values in (["next"], ["next", "0"], ["next", "-1"], ["next", "many"], ["next", "1", "2"]):
            with (
                self.subTest(values=values),
                patch("sys.argv", ["donut", "--work", *values]),
                patch("donut.cli.run_batch") as run,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as result,
            ):
                main()
            self.assertEqual(result.exception.code, 2)
            run.assert_not_called()

    def test_conflicting_modes_rejected(self):
        for argv in (["--plan", "--work"], ["--plan", "hello", "models"], ["--work", "2", "setup"]):
            with (
                self.subTest(argv=argv),
                patch("sys.argv", ["donut", *argv]),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as result,
            ):
                main()
            self.assertEqual(result.exception.code, 2)

    def test_unknown_alias_does_not_launch_agent(self):
        with (
            patch("sys.argv", ["donut", "-m", "missing", "do", "next", "1"]),
            patch("donut.cli.run_batch") as run,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(), 1)
            run.assert_not_called()

    def test_invalid_profile_does_not_replace_config(self):
        original = config_path().read_bytes()
        with self.assertRaises(ValueError):
            save({"aliases": {"bad alias": {"engine": "codex", "model": ""}}})
        self.assertEqual(config_path().read_bytes(), original)
        self.assertEqual(json.loads(original)["aliases"]["worker"]["engine"], "codex")

    def test_malformed_dependency_fails_closed_without_stopping_the_queue(self):
        lane = self.root / "swim_lanes" / "to_do"
        lane.mkdir(parents=True)
        task = lane / "001_task.md"
        task.write_text("**Slug:** `task`\n**Depends on:** ambiguous freeform dependency\n")
        ticket = Ticket(task, "to_do")
        self.assertEqual(ticket.dependencies, ["ambiguous", "freeform", "dependency"])
        report = queue_report(self.root)
        self.assertEqual(len(report), 1)
        self.assertIn("unknown slug `ambiguous`", report[0])

    def test_missing_project_fails_without_creating_it(self):
        target = self.root / "missing"
        with patch("sys.argv", ["donut", "init", "-p", str(target)]), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 1)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
