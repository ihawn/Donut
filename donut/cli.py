import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from .config import load
from .install_skills import install
from .providers import validate
from .runner import run_agent, run_batch
from .setup import setup, discover
from .ui import banner


def main():
    # Shared flags work both before and after the subcommand.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument('-m', '--model', default=argparse.SUPPRESS, help='configured model alias')
    shared.add_argument('-p', '--project', default=argparse.SUPPRESS, help='project path (default: current directory)')
    parser = argparse.ArgumentParser(prog='donut', parents=[shared], description='Small tickets. Fresh contexts. Local or hosted coding agents.')
    sub = parser.add_subparsers(dest='action', required=True)
    init = sub.add_parser('setup', parents=[shared], help='interactive authentication, model discovery and project setup')
    init.add_argument('--scan', help='additional directory to scan for model files')
    sub.add_parser('models', parents=[shared], help='list configured aliases')
    scan = sub.add_parser('discover', parents=[shared], help='find Ollama models and GGUF/Safetensors files')
    scan.add_argument('--scan', help='directory to scan instead of common model folders')
    for name in ('init', 'add-skills'):
        sub.add_parser(name, parents=[shared], help='install planner/worker skills and swim lanes')
    work = sub.add_parser('do', parents=[shared], help='execute tickets in fresh agent sessions')
    work.add_argument('next', choices=['next'])
    work.add_argument('count', help='positive number or all')
    plan = sub.add_parser('plan', parents=[shared], help='ask the planning agent to research and write tickets')
    plan.add_argument('request', help='quoted description of the work to plan')
    args = parser.parse_args()
    project = Path(getattr(args, 'project', '.')).expanduser().resolve()
    try:
        if not project.is_dir(): raise ValueError(f'Project directory does not exist: {project}')
        if args.action == 'setup':
            setup(project, args.scan); return 0
        if args.action in ('init', 'add-skills'):
            banner(); install(project, Path.home()); return 0
        if args.action == 'discover':
            banner(); models, files = discover(args.scan)
            for model in models: print('Ollama: ' + model)
            for file in files: print('Model file: ' + file)
            if not models and not files: print('No models found in the searched locations.')
            return 0
        config = load()
        if args.action == 'models':
            for name, profile in config['aliases'].items():
                print(f'{name:20} {profile["engine"]:10} {profile.get("model") or "provider default"}')
            if not config['aliases']: print('No aliases yet. Run donut setup.')
            return 0
        alias = getattr(args, 'model', None) or config.get('planner' if args.action == 'plan' else 'default_model')
        if alias not in config['aliases']: raise ValueError('Choose a configured alias with -m, or run donut setup.')
        profile = config['aliases'][alias]
        validate(profile)
        banner()
        if args.action == 'do': return run_batch(profile, project, args.count)
        install(project, Path.home())
        prompt = ('Planning only: read skills/donut-planner/SKILL.md and project instructions. '
                  'This explicit planner assignment overrides worker-only prohibitions on creating tickets. '
                  'Research the request and write scoped tickets in swim_lanes/to_do/. Do not implement, '
                  'commit or push. User request:\n' + args.request)
        state = Path(os.environ.get('XDG_STATE_HOME', Path.home()/'.local/state'))/'donut'
        state.mkdir(parents=True, exist_ok=True)
        run = Path(tempfile.mkdtemp(prefix='plan-', dir=state))
        print(f'Planner: {alias}\nLogs: {run}')
        return run_agent(profile, project, prompt, run/'planner.log')
    except KeyboardInterrupt:
        print('\nCancelled.', file=sys.stderr); return 130
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'donut: {error}', file=sys.stderr); return 1
