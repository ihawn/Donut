import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from donut.cli import main
from donut.config import save, config_path
from donut.runner import dependencies


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')})
        env.start()
        self.addCleanup(env.stop)
        save({'aliases': {'worker': {'engine': 'codex', 'model': ''}}})

    def test_version_flags_need_no_project_or_config(self):
        for flag in ('-v', '--version'):
            output = io.StringIO()
            with patch('sys.argv', ['donut', flag]), patch('donut.cli.release_version', return_value='1.2.3'), patch('donut.cli.load') as load, contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
                main()
            self.assertEqual(result.exception.code, 0)
            self.assertEqual(output.getvalue(), 'donut 1.2.3\n')
            load.assert_not_called()

    def test_model_and_project_flags_before_or_after_command(self):
        variants = [
            ['donut', '-m', 'worker', '-p', str(self.root), 'do', 'next', '3'],
            ['donut', 'do', 'next', '3', '--model', 'worker', '--project', str(self.root)],
        ]
        for args in variants:
            with self.subTest(args=args), patch('sys.argv', args), patch('donut.cli.run_batch', return_value=0) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
                self.assertEqual(run.call_args.args[1:], (self.root.resolve(), '3'))

    def test_local_planner_uses_default_or_explicit_alias_and_model_lifecycle(self):
        profile = {'engine':'ollama','model':'local-model','auto_approve':True}
        save({'aliases':{'local':profile}, 'planner':'local'})
        for args in (['donut','plan','a small task'], ['donut','-m','local','plan','a small task']):
            with self.subTest(args=args),patch('sys.argv',args),patch('donut.cli.install'),patch('donut.cli.activate') as activate,patch('donut.cli.run_agent',return_value=0) as run,patch.dict(os.environ,{'XDG_STATE_HOME':str(self.root/'state')}),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(),0)
                self.assertEqual(activate.call_count,2)
                self.assertEqual(run.call_args.args[0],profile)
                self.assertIn('Planning only',run.call_args.args[2])

    def test_unknown_alias_does_not_launch_agent(self):
        with patch('sys.argv', ['donut', '-m', 'missing', 'do', 'next', '1']), patch('donut.cli.run_batch') as run, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 1)
            run.assert_not_called()

    def test_invalid_profile_does_not_replace_config(self):
        original = config_path().read_bytes()
        with self.assertRaises(ValueError):
            save({'aliases': {'bad alias': {'engine': 'codex', 'model': ''}}})
        self.assertEqual(config_path().read_bytes(), original)
        self.assertEqual(json.loads(original)['aliases']['worker']['engine'], 'codex')

    def test_malformed_dependency_fails_closed(self):
        task = self.root / '001_task.md'
        task.write_text('**Slug:** `task`\n**Depends on:** ambiguous freeform dependency\n')
        with self.assertRaises(ValueError):
            dependencies(task)

    def test_missing_project_fails_without_creating_it(self):
        target = self.root / 'missing'
        with patch('sys.argv', ['donut', 'init', '-p', str(target)]), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 1)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
