import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from donut import conversations, platform, workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)

    def test_each_session_gets_its_own_lanes_folder(self):
        now = time.mktime((2026, 11, 9, 17, 12, 0, 0, 0, -1))
        first = workspace.new_name(self.project, now)
        self.assertEqual(first, "20261109-1712")
        lanes = workspace.create(self.project, first)
        self.assertEqual(lanes, self.project / ".donut" / first)
        self.assertTrue((lanes / "on_hold" / ".gitkeep").is_file())
        self.assertEqual(workspace.new_name(self.project, now), "20261109-1712-2")  # Never reuses a folder.
        self.assertEqual(workspace.relative(self.project, lanes), ".donut/20261109-1712/")
        self.assertEqual(workspace.lanes_dir(self.project), self.project / "swim_lanes")

    def test_skills_folder_and_odd_names_are_not_sessions(self):
        (self.project / ".donut" / "skills" / "swim-lane-worker").mkdir(parents=True)
        for name in ("skills", "../up", "a b", ""):
            self.assertFalse(workspace.valid(name), name)
        with self.assertRaises(ValueError):
            workspace.create(self.project, "skills")
        self.assertEqual(workspace.sessions(self.project), [])

    def test_command_line_default_is_latest_then_legacy_then_new(self):
        self.assertRegex(workspace.default(self.project), r"^\d{8}-\d{4}$")
        workspace.create(self.project, None)  # swim_lanes/
        self.assertIsNone(workspace.default(self.project))
        older = workspace.create(self.project, "older")
        newer = workspace.create(self.project, "newer")
        os.utime(older / "to_do", (1, 1))
        os.utime(older, (1, 1))
        for path in [older, *older.iterdir()]:
            os.utime(path, (1, 1))
        (newer / "to_do" / "001_x.md").write_text("x")
        self.assertEqual(workspace.sessions(self.project), ["newer", "older"])
        self.assertEqual(workspace.default(self.project), "newer")
        self.assertEqual(workspace.count(self.project, "newer")["to_do"], 1)


class RecentProjectTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        state = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        state.start()
        self.addCleanup(state.stop)

    def test_projects_opened_or_talked_about_are_listed_newest_first(self):
        a, b, c, gone = (self.root / name for name in ("a", "b", "c", "gone"))
        for folder in (a, b, c, gone):
            folder.mkdir()
        conversations.record_turn(None, c, "plan", "plan", "x", "m", None, "hi", "yo", session="s1")
        conversations.remember_project(a)
        conversations.remember_project(gone)
        conversations.remember_project(b)
        conversations.remember_project(a)
        gone.rmdir()
        self.assertEqual(conversations.recent_projects(), [str(a.resolve()), str(b.resolve()), str(c.resolve())])
        self.assertEqual(conversations.for_project(c)[0]["session"], "s1")
        if os.name != "nt":  # Windows has no Unix permission bits.
            self.assertEqual(oct(conversations.projects_path().stat().st_mode & 0o777), "0o600")


class OpenWindowTests(unittest.TestCase):
    def open(self, system, env=None, found=()):
        with patch("donut.platform.subprocess.Popen") as popen:
            where = platform.open_window(Path("/work/app"), env or {}, lambda name: name in found, system)
        return where, (popen.call_args.args[0] if popen.called else None)

    def test_each_platform_opens_donut_in_its_own_terminal(self):
        command = platform.window_command(Path("/work/app"))
        app = str(Path("/work/app"))
        where, args = self.open("linux", {"TMUX": "/tmp/t"}, {"tmux", "gnome-terminal"})
        self.assertEqual((where, args[:4]), ("a new tmux window", ["tmux", "new-window", "-c", app]))
        where, args = self.open("darwin")
        self.assertEqual(where, "a new Terminal window")
        self.assertTrue(args[2].startswith('tell application "Terminal" to do script "cd '), args[2])
        self.assertIn(" -m donut -p ", args[2])
        where, args = self.open("darwin", {"TERM_PROGRAM": "iTerm.app"})
        self.assertEqual(where, "a new iTerm window")
        where, args = self.open("win32", found={"wt"})
        self.assertEqual(args, ["wt", "-d", app, *command])
        where, args = self.open("linux", found={"gnome-terminal", "xterm"})
        self.assertEqual(args[:4], ["gnome-terminal", "--working-directory", app, "--"])
        where, args = self.open("linux", {"TERMINAL": "foot"}, {"foot", "xterm"})
        self.assertEqual((where, args[:2]), ("a new foot window", ["foot", "-e"]))
        self.assertEqual(self.open("linux"), (None, None))
        self.assertEqual(json.loads(json.dumps(command))[-2:], ["-p", app])


if __name__ == "__main__":
    unittest.main()
