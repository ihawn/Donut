import tempfile
import unittest
from pathlib import Path

from donut import repomap, runner, worktrees


class RepoMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        for name, text in {
            "app/models.py": "class Ticket:\n    def pick(self):\n        pass\n\nasync def load():\n    pass\n",
            "app/views.py": "def index():\n    pass\n",
            "web/main.ts": "export function start() {}\nexport default class App {}\ninterface Options {}\n",
            "web/logo.png": "",
            "node_modules/x/index.js": "function hidden() {}\n",
            ".donut/s1/to_do/001_a.md": "**Slug:** `a`\n**Depends on:** None\n\n## Files\n- `app/models.py`\n",
        }.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def test_the_tickets_files_come_first_with_symbols_then_neighbours_then_folders(self):
        lines = repomap.build(self.project, ["app/models.py"]).splitlines()
        self.assertEqual(lines[0], "app/models.py: class Ticket, def pick, def load")
        self.assertEqual(lines[1], "app/views.py: def index")
        self.assertIn("web/ (2 files)", lines)
        self.assertFalse(any("node_modules" in line or ".donut" in line for line in lines))

    def test_a_folder_in_the_ticket_lists_its_files_and_the_limit_holds(self):
        found = repomap.build(self.project, ["web/"])
        self.assertIn("web/main.ts: function start, class App, interface Options", found)
        self.assertLessEqual(len(repomap.build(self.project, ["app/models.py"], limit=40)), 80)
        self.assertEqual(repomap.build(self.project, skip=["app/", "web/"]), "")

    def test_the_worker_prompt_carries_the_map_unless_it_is_off(self):
        ticket = self.project / ".donut/s1/to_do/001_a.md"
        prompt = runner.worker_prompt(self.project, ticket)
        self.assertIn("<map>\napp/models.py: class Ticket, def pick, def load\n", prompt)
        self.assertNotIn("001_a.md\n", prompt.split("<map>")[1])  # The tickets folder is not part of the map.
        self.assertNotIn("<map>", runner.worker_prompt(self.project, ticket, repo_map=False))
        rework = runner.rework_prompt(self.project, ticket)
        self.assertLess(rework.index("This is a rework"), rework.index("<map>"))

    def test_overlap_of_ticket_file_lists(self):
        self.assertTrue(worktrees.overlap(["a.py"], ["b.py", "a.py"]))
        self.assertTrue(worktrees.overlap(["src/"], ["src/a.py"]))
        self.assertTrue(worktrees.overlap(["src/a.py"], ["src"]))
        self.assertFalse(worktrees.overlap(["src/a.py"], ["src/a.pyi", "srcs/a.py"]))


if __name__ == "__main__":
    unittest.main()
