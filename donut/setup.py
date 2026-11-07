import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
from .config import load, save
from .install_skills import install
from .providers import validate
from .ui import welcome, paint


def ask(label, default=''):
    answer = input(paint('  ' + label, 'gray') + (paint(f' [{default}]', 'orange') if default else '') + paint(' > ', 'teal')).strip()
    return answer or default


def yes(label, default=False):
    return ask(label + ' (y/n)', 'y' if default else 'n').lower() in ('y', 'yes')


def choose(label, options, default=None):
    for index, option in enumerate(options, 1):
        print(paint(f'  {index:>2}  ', 'teal') + option)
    while True:
        value = ask(label + ' (number or name)', default or options[0])
        if value.isdecimal() and 1 <= int(value) <= len(options):
            return options[int(value) - 1]
        if value in options: return value
        print(paint('Choose one of the listed options.', 'coral'))


def shell_aliases(home=None):
    """Import simple provider aliases without sourcing or executing shell code."""
    home = Path(home) if home else Path.home()
    aliases = {}
    for filename in ('.bash_profile', '.bashrc', '.zshrc'):
        path = home / filename
        if not path.is_file(): continue
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            if not re.match(r'^\s*alias\s+', line): continue
            try:
                declaration = shlex.split(line, comments=True)
                if len(declaration) != 2: continue
                name, value = declaration[1].split('=', 1)
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', name): continue
                words = shlex.split(value)
                if words[:1] == ['env']: words = words[1:]
                directory = None
                if words and words[0].startswith('CLAUDE_CONFIG_DIR='):
                    directory = words.pop(0).split('=', 1)[1]
                if len(words) != 1 or words[0] not in ('claude', 'codex'): continue
                if directory is not None and words[0] != 'claude': continue
                profile = {'engine': words[0], 'model': ''}
                if directory is not None:
                    directory = directory.replace('${HOME}', str(home)).replace('$HOME', str(home))
                    if directory.startswith('~/'): directory = str(home / directory[2:])
                    if not directory or any(c in directory for c in '$`\n\0') or not Path(directory).is_absolute(): continue
                    profile['claude_config_dir'] = str(Path(directory).resolve())
                aliases[name] = profile
            except ValueError:
                continue
    return aliases


def claude_directory():
    current = str(Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home()/'.claude')).expanduser().resolve())
    paths = list(dict.fromkeys([current, str(Path.home()/'.claude')] +
                 [str(p) for p in sorted(Path.home().glob('.claude-*')) if p.is_dir()]))
    print('  Claude login is selected by its configuration folder, not the Donut alias.')
    selected = choose('Claude login folder', paths + ['Custom path'], current)
    if selected == 'Custom path':
        selected = ask('Configuration folder (existing login or a new folder)')
        if not selected: raise ValueError('A configuration folder is required')
    return str(Path(selected).expanduser().resolve())


def discover(root=None):
    models = []
    if shutil.which('ollama'):
        result = subprocess.run(['ollama', 'list'], capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            models = [line.split()[0] for line in result.stdout.splitlines()[1:] if line.split()]
    roots = [Path(root).expanduser()] if root else [Path.home()/'Models', Path.home()/'Downloads',
             Path.home()/'.cache/huggingface/hub', Path.home()/'.lmstudio/models', Path.home()/'.ollama/models']
    files = []
    # Only filenames in model locations; never read unrelated user documents or weights.
    for base in roots:
        if not base.is_dir(): continue
        for folder, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in ('.git', 'node_modules', '.venv')]
            for name in names:
                if name.lower().endswith('.gguf'):
                    files.append(str(Path(folder)/name))
                elif name == 'config.json' and any(n.endswith('.safetensors') for n in names):
                    files.append(str(Path(folder)))
    return models, sorted(set(files))


def add_alias(config, profile, suggested=''):
    while True:
        alias = ask('Donut alias (Enter keeps suggestion)' if suggested else 'Donut alias (Enter skips)', suggested)
        if not alias: return None
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', alias):
            print('Use letters, numbers, hyphens or underscores.'); continue
        if alias in config['aliases'] and not yes(f'Replace alias {alias}?'): return None
        validate(profile)
        config['aliases'][alias] = profile
        save(config)
        return alias


