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

    def test_other_models_are_only_unloaded_when_asked(self):
        with patch("donut.providers.subprocess.run") as run:
            run.return_value.stdout = "NAME ID\nother:7b x\nmine:7b y\n"
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
