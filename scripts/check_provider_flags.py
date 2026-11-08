#!/usr/bin/env python3
"""Check that every command-line flag Donut passes to a provider CLI is still one that CLI advertises.

The unit tests use fake executables, so this is the only check that exercises the real provider
contracts. Run it with the provider CLIs installed; a missing CLI is reported and skipped.
"""

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from donut.providers import command  # noqa: E402

MODES = ("ask", "accept_edits", "auto", "full")
PROFILES = {
    "codex": [{"engine": "codex", "model": "x", "permission_mode": mode} for mode in MODES],
    "claude": [{"engine": "claude", "model": "x", "permission_mode": mode} for mode in MODES],
    "qwen": [{"engine": "ollama", "model": "x", "permission_mode": mode} for mode in MODES],
}
# Values passed to a flag must also be among the choices the CLI documents.
VALUES = {"claude": "--permission-mode", "qwen": "--approval-mode"}
HELP = {"codex": ["codex", "exec", "--help"], "claude": ["claude", "--help"], "qwen": ["qwen", "--help"]}


def flags_for(profiles):
    flags = set()
    for profile in profiles:
        for readonly in (False, True):
            args, _ = command(profile, "probe", readonly=readonly)
            flags.update(arg for arg in args[1:] if arg.startswith("--"))
    return sorted(flags)


def values_for(cli, profiles):
    if cli not in VALUES:
        return []
    values = set()
    for profile in profiles:
        args, _ = command(profile, "probe")
        if VALUES[cli] in args:
            values.add(args[args.index(VALUES[cli]) + 1])
    return sorted(values)


def main():
    failed = False
    for cli, profiles in PROFILES.items():
        if not shutil.which(cli):
            print(f"{cli}: not installed, skipped")
            continue
        result = subprocess.run(HELP[cli], capture_output=True, text=True, encoding="utf-8", errors="replace")
        help_text = result.stdout + result.stderr
        missing = [flag for flag in flags_for(profiles) if flag not in help_text]
        missing += [f"{VALUES[cli]} {value}" for value in values_for(cli, profiles) if f'"{value}"' not in help_text]
        version = subprocess.run([cli, "--version"], capture_output=True, text=True, errors="replace").stdout.strip()
        if missing:
            failed = True
            print(f"{cli} {version}: no longer documents " + ", ".join(missing))
        else:
            print(f"{cli} {version}: all {len(flags_for(profiles))} flags documented")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