def configure_provider(config, preset=None, suggested=None):
    engine = preset["engine"] if preset else choose('Provider', ['codex', 'claude', 'ollama', 'grok', 'openai'], 'codex')
    if engine == 'ollama':
        for binary in ('ollama', 'qwen'):
            if not shutil.which(binary): raise ValueError(f'Install {binary} to use a local orchestrator.')
        result = subprocess.run(['ollama', 'list'], capture_output=True, text=True, timeout=15, check=True)
        models = [line.split()[0] for line in result.stdout.splitlines()[1:] if line.split()]
        if not models: raise ValueError('No Ollama models found. Pull or import a model, then rerun setup.')
        model = choose('Local model', models)
        if model not in models: raise ValueError('Select an installed model from the list.')
        profile = {'engine': 'ollama', 'model': model}
    elif engine in ('codex', 'claude'):
        if not shutil.which(engine): raise ValueError(f'Install {engine}, then rerun donut setup.')
        directory = (preset.get('claude_config_dir') if preset else None) or (claude_directory() if engine == 'claude' else None)
        if directory: print('  Claude login folder: ' + directory)
        login_env = os.environ.copy()
        if directory: login_env['CLAUDE_CONFIG_DIR'] = directory
        print(f'  Executable: {shutil.which(engine)}')
        if yes(f'Run {engine} login now? Choose no to reuse this login', False):
            command = ['codex', 'login'] if engine == 'codex' else ['claude', 'auth', 'login']
            subprocess.run(command, check=True, env=login_env)
        model = ask('Planner model ID (blank uses provider default)')
        profile = {'engine': engine, 'model': model}
        if directory: profile['claude_config_dir'] = directory
    elif engine in ('grok', 'openai'):
        if not shutil.which('qwen'): raise ValueError('Install Qwen Code to use OpenAI-compatible endpoints.')
        print('API authentication: set the provider key in your shell or secret manager. Do not paste it here.')
        base = ask('OpenAI-compatible base URL', 'https://api.x.ai/v1' if engine == 'grok' else '')
        env = ask('Environment variable containing the API key', 'XAI_API_KEY' if engine == 'grok' else 'OPENAI_API_KEY')
        model = ask('Planner model ID (exact provider ID)')
        profile = {'engine': 'openai', 'model': model, 'base_url': base, 'key_env': env}
        validate(profile)
        if not os.environ.get(env): print(f'Authentication pending: export {env} before running this alias.')
    else:
        raise ValueError('Choose ollama, codex, claude, grok, or openai.')
    profile['auto_approve'] = yes('Allow this agent to run tools without approval in projects you select?')
    print('  An alias is a nickname for these settings; it does not change your account.')
    alias = add_alias(config, profile, suggested or engine)
    if alias:
        config['planner'] = alias
        config.setdefault('default_model', alias)
        save(config)


def setup(project, root=None):
    welcome()
    print('  Planner: writes tickets. Worker: builds one ticket per fresh context.')
    print('  One model can do both jobs. Existing aliases are kept unless you replace them.')
    print(paint('\n  1 / 3  CHOOSE YOUR PLANNER\n  ' + '─' * 42, 'teal'))
    config = load()
    discovered = {name: profile for name, profile in shell_aliases().items() if name not in config['aliases']}
    if config['aliases'] or discovered:
        if discovered: print('  Shell aliases found: ' + ', '.join(discovered))
        selected = choose('Planner alias', list(config['aliases']) + list(discovered) + ['Add new provider'], config.get('planner') or 'Add new provider')
        if selected == 'Add new provider': configure_provider(config)
        elif selected in discovered: configure_provider(config, discovered[selected], selected)
        else: config['planner'] = selected
    else:
        configure_provider(config)
    print(paint('\n  2 / 3  ADD WORKER MODELS\n  ' + '─' * 42, 'teal'))
    print(paint('  Discovering local models…', 'orange'), flush=True)
    models, files = discover(root)
    for index, model in enumerate(models, 1):
        aliases = [name for name, p in config['aliases'].items() if p['engine'] == 'ollama' and p.get('model') == model]
        print(paint(f'  {index:>2}  ', 'teal') + model + paint('  [' + (', '.join(aliases) or 'not configured') + ']', 'orange'))
    if models:
        print('  Select only the models you want to add. Different names may refer to the same weights.')
        while True:
            selected = ask('Model numbers, separated by spaces (Enter skips)')
            numbers = selected.split()
            if all(n.isdecimal() and 1 <= int(n) <= len(models) for n in numbers): break
            print(paint('Use model numbers from the list.', 'coral'))
        for number in dict.fromkeys(numbers):
            model = models[int(number) - 1]
            print(paint('  ' + model, 'orange'))
            add_alias(config, {'engine': 'ollama', 'model': model, 'auto_approve': True})
    for filename in files:
        print(f'\nModel files: {filename}')
        if filename.endswith('.gguf') and shutil.which('ollama') and yes('Import this GGUF into Ollama?'):
            name = ask('Ollama model name')
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]*', name): raise ValueError('Invalid Ollama model name')
            if '\n' in filename or '"' in filename: raise ValueError('Unsupported characters in model path')
            with tempfile.TemporaryDirectory() as temp:
                modelfile = Path(temp)/'Modelfile'
                modelfile.write_text('FROM "' + filename + '"\n')
                subprocess.run(['ollama', 'create', name, '-f', str(modelfile)], check=True)
            add_alias(config, {'engine': 'ollama', 'model': name, 'auto_approve': True})
        elif not filename.endswith('.gguf'):
            print('Safetensors directory found. Serve it through an OpenAI-compatible runtime, then add its endpoint.')
    while yes('Add another local or hosted model?'):
        planner = config.get('planner')
        configure_provider(config)
        config['planner'] = planner
        save(config)
    print(paint('\n  3 / 3  CHOOSE YOUR DEFAULT WORKER\n  ' + '─' * 42, 'teal'))
    if config['aliases']:
        default = choose('Worker alias', list(config['aliases']), config.get('default_model', next(iter(config['aliases']))))
        if default not in config['aliases']: raise ValueError('Unknown default alias')
        config['default_model'] = default
        save(config)
    install(project, Path.home())
    print(paint('\n  READY  /  Setup saved', 'teal'))
    for role, key in [('Planner', 'planner'), ('Worker', 'default_model')]:
        alias = config.get(key)
        if alias:
            profile = config['aliases'][alias]
            print(f'  {role:7} {alias}  →  {profile.get("model") or profile["engine"] + " default"}')
            if profile.get('claude_config_dir'): print('          Login folder: ' + profile['claude_config_dir'])
    print('For local residency, set OLLAMA_KEEP_ALIVE=1h on your Ollama server. Donut also refreshes it after runs.')
    print(paint('\nSETUP COMPLETE', 'teal'))
    print('Try running `donut --plan`')
