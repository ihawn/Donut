import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from donut import pullrequest, runner


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def record(task, status="Done", verdict="approved", files=()):
    return {"task": task, "status": status, "review": {"verdict": verdict}, "changed_files": list(files)}


class RuleTests(unittest.TestCase):
    def test_only_fully_approved_runs_become_pull_requests(self):
        self.assertEqual(pullrequest.approved([record("001_a.md"), record("002_b.md")]), (True, ""))
        ok, reason = pullrequest.approved([record("001_a.md"), record("002_b.md", status="Blocked")])
        self.assertFalse(ok)
        self.assertIn("002_b.md", reason)
        ok, reason = pullrequest.approved([record("001_a.md", verdict="changes requested")])
        self.assertIn("did not approve 001_a.md", reason)
        # A ticket requeued and then finished counts by its last attempt.
        self.assertTrue(pullrequest.approved([record("001_a.md", status="Blocked"), record("001_a.md")])[0])
        self.assertFalse(pullrequest.approved([])[0])

    def test_planner_text_becomes_title_and_body(self):
        text = "[opus]\nTITLE: Add rate limiting to the API\n\n## Summary\nLimits each key.\n[opus · Connected]\n"
        self.assertEqual(
            pullrequest.parse_description(text), ("Add rate limiting to the API", "## Summary\nLimits each key.")
        )
        self.assertEqual(pullrequest.parse_description("no title here"), (None, ""))


class GitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.remote, self.project, self.bin = root / "remote.git", root / "project", root / "bin"
        env = {"GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "GIT_CONFIG_SYSTEM": str(root / "none")}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        (root / "gitconfig").write_text("[user]\n\tname = T\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n")
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.project.mkdir()
        git(self.project, "init", "-q", "-b", "main")
        (self.project / "app.py").write_text("print(1)\n")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "start")
        git(self.project, "remote", "add", "origin", str(self.remote))
        git(self.project, "push", "-q", "-u", "origin", "main")
        git(self.project, "remote", "set-head", "origin", "main")
        self.bin.mkdir()
        self.calls = root / "gh_calls"
        gh = self.bin / "gh"
        gh.write_text(
            f"#!{sys.executable}\nimport sys\nopen({str(self.calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n' + sys.stdin.read())\n"
            "print('https://github.com/example/project/pull/9')\n"
        )
        gh.chmod(0o755)

    @unittest.skipIf(os.name == "nt", "the stand-in gh is a POSIX script")
    def test_commits_only_the_tickets_files_on_a_new_branch_and_opens_the_pr(self):
        (self.project / "app.py").write_text("print(2)\n")
        (self.project / "notes.txt").write_text("my own unrelated edit\n")
        with patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}):
            url = pullrequest.raise_pull_request(self.project, "Print two", "## Summary\nPrints 2.", ["app.py"])
        self.assertEqual(url, "https://github.com/example/project/pull/9")
        self.assertEqual(git(self.project, "rev-parse", "--abbrev-ref", "HEAD"), "donut/print-two")
        self.assertEqual(
            git(self.project, "show", "--name-only", "--format=%s", "HEAD").split(), ["Print", "two", "app.py"]
        )
        self.assertIn("notes.txt", git(self.project, "status", "--porcelain"))  # Left alone, uncommitted.
        self.assertIn("donut/print-two", git(self.remote, "branch"))
        call = self.calls.read_text()
        self.assertIn("pr create --base main --head donut/print-two --title Print two", call)
        self.assertIn("## Summary\nPrints 2.", call)

    def test_without_a_remote_or_gh_it_says_why(self):
        git(self.project, "remote", "remove", "origin")
        with self.assertRaisesRegex(ValueError, "no origin remote"):
            pullrequest.raise_pull_request(self.project, "t", "b", [])
        git(self.project, "remote", "add", "origin", str(self.remote))
        with patch("donut.pullrequest.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "gh"):
                pullrequest.raise_pull_request(self.project, "t", "b", [])


class RunnerTests(unittest.TestCase):
    def test_the_planner_writes_it_after_approval_and_not_otherwise(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)

            def planner_writes(profile, project, prompt, log, **kwargs):
                self.assertIn("approved every ticket", prompt)
                self.assertTrue(kwargs.get("summary"))  # Read-only: no tools.
                log.write_text("[opus]\nTITLE: Add the thing\n\nBody.\n")
                return 0

            planner, reviewer = {"engine": "claude", "model": "opus"}, {"engine": "codex"}
            with (
                patch("donut.runner.run_agent", side_effect=planner_writes),
                patch("donut.runner.pullrequest.raise_pull_request", return_value="https://x/pull/1") as raised,
            ):
                url = runner.open_pull_request(
                    planner, reviewer, Path(temp), [record("001_a.md", files=["a.py"])], run_dir
                )
                self.assertEqual(url, "https://x/pull/1")
                raised.assert_called_with(Path(temp), "Add the thing", "Body.", ["a.py"])
                raised.reset_mock()
                blocked = [record("001_a.md", status="Blocked")]
                self.assertIsNone(runner.open_pull_request(planner, reviewer, Path(temp), blocked, run_dir))
                self.assertIsNone(runner.open_pull_request(planner, None, Path(temp), [record("001_a.md")], run_dir))
                raised.assert_not_called()


if __name__ == "__main__":
    unittest.main()
