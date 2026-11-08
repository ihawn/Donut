"""CI: upgrade the isolated tool install and preserve user configuration."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
project = root / "pyproject.toml"
original = project.read_text(encoding="utf-8")
with tempfile.TemporaryDirectory() as temp:
    temp = Path(temp)
    env = dict(os.environ, XDG_CONFIG_HOME=str(temp / "config"), CI="true")
    config = temp / "config/donut/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"aliases": {"keep-me": {"engine": "codex"}}}), encoding="utf-8")
    before = config.read_bytes()
    try:
        import re

        project.write_text(
            re.sub(r'^version = ".*"$', 'version = "999.0.0"', original, count=1, flags=re.M), encoding="utf-8"
        )
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(temp / "dist"), str(root)],
            check=True,
        )
    finally:
        project.write_text(original, encoding="utf-8")
    env["DONUT_INSTALL_SOURCE"] = str(next((temp / "dist").glob("*.whl")))
    command = ["pwsh", "-File", str(root / "install.ps1")] if os.name == "nt" else ["sh", str(root / "install.sh")]
    subprocess.run(command, env=env, check=True)
    binary = Path(env["UV_TOOL_BIN_DIR"]) / ("donut.exe" if os.name == "nt" else "donut")
    version = subprocess.check_output([str(binary), "-v"], text=True).strip()
    assert version == "donut 999.0.0", version
    assert config.read_bytes() == before, "Upgrade changed user configuration"
    print("PASS: installer upgraded Donut and preserved configuration")
