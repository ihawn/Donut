import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from donut import checkpoints, pullrequest, runner


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


class MultiRepositoryTests(unittest.TestCase):
    def test_the_planner_answer_splits_by_repository(self):
        text = "REPO: api\nTITLE: Add the endpoint\n\nAPI body.\n\nREPO: web\nTITLE: Call the endpoint\n\nWeb body.\n"
        self.assertEqual(
            pullrequest.parse_descriptions(text, ["api", "web"]),
            {"api": ("Add the endpoint", "API body."), "web": ("Call the endpoint", "Web body.")},
        )
        # One description with no REPO lines serves every repository.
        self.assertEqual(pullrequest.parse_descriptions("TITLE: T\n\nB", ["api"]), {"api": ("T", "B")})
        prompt = pullrequest.describe_prompt([record("001_a.md")], [{"name": "api", "files": ["x.py"]}] * 2)
        self.assertIn("each gets its own pull request", prompt)

    def test_files_group_by_repository_and_checkpoints_commit_each(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {"GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "GIT_CONFIG_SYSTEM": str(root / "none")}
            (root / "gitconfig").write_text("[user]\n\tname = T\n\temail = t@example.com\n")
            workspace = root / "workspace"
            with patch.dict(os.environ, env):
                for name in ("api", "web"):
                    (workspace / name).mkdir(parents=True)
                    git(workspace / name, "init", "-q")
                    (workspace / name / "a.txt").write_text("1\n")
                (workspace / "notes.md").write_text("not in any repository\n")
                self.assertFalse(checkpoints.is_repository(workspace))
                self.assertEqual(checkpoints.changed_files(workspace), ["api/a.txt", "web/a.txt"])
                groups = checkpoints.group(workspace, ["api/a.txt", "web/a.txt"])
                self.assertEqual([(p, f) for _, p, f in groups], [("api/", ["a.txt"]), ("web/", ["a.txt"])])
                self.assertEqual(len(checkpoints.commit(workspace, "donut: done 001_a.md").split(", ")), 2)
                self.assertEqual(checkpoints.changed_files(workspace), [])
                git(workspace / "api", "remote", "add", "origin", str(root / "api.git"))
                (workspace / "api" / "a.txt").write_text("2\n")
                (workspace / "web" / "a.txt").write_text("2\n")
                found, skipped = pullrequest.targets(workspace, checkpoints.changed_files(workspace))
                self.assertEqual([(t["name"], t["files"]) for t in found], [("api", ["a.txt"])])
                self.assertEqual(skipped, [("web", "no origin remote to push to")])

    def test_a_repository_inside_a_repository_stays_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {"GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "GIT_CONFIG_SYSTEM": str(root / "none")}
            (root / "gitconfig").write_text("[user]\n\tname = T\n\temail = t@example.com\n")
            with patch.dict(os.environ, env):
                root = root / "project"
                root.mkdir()
                git(root, "init", "-q")
                (root / "libs" / "shared").mkdir(parents=True)
                git(root / "libs" / "shared", "init", "-q")
                (root / "app.py").write_text("1\n")
                (root / "libs" / "shared" / "util.py").write_text("1\n")
                self.assertEqual(checkpoints.changed_files(root), ["app.py", "libs/shared/util.py"])
                checkpoints.commit(root, "checkpoint")
                # The outer repository did not swallow the inner one.
                self.assertEqual(git(root, "show", "--name-only", "--format=", "HEAD").split(), ["app.py"])


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
            target = {"name": "project", "root": Path(temp), "files": ["a.py"]}
            with (
                patch("donut.runner.run_agent", side_effect=planner_writes),
                patch("donut.runner.pullrequest.targets", return_value=([target], [])),
                patch("donut.runner.pullrequest.raise_pull_request", return_value="https://x/pull/1") as raised,
            ):
                urls = runner.open_pull_request(
                    planner, reviewer, Path(temp), [record("001_a.md", files=["a.py"])], run_dir
                )
                self.assertEqual(urls, ["https://x/pull/1"])
                raised.assert_called_with(Path(temp), "Add the thing", "Body.", ["a.py"])
                raised.reset_mock()
                blocked = [record("001_a.md", status="Blocked")]
                self.assertEqual(runner.open_pull_request(planner, reviewer, Path(temp), blocked, run_dir), [])
                self.assertEqual(runner.open_pull_request(planner, None, Path(temp), [record("001_a.md")], run_dir), [])
                # Only ticket files changed: nothing to raise.
                lanes = Path(temp) / ".donut" / "s1"
                only_tickets = [record("001_a.md", files=[".donut/s1/done/001_a.md"])]
                self.assertEqual(
                    runner.open_pull_request(planner, reviewer, Path(temp), only_tickets, run_dir, lanes), []
                )
                raised.assert_not_called()


if __name__ == "__main__":
    unittest.main()
