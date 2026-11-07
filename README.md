# ◉ Donut

Small tickets. Fresh contexts. A local-first command-line wrapper around coding agents.

Donut uses a planning model to write scoped Markdown tickets, then runs each ticket in a new agent process. Workers can be local Ollama models, Claude Code, Codex, or models served through an OpenAI-compatible endpoint (including Grok). There is no bundled model download.

## Install

Requires Python 3.10+ on macOS/Linux. Install the provider CLI(s) you intend to use: Qwen Code for Ollama and compatible endpoints, Claude Code for Claude, or Codex for OpenAI. Local inference also requires a running Ollama server. Donut does not silently install these tools.

```sh
pipx install git+https://github.com/ihawn/Donut.git
cd your-project
donut setup
```

For development: `python3 -m venv .venv`, `.venv/bin/pip install -e .`, then `.venv/bin/donut`.

## Setup

`donut setup` walks through:

1. Choosing a planning provider. Claude/Codex can open their own login flow or reuse an existing login. Grok/compatible endpoints use a named environment variable containing an API key; Donut never asks you to paste that key into its configuration.
2. Naming aliases. Each alias selects its engine, model, and optional compatible endpoint. A hosted model can be the worker too.
3. Discovering registered Ollama models and model files under `~/Models`, `~/Downloads`, `~/.cache/huggingface/hub`, `~/.lmstudio/models`, and `~/.ollama/models`. `--scan PATH` instead scans a directory you choose. It inspects filenames, not personal document contents.
4. Optionally importing a discovered GGUF into Ollama. Safetensors directories are reported; serve these with a compatible runtime before configuring an endpoint. A raw weight file is not necessarily a runnable coding model.
5. Installing project instructions and selecting the default worker.

Setup asks whether each cloud alias may execute tools without approval. Local aliases retain the original Qwen worker's unattended mode. Only run unattended agents in projects you trust. Without automatic approval, a provider may refuse commands it cannot approve non-interactively; Donut reports the failure instead of claiming completion.

```sh
donut models
donut discover --scan /path/to/models
donut setup -p ../another-project
```

Configuration is user-local: `$XDG_CONFIG_HOME/donut/config.json` (default `~/.config/donut/config.json`, mode 0600). It contains aliases and **environment variable names**, never API key values. Provider CLIs own their own OAuth credentials. Subscription login and API billing are different; Grok uses API authentication here, not a browser-chat subscription.

## Plan and build

```sh
donut plan "Add wall jumping, with a human feel check"
donut -m qwen-faster do next 3
donut -m qwen do next all
donut -m cloud-worker do next 1 --project ../another-project
```

`-m` / `--model` accepts a configured alias. `-p` / `--project` accepts an absolute or relative directory and defaults to the current directory. Flags work before or after the subcommand. `donut -h` lists commands.

`donut init` and `donut add-skills` install skills without login/setup. Existing instructions and skills are kept. They create:

- `swim_lanes/{to_do,in_progress,done}/`
- `.qwen/skills/swim-lane-worker/SKILL.md`, connected through `QWEN.md`
- `skills/donut-planner/SKILL.md`, explicitly loaded by `donut plan` (including Codex), plus Claude/Cursor skill copies
- Relevant locally available Unity skills when a Unity project is detected

No game-specific rules are copied from another project. If this project has `skills/project-rules/SKILL.md`, its worker import is connected. Planner instructions are deliberately not placed in `AGENTS.md` or `.agents/skills`, since Qwen also reads those locations.

## Execution and output

Each ticket gets a fresh process with no resume/continue flag. Existing in-progress work is considered first, then the lowest numbered eligible ticket. Dependencies use `**Slug:**` and `**Depends on:**` metadata. A blocked ticket counts toward the requested number; its dependent tasks are skipped, while independent tasks can continue. `all` rechecks the queue, including newly added tickets. Process failure or an invalid lane transition stops the batch.

Donut uses an orange/navy/coral/teal/gray palette, a loading spinner while the agent is silent, and live emitted text, reasoning, tools, and results. Redirected output has no color and prints periodic status lines. Set `NO_COLOR=1` to disable colors. Some providers expose reasoning summaries or only complete events; Donut cannot display reasoning they do not expose.

The final summary is produced by another fresh agent context using completion notes. Donut checks lane transitions, not the truth of model-authored tests. Human checks remain human checks. Codex's summary context is read-only; Claude disables tools and Qwen sets its tool-call budget to zero.

Run logs/results live outside the repo under `$XDG_STATE_HOME/donut` (default `~/.local/state/donut`). They can contain project content and tool output, so treat them as private. Ctrl-C terminates the current process group and prevents the next ticket from starting. Re-running resumes unfinished work in a fresh context. Per-project locks stop overlapping Donut batches, but cannot stop manually launched agents or the older Qwen wrapper.

For Ollama, Donut unloads other loaded models before activating the chosen model and refreshes its one-hour keepalive after the batch. Configure `OLLAMA_KEEP_ALIVE=1h` on the **Ollama server** for the same timeout on requests made by provider clients and on interrupted runs. Donut does not rewrite OS service configuration. Hosted model residency is controlled by the provider.

## Development / verification

```sh
python3 -m unittest discover -s tests -v
python3 -m donut --help
```

Tests use fake agent executables and temporary projects; no credentials or paid model calls are needed. They cover dependency ordering, blocked tasks, process errors, locking, fresh invocations, output rendering, setup, model discovery, instruction preservation, credential boundaries and provider command construction.

This first release supports macOS/Linux. Windows process groups and file locking are not implemented. No remote accounts, API keys, user configuration, or run logs belong in this repository.
