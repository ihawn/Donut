import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from donut import runner
import sys
import json


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project with spaces"
        for lane in ("to_do", "in_progress", "blocked", "done"):
            (self.project / "swim_lanes" / lane).mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "qwen"
        fake.write_text("""#!/usr/bin/env python3
import json, os, pathlib, re, sys
args=sys.argv[1:]
prompt=sys.stdin.read()
args[args.index('--prompt')+1]=prompt
with open(os.environ['CALLS'],'a') as f: f.write(json.dumps(args)+'\\n')
if '--max-tool-calls' in args:
    print('| Task | Status | What changed | Tests | Human check |')
    sys.exit(0)
if 'You are the planner, recovering' in prompt:
    ticket=pathlib.Path(re.search(r'Blocked ticket: (\\S+)', prompt)[1])
    text=ticket.read_text()
    if 'needs-human' in text:
        print('RECOVERY: NEEDS HUMAN choose the chimney width')
    else:
        text=text.replace('MARKER: block-me','MARKER: fixed').replace('## Completion notes','## Addendum 1: planner recovery\\n\\nUse a 0.3u chimney.\\n\\n## Completion notes',1)
        ticket.write_text(text)
        print('RECOVERY: REQUEUED corrected the chimney width')
    sys.exit(0)
if 'repairing the ticket queue' in prompt:
    for path in pathlib.Path('swim_lanes').rglob('*.md'):
        text=path.read_text()
        if 'BuildAnnex.cs' in text:
            path.write_text(re.sub(r'\\*\\*Depends on:\\*\\*.*', '**Depends on:** None', text))
    print('REPAIR: DONE fixed one Depends on line')
    sys.exit(0)
if 'You are the reviewer' in prompt:
    ticket=pathlib.Path(re.search(r'Ticket file: (\\S+)', prompt)[1]).read_text()
    if 'always-reject' in ticket or ('reject-once' in ticket and 'Addendum 1: review feedback' not in ticket):
        print('Looked at the change.')
        print('VERDICT: CHANGES REQUESTED')
        print('1. Add the missing jump test.')
    else:
        print('VERDICT: APPROVED')
    sys.exit(0)
p=pathlib.Path(re.search(r'ONE named task: (.*?)\\. Read this', prompt)[1])
text=p.read_text()
blocked='block-me' in text and 'MARKER: fixed' not in text
lane='blocked' if blocked else 'done'
new=p.parent.parent/lane/p.name
p.rename(new)
with new.open('a') as f: f.write('\\n## Completion notes\\n'+('BLOCKED: test failure' if blocked else 'Changed fixture. Tests: passed.'))
print('finished', p.name)
""")
        fake.chmod(0o755)
        if os.name == "nt":
            (self.bin / "qwen.cmd").write_text('@echo off\n"' + sys.executable + '" "' + str(fake) + '" %*\n')
        self.calls = self.root / "calls"
        self.env = dict(
            os.environ,
            PYTHONIOENCODING="utf-8",
            PYTHONUTF8="1",
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            CALLS=str(self.calls),
            HOME=str(self.root),
        )

    def ticket(self, number, slug, deps="None", lane="to_do"):
        path = self.project / "swim_lanes" / lane / f"{number:03}_{slug}.md"
        path.write_text(f"**Slug:** `{slug}`\n**Depends on:** {deps}\n")
        return path

    def run_batch(self, count, *extra, new_syntax=False):
        config = self.root / "config/donut"
        config.mkdir(parents=True, exist_ok=True)
        (config / "config.json").write_text(
            json.dumps(
                {
                    "aliases": {
                        "test": {
                            "engine": "openai",
                            "model": "test",
                            "base_url": "https://example.com/v1",
                            "key_env": "TEST_DONUT_KEY",
                            "auto_approve": True,
                        }
                    }
                }
            )
        )
        self.env.update(
            XDG_CONFIG_HOME=str(self.root / "config"), XDG_STATE_HOME=str(self.root / "state"), TEST_DONUT_KEY="fixture"
        )
        mode = ["--work", "next", count] if new_syntax else ["do", "next", count]
        return subprocess.run(
            [sys.executable, "-m", "donut", "--model", "test", *mode, "-p", str(self.project), *extra],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def test_work_next_one_executes_exactly_one_ticket(self):
        for number in range(1, 4):
            self.ticket(number, f"task{number}")
        result = self.run_batch("1", new_syntax=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([p.name for p in runner.tasks(self.project, "done")], ["001_task1.md"])
        self.assertEqual(len(runner.tasks(self.project, "to_do")), 2)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)  # One worker plus summary.

    def test_count_and_unique_sessions(self):
        for n in range(1, 4):
            self.ticket(n, f"task{n}")
        result = self.run_batch("2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(runner.tasks(self.project, "done")), 2)
        calls = [__import__("json").loads(s) for s in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 3)
        self.assertEqual(sum("--prompt" in a for a in calls), 3)
        self.assertTrue(all("--resume" not in a and "--continue" not in a for a in calls))
        self.assertIn("--max-tool-calls", calls[-1])
        summary = calls[-1][calls[-1].index("--prompt") + 1]
        self.assertNotIn("003_task3", summary)

    def test_all_skips_blocked_dependencies(self):
        self.ticket(1, "block-me")
        self.ticket(2, "dependent", "`block-me`")
        self.ticket(3, "independent")
        result = self.run_batch("all")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([p.name for p in runner.tasks(self.project, "done")], ["003_independent.md"])
        self.assertEqual(len(runner.tasks(self.project, "blocked")), 1)
        self.assertEqual(len(runner.tasks(self.project, "to_do")), 1)

    def test_on_hold_and_dependent_work_are_skipped(self):
        (self.project / "swim_lanes/on_hold").mkdir()
        held = self.ticket(1, "held", lane="on_hold")
        before = held.read_bytes()
        self.ticket(2, "dependent", "`held`")
        self.ticket(3, "independent")
        result = self.run_batch("all", new_syntax=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([p.name for p in runner.tasks(self.project, "done")], ["003_independent.md"])
        self.assertEqual(held.read_bytes(), before)
        self.assertTrue((self.project / "swim_lanes/to_do/002_dependent.md").exists())

    def test_hold_wins_over_stale_active_or_done_duplicates(self):
        (self.project / "swim_lanes/on_hold").mkdir()
        self.ticket(1, "held", lane="on_hold")
        self.ticket(2, "held", lane="done")
        self.ticket(3, "held", lane="in_progress")
        self.ticket(4, "dependent", "`held`")
        independent = self.ticket(5, "independent")
        self.assertEqual(runner.pick(self.project, set()), independent)

    def test_blocked_tickets_and_their_dependents_stay_untouched(self):
        blocked = self.ticket(1, "blocked-work", lane="blocked")
        before = blocked.read_bytes()
        self.ticket(2, "dependent", "`blocked-work`")
        self.ticket(3, "independent")
        result = self.run_batch("all", new_syntax=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(blocked.read_bytes(), before)
        self.assertEqual([p.name for p in runner.tasks(self.project, "done")], ["003_independent.md"])

    def test_resume_in_progress_first(self):
        existing = self.ticket(5, "existing", lane="in_progress")
        self.ticket(1, "new")
        self.assertEqual(runner.pick(self.project, set()), existing)

    def test_invalid_count_and_empty(self):
        self.assertEqual(self.run_batch("0").returncode, 1)
        self.assertEqual(self.run_batch("all").returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_project_lock(self):
        import hashlib

        cache = self.root / "state/donut"
        cache.mkdir(parents=True)
        lock = runner.acquire_lock(cache / (hashlib.sha256(str(self.project.resolve()).encode()).hexdigest() + ".lock"))
        self.addCleanup(lock.close)
        self.assertEqual(self.run_batch("1").returncode, 1)
        self.assertFalse(self.calls.exists())

    def test_process_failure_stops_batch(self):
        self.ticket(1, "first")
        self.ticket(2, "second")
        (self.bin / "qwen").write_text("#!/usr/bin/env python3\nraise SystemExit(7)\n")
        result = self.run_batch("all")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("[2/all]", result.stdout)
        self.assertEqual(len(runner.tasks(self.project, "to_do")), 2)

    def test_reported_model_and_code_stream_reach_terminal_and_plain_log(self):
        import contextlib
        import io
        from unittest.mock import patch

        events = [
            {"type": "system", "subtype": "init", "model": "actual-model"},
            {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}},
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "```py\nreturn 42\n```"},
                },
            },
        ]
        script = self.root / "stream.py"
        script.write_text(
            "import sys\nsys.stdin.read()\n" + "\n".join("print(" + repr(json.dumps(event)) + ")" for event in events)
        )
        log = self.root / "logs" / "run.log"
        terminal = io.StringIO()
        with (
            patch("donut.runner.command", return_value=([sys.executable, str(script)], os.environ.copy())),
            contextlib.redirect_stdout(terminal),
        ):
            result = runner.run_agent({"engine": "claude"}, self.project, "test", log)
        self.assertEqual(result, 0)
        self.assertIn("[actual-model]", terminal.getvalue())
        self.assertIn("return 42", terminal.getvalue())
        self.assertNotIn("\x1b", log.read_text())
        self.assertTrue(terminal.getvalue().endswith("```"))

    def test_stream_rendering(self):
        import json

        def event(e):
            return runner.render_event(json.dumps(dict(type="stream_event", event=e)))

        self.assertIn("Thinking]", event(dict(type="content_block_start", content_block=dict(type="thinking"))))
        self.assertEqual(
            event(dict(type="content_block_delta", delta=dict(type="thinking_delta", thinking="reason"))), "reason"
        )
        self.assertEqual(
            event(dict(type="content_block_delta", delta=dict(type="text_delta", text="answer"))), "answer"
        )
        self.assertIn(
            "read_file", event(dict(type="content_block_start", content_block=dict(type="tool_use", name="read_file")))
        )
        self.assertEqual(
            event(dict(type="content_block_delta", delta=dict(type="input_json_delta", partial_json="{}"))), "{}"
        )
        result = dict(type="user", message=dict(content=[dict(type="tool_result", content="PASS")]))
        self.assertIn("PASS", runner.render_event(json.dumps(result)))
        self.assertEqual(runner.render_event(json.dumps(dict(type="assistant", message=dict(content=[])))), "")
        self.assertEqual(runner.render_event("stderr warning\n"), "stderr warning\n")

    def test_bounded_summary_excludes_unattempted_dependencies(self):
        self.ticket(1, "first")
        self.ticket(2, "waiting", "`missing-task`")
        result = self.run_batch("1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        summary = calls[-1][calls[-1].index("--prompt") + 1]
        self.assertIn("001_first", summary)
        self.assertNotIn("waiting", summary)
        self.assertNotIn("missing-task", summary)

    def test_verify_command_failure_moves_ticket_to_blocked(self):
        self.ticket(1, "checked")
        path = self.project / "swim_lanes/to_do/001_checked.md"
        path.write_text(path.read_text() + "**Verify:** `exit 3`\n")
        self.ticket(2, "dependent", "`checked`")
        self.ticket(3, "passing")
        path = self.project / "swim_lanes/to_do/003_passing.md"
        path.write_text(path.read_text() + "**Verify:** `exit 0`\n")
        result = self.run_batch("all", new_syntax=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([p.name for p in runner.tasks(self.project, "done")], ["003_passing.md"])
        self.assertEqual([p.name for p in runner.tasks(self.project, "blocked")], ["001_checked.md"])
        blocked = (self.project / "swim_lanes/blocked/001_checked.md").read_text()
        self.assertIn("BLOCKED: Donut ran the Verify command and it failed (exit 3)", blocked)
        self.assertTrue((self.project / "swim_lanes/to_do/002_dependent.md").exists())
        self.assertIn("Verify failed (exit 3)", result.stdout)
        self.assertIn("Queue: to_do/002_dependent.md: depends on `checked`, which is on hold or blocked", result.stdout)
        runs = sorted((self.root / "state/donut").glob("run-*"))
        records = json.loads((runs[-1] / "results.json").read_text())
        self.assertEqual([r["verify"] for r in records], ["failed (exit 3)", "passed"])
        self.assertTrue((runs[-1] / "001.verify.log").exists())

    def test_checkpoint_commits_after_each_ticket(self):
        git_env = dict(
            GIT_CONFIG_GLOBAL=str(self.root / "no-gitconfig"), GIT_CONFIG_SYSTEM=str(self.root / "no-gitconfig")
        )
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True, env=dict(os.environ, **git_env))
        self.env.update(git_env)
        self.ticket(1, "first")
        self.ticket(2, "second")
        result = self.run_batch("all", "--checkpoint", new_syntax=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = subprocess.run(["git", "log", "--format=%s"], cwd=self.project, capture_output=True, text=True)
        self.assertEqual(log.stdout.split(), ["donut:", "done", "002_second.md", "donut:", "done", "001_first.md"])
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.project, capture_output=True, text=True)
        self.assertEqual(status.stdout, "")
        runs = sorted((self.root / "state/donut").glob("run-*"))
        records = json.loads((runs[-1] / "results.json").read_text())
        self.assertTrue(all(r["checkpoint"] for r in records))
        self.assertEqual(records[0]["changed_files"], ["swim_lanes/done/001_first.md"])

    def test_checkpoint_outside_git_is_refused_before_any_work(self):
        self.ticket(1, "first")
        result = self.run_batch("all", "--checkpoint", new_syntax=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a git repository", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_dependency_order(self):
        self.ticket(1, "dependent", "`parent`")
        self.ticket(2, "parent")
        result = self.run_batch("all")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(result.stdout.index("002_parent.md"), result.stdout.index("001_dependent.md"))

    def results(self):
        runs = sorted((self.root / "state/donut").glob("run-*"))
        return json.loads((runs[-1] / "results.json").read_text())

    def calls_made(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_reviewer_approves_and_the_worker_moves_on(self):
        self.ticket(1, "first")
        self.ticket(2, "second")
        result = self.run_batch("all", "--review", "test", new_syntax=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(runner.tasks(self.project, "done")), 2)
        self.assertIn("[review 1/3] 001_first.md", result.stdout)
        self.assertIn("Review: approved", result.stdout)
        self.assertEqual([r["review"]["verdict"] for r in self.results()], ["approved", "approved"])
        review_calls = [c for c in self.calls_made() if "You are the reviewer" in c[c.index("--prompt") + 1]]
        self.assertEqual(len(review_calls), 2)
        self.assertTrue(all(c[c.index("--approval-mode") + 1] == "plan" for c in review_calls))  # Read-only.

    def test_requested_changes_go_back_to_the_worker(self):
        self.ticket(1, "reject-once")
        result = self.run_batch("all", "--review", "test", new_syntax=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Review: changes requested; sending feedback to the worker (2/3)", result.stdout)
        self.assertIn("[review 2/3] 001_reject-once.md", result.stdout)
        done = self.project / "swim_lanes/done/001_reject-once.md"
        self.assertTrue(done.exists())
        text = done.read_text()
        self.assertIn("## Addendum 1: review feedback", text)
        self.assertIn("1. Add the missing jump test.", text)
        # The addendum sits in the ticket body, before the completion notes.
        self.assertLess(text.index("## Addendum 1"), text.index("## Completion notes"))
        prompts = [c[c.index("--prompt") + 1] for c in self.calls_made()]
        worker_prompts = [p for p in prompts if "ONE named task" in p]
        self.assertEqual(len(worker_prompts), 2)
        self.assertIn("This is a rework", worker_prompts[1])
        self.assertIn("swim_lanes/in_progress/001_reject-once.md", worker_prompts[1])
        self.assertEqual(self.results()[0]["review"]["rounds"], 2)

    def test_ticket_is_blocked_when_the_reviewer_never_approves(self):
        self.ticket(1, "always-reject")
        self.ticket(2, "dependent", "`always-reject`")
        result = self.run_batch("all", "--review", "test", "--review-rounds", "2", new_syntax=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        blocked = self.project / "swim_lanes/blocked/001_always-reject.md"
        self.assertTrue(blocked.exists())
        self.assertIn("BLOCKED: the reviewer did not approve after 2 reviews", blocked.read_text())
        self.assertTrue((self.project / "swim_lanes/to_do/002_dependent.md").exists())
        self.assertEqual(self.results()[0]["status"], "Blocked")

    def test_review_off_and_no_planner_mean_no_review(self):
        self.ticket(1, "first")
        result = self.run_batch("all", "--review", "off", new_syntax=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("[review", result.stdout)
        self.assertNotIn("review", self.results()[0])

    def test_addenda_go_before_completion_notes_in_order(self):
        text = "# Jump\n**Slug:** `jump`\n\n## Requirements\n1. Jump.\n\n## Completion notes\nDone.\n"
        once = runner.add_addendum(text, 1, "1. Add a test.")
        twice = runner.add_addendum(once, 2, "1. The test still fails.")
        self.assertLess(twice.index("## Addendum 1"), twice.index("## Addendum 2"))
        self.assertLess(twice.index("## Addendum 2"), twice.index("## Completion notes"))
        self.assertTrue(twice.rstrip().endswith("Done."))
        no_notes = runner.add_addendum("# Jump\n", 1, "1. Fix it.")
        self.assertTrue(no_notes.rstrip().endswith("1. Fix it."))

    def write(self, number, slug, body, deps="None", lane="to_do"):
        path = self.project / "swim_lanes" / lane / f"{number:03}_{slug}.md"
        path.write_text(f"**Slug:** `{slug}`\n**Depends on:** {deps}\n\n{body}\n")
        return path

    def test_planner_recovers_a_blocked_ticket_and_the_run_continues(self):
        self.write(1, "chimney", "MARKER: block-me")
        self.ticket(2, "dependent", "`chimney`")
        result = self.run_batch("all", "--recover", "test", new_syntax=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[recover] 001_chimney.md", result.stdout)
        self.assertIn("Recovery: ticket revised and requeued. corrected the chimney width", result.stdout)
        self.assertEqual([p.name for p in runner.tasks(self.project, "done")], ["001_chimney.md", "002_dependent.md"])
        text = (self.project / "swim_lanes/done/001_chimney.md").read_text()
        self.assertLess(text.index("## Addendum 1: planner recovery"), text.index("## Completion notes"))
        self.assertEqual(
            [(r["task"], r["status"]) for r in self.results()][:2],
            [("001_chimney.md", "Blocked"), ("001_chimney.md", "Done")],
        )
        runs = sorted((self.root / "state/donut").glob("run-*"))
        self.assertTrue((runs[-1] / "001.recover.log").exists())
        self.assertTrue((runs[-1] / "002.log").exists())  # The retry got its own log.

    def test_recovery_retries_within_the_requested_count(self):
        self.write(1, "chimney", "MARKER: block-me")
        result = self.run_batch("1", "--recover", "test", new_syntax=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.project / "swim_lanes/done/001_chimney.md").exists())

    def test_planner_can_leave_a_ticket_for_a_human(self):
        self.write(1, "chimney", "MARKER: block-me needs-human")
        result = self.run_batch("all", "--recover", "test", new_syntax=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Recovery: needs a human decision. choose the chimney width", result.stdout)
        self.assertTrue((self.project / "swim_lanes/blocked/001_chimney.md").exists())
        self.assertEqual(result.stdout.count("[recover]"), 1)  # Never more than once per ticket.

    def test_planner_repairs_broken_dependencies_once(self):
        self.ticket(1, "annex", "`BuildAnnex.cs` Section Done")
        result = self.run_batch("all", "--recover", "test", new_syntax=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[repair queue] 1 stuck ticket(s)", result.stdout)
        self.assertIn("Repair: 1 of 1 fixed.", result.stdout)
        self.assertTrue((self.project / "swim_lanes/done/001_annex.md").exists())

    def test_recovery_off(self):
        self.write(1, "chimney", "MARKER: block-me")
        self.ticket(2, "annex", "`BuildAnnex.cs`")
        result = self.run_batch("all", "--recover", "off", new_syntax=True)
        self.assertNotIn("[recover]", result.stdout)
        self.assertNotIn("[repair queue]", result.stdout)

    def test_verdict_parsing(self):
        log = self.root / "review.log"
        log.write_text(
            "[model]\nNotes VERDICT: APPROVED earlier\nMore\n**VERDICT: CHANGES REQUESTED**\n1. Fix it.\n[Tool result]\n"
        )
        verdict, feedback = runner.read_verdict(log)
        self.assertEqual(verdict, "changes")  # The last verdict line wins.
        self.assertIn("1. Fix it.", feedback)
        self.assertNotIn("[Tool result]", feedback)
        log.write_text("no verdict here")
        self.assertEqual(runner.read_verdict(log), (None, ""))


if __name__ == "__main__":
    unittest.main()
