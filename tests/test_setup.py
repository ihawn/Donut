import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from donut import config, setup, providers
from donut.install_skills import install


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'XDG_CONFIG_HOME':str(self.root/'config')})
        self.env.start();self.addCleanup(self.env.stop)

    def test_install_idempotent_and_generic(self):
        (self.root/'QWEN.md').write_text('Keep my instructions\n')
        with contextlib.redirect_stdout(io.StringIO()):
            install(self.root,self.root)
            before=(self.root/'QWEN.md').read_bytes()
            install(self.root,self.root)
        self.assertEqual(before,(self.root/'QWEN.md').read_bytes())
        self.assertTrue((self.root/'skills/donut-planner/SKILL.md').exists())
        self.assertFalse((self.root/'AGENTS.md').exists())
        self.assertNotIn('Doodle',(self.root/'.qwen/skills/swim-lane-worker/SKILL.md').read_text())

    def test_existing_skills_get_hold_guidance_without_losing_custom_text(self):
        worker = self.root/'.qwen/skills/swim-lane-worker/SKILL.md'
        planner = self.root/'skills/donut-planner/SKILL.md'
        for file in (worker, planner):
            file.parent.mkdir(parents=True)
            file.write_text('My existing project instructions.\n')
        with contextlib.redirect_stdout(io.StringIO()):
            install(self.root, self.root)
            once = [file.read_bytes() for file in (worker, planner)]
            install(self.root, self.root)
        self.assertTrue((self.root/'swim_lanes/on_hold/.gitkeep').is_file())
        self.assertTrue((self.root/'swim_lanes/blocked/.gitkeep').is_file())
        for index, file in enumerate((worker, planner)):
            self.assertTrue(file.read_text().startswith('My existing project instructions.'))
            self.assertIn('on_hold/', file.read_text())
            self.assertEqual(file.read_bytes(), once[index])

    def test_existing_custom_hold_rules_are_preserved(self):
        worker = self.root/'.qwen/skills/swim-lane-worker/SKILL.md'
        worker.parent.mkdir(parents=True)
        worker.write_text('Custom rules for swim_lanes/on_hold/\n')
        before = worker.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()): install(self.root, self.root)
        self.assertTrue(worker.read_bytes().startswith(before))
        self.assertIn('blocked/', worker.read_text())

    @unittest.skipIf(os.name == 'nt', 'Creating symlinks needs Windows privileges')
    def test_linked_skills_are_not_modified(self):
        external = self.root/'external'
        external.mkdir()
        file = external/'SKILL.md'
        file.write_text('External custom instructions\n')
        dest = self.root/'.qwen/skills/swim-lane-worker'
        dest.parent.mkdir(parents=True)
        dest.symlink_to(external, target_is_directory=True)
        with contextlib.redirect_stdout(io.StringIO()): install(self.root, self.root)
        self.assertEqual(file.read_text(), 'External custom instructions\n')

    def test_credentials_not_saved(self):
        profile={'engine':'openai','model':'remote','base_url':'https://example.com/v1','key_env':'TEST_KEY'}
        with patch.dict(os.environ,{'TEST_KEY':'secret-fixture'}),patch('shutil.which',return_value='/bin/qwen'):
            config.save({'aliases':{'remote':profile}})
            args,env=providers.command(profile,'hello')
        self.assertNotIn('secret-fixture',config.config_path().read_text())
        self.assertNotIn('secret-fixture',' '.join(args))
        self.assertEqual(env['OPENAI_API_KEY'],'secret-fixture')
        if os.name != 'nt': self.assertEqual(config.config_path().stat().st_mode & 0o777,0o600)
        with self.assertRaises(ValueError): config.save({'aliases':{'bad':dict(profile,api_key='secret-fixture')}})

    def test_credential_urls_rejected(self):
        for url in ('https://user:password@example.com/v1','https://example.com/v1?api_key=x','http://example.com/v1'):
            with self.assertRaises(ValueError): providers.validate(dict(engine='openai',model='x',base_url=url,key_env='KEY'))

    def test_discover_only_model_locations(self):
        (self.root/'model.gguf').write_bytes(b'')
        (self.root/'private.txt').write_text('not read')
        with patch('shutil.which',return_value=None):
            models,files=setup.discover(str(self.root))
        self.assertEqual(models,[]);self.assertEqual(files,[str(self.root/'model.gguf')])

    def test_interactive_setup(self):
        answers=iter(['codex','n','','n','planner','','n','planner'])
        with patch('builtins.input',side_effect=lambda _:next(answers)),patch('shutil.which',return_value='/bin/codex'),patch('donut.setup.discover',return_value=(['local-model'],[])),patch('donut.setup.install'),patch('donut.setup.shell_aliases', return_value={}),contextlib.redirect_stdout(io.StringIO()) as output:
            setup.setup(self.root)
        self.assertTrue(output.getvalue().endswith('SETUP COMPLETE\nTry running `donut`\n'))
        saved=config.load()
        self.assertEqual(saved['planner'],'planner')
        self.assertEqual(saved['aliases']['planner']['engine'],'codex')

    def test_local_orchestrator_setup_never_requests_cloud_login(self):
        import subprocess
        answers=iter(['ollama','local-model','y','local-planner','','n','local-planner'])
        result=subprocess.CompletedProcess(['ollama','list'],0,'NAME ID SIZE MODIFIED\nlocal-model abc 1GB now\n','')
        with patch('builtins.input',side_effect=lambda _:next(answers)),patch('shutil.which',return_value='/bin/tool'),patch('donut.setup.subprocess.run',return_value=result) as calls,patch('donut.setup.discover',return_value=(['local-model'],[])),patch('donut.setup.install'),patch('donut.setup.shell_aliases', return_value={}),contextlib.redirect_stdout(io.StringIO()) as output:
            setup.setup(self.root)
        self.assertTrue(output.getvalue().endswith('SETUP COMPLETE\nTry running `donut`\n'))
        saved=config.load()
        self.assertEqual(saved['planner'],'local-planner')
        self.assertEqual(saved['aliases']['local-planner']['engine'],'ollama')
        self.assertEqual([call.args[0] for call in calls.call_args_list],[['ollama','list']])

    def test_claude_login_folder_used_for_login_and_execution(self):
        personal = str((self.root / '.claude-personal').resolve())
        answers = iter(['claude', 'Custom path', personal, 'y', 'opus', 'n', 'personal'])
        with patch('builtins.input', side_effect=lambda _: next(answers)), patch('shutil.which', return_value='/bin/claude'), patch('donut.setup.subprocess.run') as login, contextlib.redirect_stdout(io.StringIO()):
            setup.configure_provider({'aliases': {}})
            profile = config.load()['aliases']['personal']
            _, env = providers.command(profile, 'test')
        self.assertEqual(login.call_args.kwargs['env']['CLAUDE_CONFIG_DIR'], personal)
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], personal)
        self.assertEqual(profile['claude_config_dir'], personal)

    def test_existing_aliases_preserved_when_selecting_roles(self):
        aliases = {'planner': {'engine': 'codex'}, 'fast': {'engine': 'ollama', 'model': 'local'}}
        config.save({'aliases': aliases, 'planner': 'planner', 'default_model': 'fast'})
        answers = iter(['1', '', 'n', '2'])
        with patch('builtins.input', side_effect=lambda _: next(answers)), patch('donut.setup.discover', return_value=(['local'], [])), patch('donut.setup.install'), patch('donut.setup.shell_aliases', return_value={}), contextlib.redirect_stdout(io.StringIO()):
            setup.setup(self.root)
        self.assertEqual(config.load()['aliases'], aliases)
        self.assertEqual(config.load()['default_model'], 'fast')

    def test_claude_directory_rejects_wrong_provider_and_invalid_path(self):
        for profile in ({'engine': 'codex', 'claude_config_dir': '/tmp/x'}, {'engine': 'claude', 'claude_config_dir': ''}, {'engine': 'claude', 'claude_config_dir': 42}):
            with self.assertRaises(ValueError): providers.validate(profile)

    def test_shell_alias_discovery_does_not_execute_code(self):
        (self.root / '.zshrc').write_text("alias claude-personal='CLAUDE_CONFIG_DIR=\"$HOME/.claude-personal\" claude'\n"
            "alias coding='codex'\n"
            "alias unsafe='CLAUDE_CONFIG_DIR=$(touch /tmp/no) claude'\n"
            "alias chained='claude; echo no'\n"
            "alias key='API_KEY=secret claude'\n")
        found = setup.shell_aliases(self.root)
        self.assertEqual(set(found), {'claude-personal', 'coding'})
        self.assertEqual(found['claude-personal']['claude_config_dir'], str((self.root / '.claude-personal').resolve()))

    def test_shell_alias_selected_as_planner(self):
        personal = str((self.root / '.claude-personal').resolve())
        discovered = {'claude-personal': {'engine': 'claude', 'model': '', 'claude_config_dir': personal}}
        answers = iter(['claude-personal', 'n', 'opus', 'n', '', 'n', 'claude-personal'])
        with patch('builtins.input', side_effect=lambda _: next(answers)), patch('shutil.which', return_value='/bin/claude'), patch('donut.setup.shell_aliases', return_value=discovered), patch('donut.setup.discover', return_value=([], [])), patch('donut.setup.install'), contextlib.redirect_stdout(io.StringIO()):
            setup.setup(self.root)
        saved = config.load()
        self.assertEqual(saved['planner'], 'claude-personal')
        self.assertEqual(saved['aliases']['claude-personal']['claude_config_dir'], personal)

    def test_provider_commands_do_not_resume(self):
        for engine in ('codex','claude','ollama'):
            with patch('shutil.which',return_value='/bin/agent'):
                args,_=providers.command({'engine':engine,'model':'x'},'task')
            self.assertNotIn('--resume',args);self.assertNotIn('--continue',args)
            self.assertNotIn('--dangerously-skip-permissions',args)

    def test_multiline_prompts_are_not_put_on_command_line(self):
        prompt = 'First line\n"quoted" & second line'
        for engine in ('codex','claude','ollama'):
            with patch('shutil.which',return_value='/bin/agent'):
                args,_=providers.command({'engine':engine,'model':'test'},prompt)
            self.assertNotIn(prompt,args)
            self.assertTrue(all('\n' not in arg for arg in args))

    def test_missing_key_rejected(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(ValueError): providers.command(dict(engine='openai',model='x',base_url='https://example.com',key_env='MISSING_KEY'),'test')

if __name__=='__main__':unittest.main()
