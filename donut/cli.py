import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from .config import load
from .install_skills import install
from .providers import validate, activate
from .runner import run_agent, run_batch
from .setup import setup, discover
from .ui import banner, welcome, release_version, paint


def plan_session(profile, alias, project, request=None):
    install(project, Path.home())
    state = Path(os.environ.get('XDG_STATE_HOME', Path.home()/'.local/state'))/'donut'
    state.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix='plan-', dir=state))
    print(f'Planner: {alias}\nProject: {project}\nLogs: {run}')
    interactive = request is None
    if interactive:
        print('Describe what to plan. Enter /exit to leave (or Ctrl-D; Windows: Ctrl-Z then Enter).')
    history = []
    while True:
        if interactive:
            try:
                request = input(paint('\nPlan > ', 'teal')).strip()
            except EOFError:
                return 0
            if request in ('/exit', '/quit'): return 0
            if not request: continue
        prompt = ('Planning only: read skills/donut-planner/SKILL.md and project instructions. '
                  'This explicit planner assignment overrides worker-only prohibitions on creating tickets. '
                  'Research the request and write scoped tickets in swim_lanes/to_do/. Inspect existing tickets '
                  'before adding or updating them; preserve work already in progress or done. Do not implement, '
                  'commit or push. Earlier user requests in this planning session:\n' + json.dumps(history) +
                  '\nUser request:\n' + request)
        if history:
            prompt += f'\nPrevious planner output (consult for follow-up context): {run / f"{len(history):03}.log"}'
        activate(profile)
        try:
            code = run_agent(profile, project, prompt, run/f'{len(history) + 1:03}.log')
        finally:
            if profile['engine'] == 'ollama': activate(profile)
        if code:
            print(paint('PLANNING FAILED\nCheck the output and logs before running workers.', 'coral'))
            return code
        history.append(request)
        print(paint('\nPLANNING COMPLETE', 'teal'))
        print('Try exiting the shell and running `donut --work next 1`')
        if not interactive: return 0


def main():
    # Shared flags work both before and after the subcommand.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument('-m', '--model', default=argparse.SUPPRESS, help='configured model alias')
    shared.add_argument('-p', '--project', default=argparse.SUPPRESS, help='project path (default: current directory)')
    parser = argparse.ArgumentParser(prog='donut', parents=[shared], description='Your planning model writes the tickets. Your worker model takes them one at a time. Do the thing. Local or hosted coding agents.')
    parser.add_argument('-v', '--version', action='version', version='donut ' + release_version())
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--plan', nargs='?', const='', metavar='REQUEST', help='open interactive planner; optionally plan one quoted request')
    modes.add_argument('--work', nargs='*', metavar='NEXT_COUNT', help='work next COUNT tickets (for example: --work next 1); bare --work runs all')
    sub = parser.add_subparsers(dest='action')
    init = sub.add_parser('setup', parents=[shared], help='choose local/hosted orchestrator, discover models and set up project')
    init.add_argument('--scan', help='additional directory to scan for model files')
    sub.add_parser('welcome', help='show the installation welcome screen')
    sub.add_parser('models', parents=[shared], help='list configured aliases')
    scan = sub.add_parser('discover', parents=[shared], help='find Ollama models and GGUF/Safetensors files')
    scan.add_argument('--scan', help='directory to scan instead of common model folders')
    for name in ('init', 'add-skills'):
        sub.add_parser(name, parents=[shared], help='install planner/worker skills and swim lanes')
    work = sub.add_parser('do', parents=[shared], help='compatibility form of --work COUNT')
    work.add_argument('next', choices=['next'])
    work.add_argument('count', help='positive number or all')
    plan = sub.add_parser('plan', parents=[shared], help='compatibility form of --plan REQUEST')
    plan.add_argument('request', help='quoted description of the work to plan')
    args = parser.parse_args()
    if args.plan is not None or args.work is not None:
        if args.action: parser.error('Use --plan or --work without a subcommand.')
        if args.plan is not None:
            args.action, args.request = 'plan', args.plan or None
        else:
            parts = args.work
            if len(parts) == 2 and parts[0] == 'next': parts = parts[1:]
            if not parts:
                count = 'all'
            elif len(parts) == 1 and (parts[0] == 'all' or (parts[0].isdecimal() and int(parts[0]) > 0)):
                count = parts[0]
            else:
                parser.error('Use --work next COUNT, where COUNT is a positive integer or all.')
            args.action, args.count = 'do', count
    if not args.action: parser.error('Choose --plan, --work, or a subcommand such as setup.')
    if args.action == 'welcome':
        welcome(installed=True)
        return 0
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
        return plan_session(profile, alias, project, args.request)
    except KeyboardInterrupt:
        print('\nCancelled.', file=sys.stderr); return 130
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(paint(f'donut: {error}', 'coral'), file=sys.stderr); return 1
