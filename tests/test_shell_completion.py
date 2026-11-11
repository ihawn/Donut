import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from donut.terminal import Donut, Draft, Menu, shell_candidates


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        for name in ("docs", "dist", ".git"):
            os.makedirs(Path(self.root) / name)
        (Path(self.root) / "a.txt").write_text("")
        (Path(self.root) / "docs" / "guide.md").write_text("")

    def test_paths_are_relative_to_the_project(self):
        self.assertEqual(shell_candidates("!cd d", self.root)[1], ["dist/", "docs/"])
        self.assertEqual(shell_candidates("!cat docs/g", self.root)[1], ["docs/guide.md"])

    def test_dotfiles_only_when_asked(self):
        self.assertNotIn(".git/", shell_candidates("!ls ", self.root)[1])
        self.assertEqual(shell_candidates("!ls .g", self.root)[1], [".git/"])

    def test_first_word_completes_programs(self):
        bin_dir = Path(self.root) / "bin"
        bin_dir.mkdir()
        tool = bin_dir / "donut-tool"
        tool.write_text("")
        tool.chmod(0o755)
        with patch.dict(os.environ, {"PATH": str(bin_dir)}):
            self.assertEqual(shell_candidates("!donut-t", self.root), ("donut-t", ["donut-tool"]))

    def test_missing_folder_gives_nothing(self):
        self.assertEqual(shell_candidates("!ls nope/x", self.root)[1], [])


class TabKeyTests(unittest.IsolatedAsyncioTestCase):
    async def run_tab(self, text, root, then=()):
        with patch("donut.terminal.install"):
            app = Donut(root)
            app.AUTO_REFRESH_QUOTAS = False
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                draft = app.query_one(Draft)
                draft.insert(text)
                await pilot.pause()  # let the queued Changed event run, or it closes the menu Tab opens
                await pilot.press("tab")
                await pilot.pause()
                menu = app.query_one(Menu)
                state = (draft.text, menu.kind, [row[0] for row in menu.visible_rows])
                for key in then:
                    await pilot.press(key)
                    await pilot.pause()
                return state, draft.text

    async def test_tab_completes_single_match_and_lists_many(self):
        root = tempfile.mkdtemp()
        for name in ("alpha", "apple", "beta"):
            os.makedirs(Path(root) / name)
        (text, kind, _), _ = await self.run_tab("!ls b", root)
        self.assertEqual((text, kind), ("!ls beta/", None))
        (text, kind, rows), after = await self.run_tab("!ls a", root, then=("down", "tab"))
        self.assertEqual((text, kind, rows), ("!ls a", "shell", ["alpha/", "apple/"]))
        self.assertEqual(after, "!ls apple/")

    async def test_tab_outside_shell_mode_is_untouched(self):
        root = tempfile.mkdtemp()
        os.makedirs(Path(root) / "alpha")
        (_, kind, _), _ = await self.run_tab("ls a", root)
        self.assertIsNone(kind)


if __name__ == "__main__":
    unittest.main()
