"""Choose a stable release version; pyproject's version is the minimum."""

import re
import subprocess
from pathlib import Path


def parse(value):
    match = re.fullmatch(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)
    return tuple(map(int, match.groups())) if match else None


def next_version(base, tags, head_tags=()):
    minimum = parse(base)
    if minimum is None:
        raise ValueError("Release version must be a stable major.minor.patch version")
    existing = [version for tag in tags if (version := parse(tag)) is not None]
    current = [version for tag in head_tags if (version := parse(tag)) is not None]
    if current:
        return ".".join(map(str, max(current)))
    latest = max(existing, default=(-1, 0, 0))
    selected = minimum if minimum > latest else (latest[0], latest[1], latest[2] + 1)
    return ".".join(map(str, selected))


if __name__ == "__main__":
    import tomllib

    base = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    tags = subprocess.check_output(["git", "tag", "--list", "v*"], text=True).splitlines()
    head = subprocess.check_output(["git", "tag", "--points-at", "HEAD"], text=True).splitlines()
    print(next_version(base, tags, head))
