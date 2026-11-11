import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from donut import config, conversations, platform, routing, workspace
from donut.cli import main


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        shell = {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_PROFILE": "work",
            "ANTHROPIC_API_KEY": "key",
        }
        env = patch.dict(os.environ, shell)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DONUT_PROFILE", None)

    def run_donut(self, *args):
        errors = io.StringIO()
        with (
            patch("sys.argv", ["donut", *args]),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(errors),
        ):
            code = main()
        return code, output.getvalue(), errors.getvalue()

    def test_each_profile_has_its_own_configuration_state_and_tickets(self):
        places = {}
        for name in ("default", "personal"):
            config.use_profile(name)
            places[name] = (
                config.config_path(),
                conversations.store_path(),
                routing.log_path(),
                workspace.lanes_dir(self.project, "one"),
            )
        self.assertEqual(places["default"][0], self.root / "config/donut/config.json")
        self.assertEqual(places["personal"][0], self.root / "config/donut/profiles/personal.json")
        self.assertEqual(places["default"][3], self.project / ".donut/one")
        self.assertEqual(places["personal"][3], self.project / ".donut/profiles/personal/one")
        for mine, other in zip(places["default"], places["personal"]):
            self.assertNotEqual(mine, other)
        # A session made in one profile is not listed in the other.
        workspace.create(self.project, "one")
        self.assertEqual(workspace.sessions(self.project), ["one"])
        config.use_profile("default")
        self.assertEqual(workspace.sessions(self.project), [])
        self.assertFalse(workspace.valid("profiles"))

    def test_a_named_profile_drops_the_shells_provider_variables(self):
        config.use_profile(None)
        self.assertEqual(os.environ["CLAUDE_CODE_USE_BEDROCK"], "1")  # The default profile keeps the shell as it is.
        config.use_profile("personal")
        self.assertEqual(os.environ["DONUT_PROFILE"], "personal")
        for key in ("CLAUDE_CODE_USE_BEDROCK", "AWS_PROFILE", "ANTHROPIC_API_KEY"):
            self.assertNotIn(key, os.environ)
        self.assertEqual(platform.window_command(self.project)[-2:], ["--profile", "personal"])
        with self.assertRaises(ValueError):
            config.use_profile("../other")

    def test_a_named_profiles_claude_alias_needs_its_own_login_folder(self):
        config.use_profile("personal")
        path = config.config_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"aliases": {"claude": {"engine": "claude", "model": "opus"}}}))
        with self.assertRaisesRegex(ValueError, "claude_config_dir"):
            config.load()
        folder = str(self.root / "claude-personal")
        config.save({"aliases": {"claude": {"engine": "claude", "model": "opus", "claude_config_dir": folder}}})
        self.assertEqual(list(config.load()["aliases"]), ["claude"])

    def test_the_command_line_uses_only_the_chosen_profiles_aliases(self):
        config.save({"aliases": {"work": {"engine": "codex", "model": ""}}})
        config.use_profile("personal")
        config.save({"aliases": {"mine": {"engine": "codex", "model": ""}}})
        config.use_profile(None)
        self.assertIn("work", self.run_donut("models", "-p", str(self.project))[1])
        code, output, _ = self.run_donut("--profile", "personal", "models", "-p", str(self.project))
        self.assertEqual(code, 0)
        self.assertIn("mine", output)
        self.assertNotIn("work", output)

    def test_a_project_can_list_the_profiles_allowed_to_open_it(self):
        config.check_project(self.project)  # No file: every profile may open it.
        pin = self.project / ".donut" / "config.json"
        pin.parent.mkdir()
        pin.write_text(json.dumps({"profiles": ["default"]}))
        config.check_project(self.project)
        code, _, errors = self.run_donut("--profile", "personal", "models", "-p", str(self.project))
        self.assertEqual(code, 1)
        self.assertIn("allows only the default profile", errors)
        self.assertEqual(self.run_donut("--profile", "default", "models", "-p", str(self.project))[0], 0)
        pin.write_text("not json")  # A file that cannot be read refuses every profile.
        with self.assertRaises(ValueError):
            config.check_project(self.project)


if __name__ == "__main__":
    unittest.main()
