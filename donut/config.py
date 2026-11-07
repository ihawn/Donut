import json
import os
from pathlib import Path
import re
import tempfile
from .providers import validate


def config_path():
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'donut/config.json'


def load():
    path = config_path()
    return json.loads(path.read_text()) if path.exists() else {'aliases': {}}


def save(config):
    for alias, profile in config['aliases'].items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', alias):
            raise ValueError('Aliases may contain letters, numbers, underscores and hyphens.')
        validate(profile)
        allowed = {'engine', 'model', 'base_url', 'key_env', 'auto_approve'}
        if set(profile) - allowed: raise ValueError('Unexpected profile fields; credentials cannot be saved.')
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as file:
            json.dump(config, file, indent=2)
            file.write('\n')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)
