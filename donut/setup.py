import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from .config import load, save
from .install_skills import install
from .providers import validate
from .ui import banner, paint


def ask(label, default=''):
    answer = input(f'{label}' + (f' [{default}]' if default else '') + ': ').strip()
    return answer or default


def yes(label, default=False):
    return ask(label + ' (y/n)', 'y' if default else 'n').lower() in ('y', 'yes')


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
        alias = ask('Alias (blank to skip)', suggested)
        if not alias: return None
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', alias):
            print('Use letters, numbers, hyphens or underscores.'); continue
        if alias in config['aliases'] and not yes(f'Replace alias {alias}?'): return None
        validate(profile)
        config['aliases'][alias] = profile
        save(config)
        return alias


def configure_provider(config):
    engine = ask('Orchestrator/provider: ollama (local), codex, claude, grok, or openai', 'codex').lower()
    if engine == 'local': engine = 'ollama'
    if engine == 'ollama':
        for binary in ('ollama', 'qwen'):
            if not shutil.which(binary): raise ValueError(f'Install {binary} to use a local orchestrator.')
        result = subprocess.run(['ollama', 'list'], capture_output=True, text=True, timeout=15, check=True)
        models = [line.split()[0] for line in result.stdout.splitlines()[1:] if line.split()]
        if not models: raise ValueError('No Ollama models found. Pull or import a model, then rerun setup.')
        print('Available local models:\n' + '\n'.join('  ' + model for model in models))
        model = ask('Local orchestrator model ID', models[0])
        if model not in models: raise ValueError('Select an installed model from the list.')
        profile = {'engine': 'ollama', 'model': model}
    elif engine in ('codex', 'claude'):
        if not shutil.which(engine): raise ValueError(f'Install {engine}, then rerun donut setup.')
        if yes(f'Run {engine} login now? Choose no if already signed in', True):
            command = ['codex', 'login'] if engine == 'codex' else ['claude', 'auth', 'login']
            subprocess.run(command, check=True)
        model = ask('Model ID (blank uses provider default)')
        profile = {'engine': engine, 'model': model}
    elif engine in ('grok', 'openai'):
        if not shutil.which('qwen'): raise ValueError('Install Qwen Code to use OpenAI-compatible endpoints.')
        print('API authentication: set the provider key in your shell or secret manager. Do not paste it here.')
        base = ask('OpenAI-compatible base URL', 'https://api.x.ai/v1' if engine == 'grok' else '')
        env = ask('Environment variable containing the API key', 'XAI_API_KEY' if engine == 'grok' else 'OPENAI_API_KEY')
        model = ask('Exact provider model ID')
        profile = {'engine': 'openai', 'model': model, 'base_url': base, 'key_env': env}
        validate(profile)
        if not os.environ.get(env): print(f'Authentication pending: export {env} before running this alias.')
    else:
        raise ValueError('Choose ollama, codex, claude, grok, or openai.')
    profile['auto_approve'] = yes('Allow this agent to run tools without approval in projects you select?')
    alias = add_alias(config, profile, engine)
    if alias:
        config['planner'] = alias
        config.setdefault('default_model', alias)
        save(config)


def setup(project, root=None):
    banner()
    print('Choose a local or hosted orchestrator to plan tickets. Cloud login is optional.')
    config = load()
    configure_provider(config)
    print(paint('\nDiscovering local models…', 'orange'), flush=True)
    models, files = discover(root)
    for model in models:
        print(f'\nOllama: {model}')
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
    if config['aliases']:
        default = ask('Default worker alias', config.get('default_model', next(iter(config['aliases']))))
        if default not in config['aliases']: raise ValueError('Unknown default alias')
        config['default_model'] = default
        save(config)
    install(project, Path.home())
    print('\nSetup saved. Run donut models, donut plan "your request", or donut -m ALIAS do next 3.')
    print('For local residency, set OLLAMA_KEEP_ALIVE=1h on your Ollama server. Donut also refreshes it after runs.')
