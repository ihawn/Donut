"""Provider CLIs own authentication and execution; no credentials in config."""
import os
import re
import shutil
import subprocess
from urllib.parse import urlsplit


def validate(profile):
    engine = profile.get('engine')
    if engine not in ('ollama', 'codex', 'claude', 'openai'):
        raise ValueError(f'Unsupported engine: {engine}')
    if not isinstance(profile.get('model', ''), str):
        raise ValueError('Model must be a string')
    if engine in ('ollama', 'openai') and not profile.get('model'):
        raise ValueError('A model ID is required')
    if engine == 'openai':
        url = urlsplit(profile.get('base_url', ''))
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('Use a base URL without credentials, query parameters or fragments')
        if url.scheme == 'http' and url.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('Remote provider URLs must use HTTPS')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', profile.get('key_env', '')):
            raise ValueError('key_env must be an environment variable name, never the key itself')


def command(profile, prompt, readonly=False):
    validate(profile)
    engine, model = profile['engine'], profile.get('model')
    env = os.environ.copy()
    if engine == 'codex':
        args = ['codex', 'exec', '--json', '--ephemeral', '--skip-git-repo-check']
        args += ['--sandbox', 'read-only'] if readonly else (['--dangerously-bypass-approvals-and-sandbox'] if profile.get('auto_approve') else ['--sandbox', 'workspace-write'])
        if model: args += ['--model', model]
        args += ['-']
    elif engine == 'claude':
        args = ['claude', '--print', '--verbose', '--output-format', 'stream-json',
                '--include-partial-messages', '--no-session-persistence']
        args += ['--permission-mode', 'plan', '--tools', ''] if readonly else (['--dangerously-skip-permissions'] if profile.get('auto_approve') else ['--permission-mode', 'acceptEdits', '--permission-prompts', 'none'])
        if model: args += ['--model', model]
        args += ['Follow the request on stdin.']
    else:
        args = ['qwen', '--auth-type', 'openai', '--model', model, '--output-format',
                'stream-json', '--include-partial-messages']
        args += ['--approval-mode', 'plan', '--max-tool-calls', '0'] if readonly else (['--yolo'] if profile.get('auto_approve') else ['--approval-mode', 'auto-edit'])
        if engine == 'ollama':
            env['OPENAI_API_KEY'] = 'ollama'
            args += ['--openai-base-url', 'http://localhost:11434/v1']
        else:
            key = os.environ.get(profile['key_env'])
            if not key: raise ValueError(f"Set {profile['key_env']} before using this alias (Donut never saves its value).")
            env['OPENAI_API_KEY'] = key
            args += ['--openai-base-url', profile['base_url']]
        args += ['--prompt', 'Follow the request on stdin.']
    binary = shutil.which(args[0])
    if not binary: raise ValueError(f'Install {args[0]} before using this alias.')
    args[0] = binary
    return args, env


def activate(profile):
    if profile['engine'] != 'ollama': return
    result = subprocess.run(['ollama', 'ps'], capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if parts and parts[0] != profile['model']:
            subprocess.run(['ollama', 'stop', parts[0]], check=True, stdout=subprocess.DEVNULL)
    # Ollama's CLI sets a per-request residency timeout. Subsequent Qwen requests
    # use the server default; setup documents OLLAMA_KEEP_ALIVE=1h for that server.
    subprocess.run(['ollama', 'run', profile['model'], '--keepalive', '1h', ''],
                   check=True, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
