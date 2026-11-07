#!/bin/sh
set -eu
printf 'Installing Donut…\n'
if ! command -v uv >/dev/null 2>&1; then
  curl -fsSL https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi
if ! command -v uv >/dev/null 2>&1; then
  printf 'uv was installed but is not on PATH. Restart your shell and retry.\n' >&2
  exit 1
fi
source=${DONUT_INSTALL_SOURCE:-}
if [ -z "$source" ]; then
  metadata=$(mktemp)
  trap 'rm -f "$metadata"' EXIT HUP INT TERM
  curl -fsSL https://api.github.com/repos/ihawn/Donut/releases/latest -o "$metadata"
  source=$(uv run --no-project --python 3.13 python -c '
import json, sys
assets = json.load(open(sys.argv[1]))["assets"]
wheels = [a["browser_download_url"] for a in assets if a["name"].endswith("-py3-none-any.whl")]
if len(wheels) != 1: raise SystemExit("Expected one Donut wheel in the latest release")
print(wheels[0])
' "$metadata")
fi
uv tool install --force --python 3.13 "$source"
if [ "${CI:-}" != true ]; then uv tool update-shell; fi
printf '\nDonut installed. Open a new terminal, then run: donut setup\n'
