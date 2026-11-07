"""Compatibility for existing qwen/qwen-faster shell launchers."""
import argparse
from pathlib import Path
import subprocess
import sys
from .runner import run_batch
from .ui import banner


def main():
    parser = argparse.ArgumentParser(description='Legacy launcher; prefer donut -m ALIAS --work COUNT.')
    parser.add_argument('--model', required=True)
    parser.add_argument('-p', '--project', default='.')
    parser.add_argument('build', choices=['build'])
    parser.add_argument('next', choices=['next'])
    parser.add_argument('count')
    args = parser.parse_args()
    try:
        banner()
        return run_batch({'engine': 'ollama', 'model': args.model, 'auto_approve': True},
                         Path(args.project).expanduser().resolve(), args.count)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'donut: {error}', file=sys.stderr)
        return 1
