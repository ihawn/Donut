"""Provider CLIs own authentication and execution; no credentials in config."""

import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit


# Donut's permission modes, applied through each provider's own mechanism.
PERMISSION_MODES = {
    "ask": ("Ask", "Ask before file edits and commands"),
    "accept_edits": ("Accept edits", "Edit files without asking; ask before commands"),
    "auto": ("Auto", "The provider's own reviewer approves safe actions and blocks risky ones"),
    "full": ("Full access", "Run everything without asking; only in projects you trust"),
}
# Shift+Tab cycles these, as in Claude Code. Full access is only reachable from /permissions.
PERMISSION_CYCLE = ("ask", "accept_edits", "auto")

NATIVE_PERMISSIONS = {
    # Claude Code permission modes (SDK and --permission-mode).
    "claude": {"ask": "default", "accept_edits": "acceptEdits", "auto": "auto", "full": "bypassPermissions"},
    # Qwen Code --approval-mode values; its auto mode is an LLM classifier.
    "qwen": {"ask": "default", "accept_edits": "auto-edit", "auto": "auto", "full": "yolo"},
    # Codex app-server (approvalPolicy, sandbox, approvalsReviewer); auto_review is its reviewing subagent.
    "codex": {
        "ask": ("untrusted", "workspace-write", "user"),
        "accept_edits": ("on-request", "workspace-write", "user"),
        "auto": ("on-request", "workspace-write", "auto_review"),
        "full": ("never", "danger-full-access", "user"),
    },
}


def provider_family(engine):
    return "qwen" if engine in ("ollama", "openai") else engine


def native_permission(engine, mode):
    return NATIVE_PERMISSIONS[provider_family(engine)][mode]


def batch_permission(profile):
    """The mode for an unattended run, which cannot answer prompts, so Ask behaves as Accept edits."""
    mode = profile.get("permission_mode") or ("full" if profile.get("auto_approve") else "accept_edits")
    return "accept_edits" if mode == "ask" else mode


def validate(profile):
    engine = profile.get("engine")
    if engine not in ("ollama", "codex", "claude", "openai"):
        raise ValueError(f"Unsupported engine: {engine}")
    if not isinstance(profile.get("model", ""), str):
        raise ValueError("Model must be a string")
    if engine in ("ollama", "openai") and not profile.get("model"):
        raise ValueError("A model ID is required")
    if "permission_mode" in profile and profile["permission_mode"] not in PERMISSION_MODES:
        raise ValueError("permission_mode must be one of: " + ", ".join(PERMISSION_MODES))
    if "effort" in profile and not re.fullmatch(r"[a-z]{1,16}", str(profile["effort"])):
        raise ValueError("effort must be a provider effort level such as low, medium or high")
    if "claude_config_dir" in profile:
        directory = profile["claude_config_dir"]
        if engine != "claude" or not isinstance(directory, str) or not directory.strip() or "\0" in directory:
            raise ValueError("claude_config_dir must be a nonempty Claude configuration path")
    if engine == "openai":
        url = urlsplit(profile.get("base_url", ""))
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Use a base URL without credentials, query parameters or fragments")
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Remote provider URLs must use HTTPS")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", profile.get("key_env", "")):
            raise ValueError("key_env must be an environment variable name, never the key itself")


def command(profile, prompt, readonly=False, permission=None, review=False):
    """Build the provider command. permission is a Donut mode; unset means the profile's unattended mode.

    readonly gives no tools at all (run summaries). review allows reading the project but never changing it.
    """
    validate(profile)
    engine, model = profile["engine"], profile.get("model")
    permission = permission or batch_permission(profile)
    env = os.environ.copy()
    if engine == "claude" and profile.get("claude_config_dir"):
        env["CLAUDE_CONFIG_DIR"] = str(Path(profile["claude_config_dir"]).expanduser().resolve())
    if engine == "codex":
        args = ["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check"]
        if readonly or review:
            args += ["--sandbox", "read-only"]
        elif permission == "full":
            args += ["--dangerously-bypass-approvals-and-sandbox"]
        elif permission == "auto":
            args += ["--approve-for-me"]
        else:
            args += ["--sandbox", "workspace-write"]
        if model:
            args += ["--model", model]
        if profile.get("effort"):
            args += ["-c", f'model_reasoning_effort="{profile["effort"]}"']
        args += ["-"]
    elif engine == "claude":
        args = [
            "claude",
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--no-session-persistence",
        ]
        if readonly:
            args += ["--permission-mode", "plan", "--tools", ""]
        elif review:
            args += ["--tools", "Read,Grep,Glob", "--permission-prompts", "none"]
        elif permission == "full":
            args += ["--dangerously-skip-permissions"]
        else:
            args += ["--permission-mode", native_permission("claude", permission), "--permission-prompts", "none"]
        if model:
            args += ["--model", model]
        if profile.get("effort"):
            args += ["--effort", profile["effort"]]
        args += ["Follow the request on stdin."]
    else:
        args = [
            "qwen",
            "--auth-type",
            "openai",
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--include-partial-messages",
        ]
        if readonly:
            args += ["--approval-mode", "plan", "--max-tool-calls", "0"]
        elif review:
            args += ["--approval-mode", "plan"]
        elif permission == "full":
            args += ["--yolo"]
        else:
            args += ["--approval-mode", native_permission("qwen", permission)]
        if engine == "ollama":
            env["OPENAI_API_KEY"] = "ollama"
            args += ["--openai-base-url", ollama_base_url()]
        else:
            key = os.environ.get(profile["key_env"])
            if not key:
                raise ValueError(f"Set {profile['key_env']} before using this alias (Donut never saves its value).")
            env["OPENAI_API_KEY"] = key
            args += ["--openai-base-url", profile["base_url"]]
        args += ["--prompt", "Follow the request on stdin."]
    binary = shutil.which(args[0])
    if not binary:
        raise ValueError(f"Install {args[0]} before using this alias.")
    args[0] = binary
    return args, env


def ollama_base_url():
    """The OpenAI-compatible endpoint of the local Ollama server, honoring OLLAMA_HOST like the Ollama CLI does."""
    host = os.environ.get("OLLAMA_HOST", "").strip() or "127.0.0.1:11434"
    if "://" not in host:
        host = "http://" + host
    if not urlsplit(host).port:
        host = host.rstrip("/") + ":11434"
    return host.rstrip("/") + "/v1"


def _ollama(*args):
    """Run an Ollama command quietly. Its progress spinner goes to stderr, so keep stderr for errors only."""
    result = subprocess.run(
        ["ollama", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode:
        detail = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|[\u2800-\u28ff]", "", result.stderr).strip().splitlines()
        raise ValueError(f"ollama {args[0]} failed: " + (detail[-1] if detail else f"exit {result.returncode}"))
    return result.stdout


def activate(profile):
    """Load the alias's Ollama model. Other loaded models are stopped only when the alias opts in."""
    if profile["engine"] != "ollama":
        return
    if profile.get("unload_others"):
        for line in _ollama("ps").splitlines()[1:]:
            parts = line.split()
            if parts and parts[0] != profile["model"]:
                _ollama("stop", parts[0])
    # Ollama's CLI sets a per-request residency timeout. Subsequent Qwen requests
    # use the server default; setup documents OLLAMA_KEEP_ALIVE=1h for that server.
    _ollama("run", profile["model"], "--keepalive", "1h", "")
