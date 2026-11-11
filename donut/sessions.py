"""Persistent provider sessions for the interactive terminal."""

import asyncio
import json
import shutil
from pathlib import Path
from .ui import release_version
from .providers import native_permission


# The approval answers the terminal returns: allow once, allow for the rest of the session, or deny.
def epoch_seconds(value):
    """Reset times arrive as Unix seconds (Claude events, Codex) or ISO 8601 strings (Claude get_usage)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        from datetime import datetime

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def window_label(minutes):
    """300 -> "5h", 10080 -> "7d"."""
    if not minutes:
        return ""
    return f"{round(minutes / 1440)}d" if minutes >= 1440 else f"{round(minutes / 60)}h"


def codex_usage(snapshot):
    """Usage windows from a Codex RateLimitSnapshot: [{"window", "percent", "resets_at"}]."""
    windows = []
    for key in ("primary", "secondary"):
        window = (snapshot or {}).get(key)
        if isinstance(window, dict) and isinstance(window.get("usedPercent"), (int, float)):
            windows.append(
                {
                    "window": window_label(window.get("windowDurationMins")),
                    "percent": float(window["usedPercent"]),
                    "resets_at": epoch_seconds(window.get("resetsAt")),
                }
            )
    return windows


CLAUDE_WINDOWS = {"five_hour": "5h", "seven_day": "7d", "seven_day_opus": "7d opus", "seven_day_sonnet": "7d sonnet"}


# Dollars per million tokens (input, output, cache read) by family, to estimate a turn's cost while it runs.
# Claude's own figure replaces the estimate when the turn ends. A cache write costs 1.25 times the input price.
CLAUDE_PRICES = {"fable": (8, 40, 0.2), "opus": (4, 20, 0.2), "sonnet": (2, 10, 0.1), "haiku": (0.1, 0.5, 0.01)}


def claude_cost(model, usage):
    """Estimated dollars for one Claude response from its token counts; 0 for a model with no known price."""
    prices = next((value for family, value in CLAUDE_PRICES.items() if family in (model or "").lower()), None)
    if not prices or not isinstance(usage, dict):
        return 0.0
    count = lambda key: int(usage.get(key) or 0)  # noqa: E731
    return (
        count("input_tokens") * prices[0]
        + count("cache_creation_input_tokens") * prices[0] * 1.25
        + count("output_tokens") * prices[1]
        + count("cache_read_input_tokens") * prices[2]
    ) / 1_000_000


def claude_usage(info):
    """Usage windows from a Claude RateLimitInfo. unifiedWindows carries every window when present."""
    raw = getattr(info, "raw", None) or {}
    windows = []
    for key, window in (raw.get("unifiedWindows") or {}).items():
        if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)):
            windows.append(
                {
                    "window": CLAUDE_WINDOWS.get(key, key),
                    "percent": window["utilization"] * 100,
                    "resets_at": epoch_seconds(window.get("resetsAt")),
                }
            )
    if not windows and getattr(info, "utilization", None) is not None:
        windows.append(
            {
                "window": CLAUDE_WINDOWS.get(info.rate_limit_type, info.rate_limit_type or ""),
                "percent": info.utilization * 100,
                "resets_at": epoch_seconds(info.resets_at),
            }
        )
    return windows


def claude_plan_usage(result):
    """Windows from the CLI's get_usage control request, the data behind Claude Code's /usage.

    Returns [] when plan limits do not apply (API key, Bedrock, Vertex) and None when the answer is unusable.
    Utilization there is a percentage, 0-100.
    """
    if not isinstance(result, dict):
        return None
    if result.get("rate_limits_available") is False:
        return []
    limits = result.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    windows = []
    for key in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
        window = limits.get(key)
        if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)):
            windows.append(
                {
                    "window": CLAUDE_WINDOWS[key],
                    "percent": float(window["utilization"]),
                    "resets_at": epoch_seconds(window.get("resets_at")),
                }
            )
    return windows


# Codex item types that are tool calls, and the name Donut shows for each.
CODEX_TOOLS = {
    "commandExecution": "Bash",
    "fileChange": "Edit",
    "mcpToolCall": "MCP",
    "webSearch": "WebSearch",
    "dynamicToolCall": "Tool",
}

CODEX_DECISIONS = {"yes": "accept", "always": "acceptForSession", "no": "decline"}
CODEX_SANDBOX = {"read-only": "readOnly", "workspace-write": "workspaceWrite", "danger-full-access": "dangerFullAccess"}


class CodexSession:
    def __init__(self, project, profile, emit, approve):
        self.project, self.profile = project, profile
        self.emit, self.approve = emit, approve
        self.pending = {}
        self.serial = 0
        self.thread = None
        self.turn_done = None
        self.permission = "ask"
        self.permission_changed = False

    async def set_effort(self, level):
        """Use this reasoning effort from the next turn on; Codex keeps it for later turns."""
        self.profile = dict(self.profile, effort=level)
        self.effort_changed = True

    async def model_details(self):
        """Codex's own model list, with each model's reasoning efforts and their descriptions."""
        models, cursor = [], None
        while True:
            result = await self.request("model/list", {"cursor": cursor})
            models.extend(result.get("data", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return models

    async def switch_model(self, model):
        """Use another model from the next turn on, in the same thread, so Codex keeps its whole context."""
        self.profile = dict(self.profile, model=model)
        self.model_changed = bool(self.thread)

    async def set_permission(self, mode):
        """Codex applies policy overrides on the next turn/start, and they persist for later turns."""
        self.permission = mode
        self.permission_changed = bool(self.thread) and not getattr(self, "read_only", False)

    def policy(self):
        if getattr(self, "read_only", False):
            return "on-request", "read-only", "user"  # Review mode: it may read and run checks, never write.
        approval, sandbox, reviewer = native_permission("codex", self.permission)
        return approval, sandbox, reviewer

    async def connect(self):
        self.process = await asyncio.create_subprocess_exec(
            shutil.which("codex") or "codex",
            "app-server",
            "--stdio",
            cwd=self.project,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=32 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self.read())
        await self.request(
            "initialize",
            {"clientInfo": {"name": "donut", "version": release_version()}, "capabilities": {"experimentalApi": True}},
        )
        await self.send({"method": "initialized"})
        self.usage_task = asyncio.create_task(self.refresh_usage())

    async def refresh_usage(self):
        """Report account rate limits for the usage bar; failures only mean the bar stays empty."""
        windows = await self.quota()
        if windows:
            self.emit("usage", windows)

    async def quota(self):
        """Current account windows, or None when Codex could not say."""
        try:
            result = await self.request("account/rateLimits/read")
        except Exception:
            return None
        return codex_usage(result.get("rateLimits")) or None

    async def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params=None):
        if self.reader.done():
            raise RuntimeError(getattr(self, "read_error", "Codex session is closed"))
        self.serial += 1
        identity = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.send({"id": identity, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, 90)
        finally:
            self.pending.pop(identity, None)

    async def read(self):
        try:
            async for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if "id" in event and "method" not in event:
                    future = self.pending.get(event["id"])
                    if future and not future.done():
                        if "error" in event:
                            future.set_exception(RuntimeError(event["error"].get("message", "Provider error")))
                        else:
                            future.set_result(event.get("result", {}))
                elif "id" in event:
                    asyncio.create_task(self.handle_request(event))
                else:
                    method, params = event.get("method", ""), event.get("params", {})
                    if method == "item/agentMessage/delta":
                        self.emit("text", params.get("delta", ""))
                        self.wrote = True
                    elif method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
                        self.emit("thinking", params.get("delta", ""))
                    elif method == "item/started":
                        item = params.get("item", {})
                        kind = item.get("type", "Working")
                        if kind == "agentMessage" and getattr(self, "wrote", False):
                            self.emit("break", "")  # A new message in the same turn.
                        self.emit("activity", kind)
                        if kind in CODEX_TOOLS:
                            self.emit("tool", {"id": item.get("id"), "name": CODEX_TOOLS[kind], "input": item})
                    elif method == "item/completed" and params.get("item", {}).get("type") in CODEX_TOOLS:
                        item = params["item"]
                        output = item.get("aggregatedOutput") or item.get("result") or item.get("error") or ""
                        failed = item.get("status") == "failed" or item.get("exitCode") not in (None, 0)
                        self.emit("tool_result", {"id": item.get("id"), "output": str(output), "error": failed})
                    if method == "item/completed" and params.get("item", {}).get("type") == "commandExecution":
                        item = params["item"]
                        command = item.get("command")
                        command = " ".join(command) if isinstance(command, list) else str(command or "")
                        self.emit("command", {"id": item.get("id"), "command": command})
                        self.emit(
                            "command_output", {"id": item.get("id"), "output": item.get("aggregatedOutput") or ""}
                        )
                    elif method == "thread/tokenUsage/updated":
                        self.usage = params.get("tokenUsage", {})
                    elif method == "account/rateLimits/updated":
                        windows = codex_usage(params.get("rateLimits"))
                        if windows:
                            self.emit("usage", windows)
                    elif method == "turn/completed" and self.turn_done and not self.turn_done.done():
                        turn = params.get("turn", {})
                        if turn.get("status") in ("failed", "interrupted"):
                            self.turn_done.set_exception(RuntimeError(str(turn.get("error") or turn["status"])))
                        else:
                            self.turn_done.set_result(None)
        except Exception as error:
            self.read_error = "Codex stream failed: " + str(error)
        finally:
            error = RuntimeError(getattr(self, "read_error", "Codex session disconnected"))
            for future in list(self.pending.values()) + [self.turn_done]:
                if future and not future.done():
                    future.set_exception(error)

    async def handle_request(self, event):
        method = event["method"]
        if method.endswith("/requestApproval") and ("commandExecution" in method or "fileChange" in method):
            decision = await self.approve(method, event.get("params", {}))
            await self.send({"id": event["id"], "result": {"decision": CODEX_DECISIONS.get(decision, "decline")}})
        else:
            self.emit("error", "Unsupported provider interaction: " + method + "; use /native.")
            await self.send(
                {
                    "id": event["id"],
                    "error": {
                        "code": -32601,
                        "message": "This interaction is not supported by Donut yet; use the native client.",
                    },
                }
            )

    async def query(self, text):
        approval, sandbox, reviewer = self.policy()
        if not self.thread:
            settings = {
                "cwd": str(self.project),
                "model": self.profile.get("model") or None,
                "approvalPolicy": approval,
                "sandbox": sandbox,
                "approvalsReviewer": reviewer,
            }
            resume = getattr(self, "resume_id", None)
            if resume:
                # /resume: continue the saved thread with today's model and policies.
                result = await self.request("thread/resume", dict(settings, threadId=resume))
            else:
                result = await self.request("thread/start", settings)
            self.thread = result["thread"]["id"]
            self.permission_changed = False
        params = {"threadId": self.thread, "input": [{"type": "text", "text": text}]}
        if getattr(self, "model_changed", False):
            params["model"] = self.profile.get("model")  # turn/start overrides persist for later turns.
            self.model_changed = False
        if self.profile.get("effort") and (
            getattr(self, "effort_changed", False) or not getattr(self, "effort_sent", False)
        ):
            params["effort"] = self.profile["effort"]
            self.effort_changed, self.effort_sent = False, True
        if self.permission_changed:
            params.update(
                approvalPolicy=approval, sandboxPolicy={"type": CODEX_SANDBOX[sandbox]}, approvalsReviewer=reviewer
            )
            self.permission_changed = False
        self.turn_done = asyncio.get_running_loop().create_future()
        self.wrote = False
        await self.request("turn/start", params)
        await self.turn_done

    @property
    def provider_id(self):
        """The Codex thread id, which thread/resume reopens."""
        return self.thread

    async def interrupt(self):
        if self.thread:
            # The thread view supplies the currently running turn ID.
            result = await self.request("thread/read", {"threadId": self.thread, "includeTurns": True})
            turns = result.get("thread", {}).get("turns", [])
            if turns:
                await self.request("turn/interrupt", {"threadId": self.thread, "turnId": turns[-1]["id"]})

    async def models(self):
        models, cursor = [], None
        while True:
            result = await self.request("model/list", {"cursor": cursor})
            models.extend({"id": m["model"], "name": m.get("displayName", m["model"])} for m in result.get("data", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return models

    async def account_usage(self):
        return await self.request("account/rateLimits/read")

    async def close(self):
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        await self.reader


class ClaudeSession:
    def __init__(self, project, profile, emit, approve):
        self.project, self.profile = project, profile
        self.emit, self.approve = emit, approve
        self.usage = {}
        self.permission = "ask"

    async def set_permission(self, mode):
        """Switch Claude's native permission mode in the live session (applied when it is idle)."""
        self.permission = mode
        if getattr(self, "read_only", False):
            return  # Review mode stays in Claude's read-only plan mode whatever the permission mode.
        if getattr(self, "owner", None) and not self.owner.done():
            await self._call("set_permission", mode)

    async def quota(self):
        """Plan usage without sending a prompt, through the CLI's get_usage control request."""
        if not getattr(self, "owner", None) or self.owner.done():
            return None
        try:
            return await asyncio.wait_for(self._call("quota"), 30)
        except Exception:
            return None

    async def _quota(self):
        query = getattr(self.client, "_query", None)
        if query is None:
            return None
        return claude_plan_usage(await query._send_control_request({"subtype": "get_usage"}))

    async def _set_permission(self, mode):
        await self.client.set_permission_mode(native_permission("claude", mode))

    async def switch_model(self, model):
        """Change the model of this live conversation; Claude Code keeps its whole context."""
        await self._call("set_model", model)
        self.profile = dict(self.profile, model=model)

    async def _set_model(self, model):
        await self.client.set_model(model or None)

    async def set_effort(self, level):
        """Change effort in the live session, as Claude Code's own /effort does."""
        await self._call("set_effort", level)
        self.profile = dict(self.profile, effort=level)

    async def _set_effort(self, level):
        await self.client._query._send_control_request(
            {"subtype": "apply_flag_settings", "settings": {"effortLevel": level}}
        )

    async def _connect(self):
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny

        async def permission(name, inputs, context):
            if await self.approve(name, inputs) in ("yes", "always"):
                return PermissionResultAllow(updated_input=inputs)
            return PermissionResultDeny(message="User declined")

        env = {}
        if self.profile.get("claude_config_dir"):
            env["CLAUDE_CONFIG_DIR"] = str(Path(self.profile["claude_config_dir"]).expanduser())
        options = ClaudeAgentOptions(
            cli_path=shutil.which("claude"),
            cwd=self.project,
            env=env,
            model=self.profile.get("model") or None,
            setting_sources=["user", "project", "local"],
            permission_mode="plan"
            if getattr(self, "read_only", False)
            else native_permission("claude", self.permission),
            can_use_tool=permission,
            include_partial_messages=True,
            resume=getattr(self, "resume_id", None) or None,
            effort=self.profile.get("effort") or None,
            # Lets /permissions or Shift+Tab switch this live session to Full access later. It bypasses
            # nothing by itself; review sessions stay read-only and never get it.
            extra_args={}
            if getattr(self, "read_only", False)
            # chrome: Claude Code's own switch for the Claude in Chrome extension, turned on with /chrome.
            else {
                "allow-dangerously-skip-permissions": None,
                **({"chrome": None} if getattr(self, "chrome", False) else {}),
            },
        )
        self.client = ClaudeSDKClient(options=options)
        await self.client.connect()
        self.info = await self.client.get_server_info() or {}
        self.inbox, self.in_turn = asyncio.Queue(), False
        self.pump = asyncio.create_task(self._pump())

    def _track_task(self, message):
        """Report background shells and agents Claude Code starts and ends. Returns whether message was one."""
        from claude_agent_sdk import TaskNotificationMessage, TaskStartedMessage, TaskUpdatedMessage

        if isinstance(message, TaskStartedMessage):
            kind = "shell" if "bash" in (message.task_type or "").lower() else "agent"
            self.emit(
                "task", {"id": message.task_id, "state": "running", "kind": kind, "description": message.description}
            )
            return True
        if isinstance(message, TaskUpdatedMessage):
            status = message.status or (message.patch or {}).get("status")
            if status in ("completed", "failed", "stopped", "killed"):
                self.emit("task", {"id": message.task_id, "state": status})
            return True
        if isinstance(message, TaskNotificationMessage):
            self.emit("task", {"id": message.task_id, "state": message.status, "summary": message.summary})
            return False  # The idle reader still announces it.
        return False

    def _handle(self, message, state):
        """Show one message from Claude. Returns True when it ends a turn (its ResultMessage)."""
        if self._track_task(message):
            return False
        from claude_agent_sdk import (
            AssistantMessage,
            RateLimitEvent,
            ResultMessage,
            TextBlock,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        if isinstance(message, RateLimitEvent):
            windows = claude_usage(message.rate_limit_info)
            if windows:
                self.emit("usage", windows)
        elif isinstance(message, AssistantMessage):
            if message.usage:
                dollars = claude_cost(message.model, message.usage)
                self.emit("spend", {"id": message.message_id or message.uuid, "dollars": dollars})
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    if state.get("wrote"):
                        self.emit("break", "")  # Separate text blocks are separate messages.
                    self.emit("text", block.text)
                    state["wrote"] = True
                elif isinstance(block, ThinkingBlock) and block.thinking.strip():
                    self.emit("thinking", block.thinking)
                elif isinstance(block, ToolUseBlock):
                    self.emit("activity", block.name)
                    self.emit("tool", {"id": block.id, "name": block.name, "input": block.input})
                    if block.name == "Bash":
                        self.emit("command", {"id": block.id, "command": str(block.input.get("command", ""))})
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    content = block.content
                    if isinstance(content, list):
                        content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
                    self.emit("command_output", {"id": block.tool_use_id, "output": str(content or "")})
                    self.emit(
                        "tool_result",
                        {"id": block.tool_use_id, "output": str(content or ""), "error": bool(block.is_error)},
                    )
        elif isinstance(message, ResultMessage):
            self.session_id = getattr(message, "session_id", None) or getattr(self, "session_id", None)
            self.usage = {"tokens": message.usage, "reported_cost_usd": message.total_cost_usd}
            if message.is_error and not state.get("woken"):
                raise RuntimeError(message.result or "Claude turn failed")
            return True
        return False

    async def _pump(self):
        """Read every message Claude sends, for the whole session.

        During a turn Donut asked for, messages go to that turn. Between turns, Claude Code may start a turn of
        its own, for example when a background shell it left running finishes; those are shown as a wake-up.
        """
        from claude_agent_sdk import SystemMessage

        state = {}
        try:
            async for message in self.client.receive_messages():
                if self.in_turn:
                    await self.inbox.put(message)
                    continue
                if self._track_task(message):
                    continue
                if isinstance(message, SystemMessage):
                    if getattr(message, "subtype", "") == "task_notification" and not state:
                        summary = getattr(message, "summary", "") or (message.data or {}).get("summary", "")
                        self.emit("background", summary or "A background task finished.")
                    continue
                if not state:
                    state = {"woken": True}
                    self.emit("wake_start", "")
                try:
                    finished = self._handle(message, state)
                except Exception as error:
                    self.emit("error", str(error))
                    finished = True
                if finished:
                    state = {}
                    self.emit("wake_end", "")
        except Exception as error:
            await self.inbox.put(error)

    async def _query(self, text):
        self.in_turn = True
        try:
            await self.client.query(text)
            state = {}
            while True:
                message = await self.inbox.get()
                if isinstance(message, Exception):
                    raise message
                if self._handle(message, state):
                    return
        finally:
            self.in_turn = False

    async def _models(self):
        return [{"id": m["value"], "name": m.get("displayName", m["value"])} for m in self.info.get("models", [])]

    async def _account_usage(self):
        return {
            "session": self.usage,
            "account_limits": "Not exposed by the supported SDK; available in native Claude /usage.",
        }

    async def interrupt(self):
        await self.client.interrupt()

    async def _close(self):
        pump = getattr(self, "pump", None)
        if pump is not None:
            pump.cancel()
        await self.client.disconnect()

    @property
    def provider_id(self):
        """The Claude Code session id, which --resume reopens."""
        return getattr(self, "session_id", None)

    async def connect(self):
        self.requests = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.owner = asyncio.create_task(self._serve())
        await self.ready

    async def _serve(self):
        try:
            await self._connect()
        except Exception as error:
            self.ready.set_exception(error)
            return
        self.ready.set_result(None)
        try:
            while True:
                name, args, future = await self.requests.get()
                if name == "close":
                    future.set_result(None)
                    break
                try:
                    result = await getattr(self, "_" + name)(*args)
                    if not future.done():
                        future.set_result(result)
                except Exception as error:
                    if not future.done():
                        future.set_exception(error)
        finally:
            await self._close()

    async def _call(self, name, *args):
        future = asyncio.get_running_loop().create_future()
        await self.requests.put((name, args, future))
        return await future

    async def query(self, text):
        return await self._call("query", text)

    async def models(self):
        return await self._call("models")

    async def account_usage(self):
        return await self._call("account_usage")

    async def close(self):
        if not self.owner.done():
            await self._call("close")
        await self.owner


class QwenSession:
    """Keep Qwen's native saved session across turns, including local models."""

    def __init__(self, project, profile, emit, approve):
        self.project, self.profile, self.emit = project, profile, emit
        self.session_id = None
        self.usage = {}
        self.process = None
        self.permission = "ask"

    async def connect(self):
        self.session_id = getattr(self, "resume_id", None) or self.session_id

    @property
    def provider_id(self):
        """The Qwen Code session id, which --resume reopens."""
        return self.session_id

    async def quota(self):
        return None  # Local and OpenAI-compatible models have no plan limits to report.

    async def set_permission(self, mode):
        # Every Qwen turn is a new process, so the next turn picks this up.
        self.permission = mode

    def effective_permission(self):
        """Qwen cannot show approval prompts inside Donut, so Ask behaves as Accept edits for it."""
        return "accept_edits" if self.permission == "ask" else self.permission

    async def query(self, text):
        from .providers import command, activate

        await asyncio.to_thread(activate, self.profile)
        args, env = command(
            self.profile, text, permission=self.effective_permission(), review=getattr(self, "read_only", False)
        )
        if self.session_id:
            args += ["--resume", self.session_id]
        self.process = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.project,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=32 * 1024 * 1024,
        )

        async def input_prompt():
            self.process.stdin.write(text.encode())
            await self.process.stdin.drain()
            self.process.stdin.close()

        async def errors():
            return await self.process.stderr.read()

        error_reader = asyncio.create_task(errors())
        await input_prompt()
        wrote = False
        async for line in self.process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "system":
                self.session_id = event.get("session_id", self.session_id)
            if event.get("type") == "stream_event":
                item = event.get("event", {})
                block, delta = item.get("content_block", {}), item.get("delta", {})
                if block.get("type") == "text" and wrote:
                    self.emit("break", "")
                if delta.get("type") == "text_delta":
                    self.emit("text", delta.get("text", ""))
                    wrote = True
                elif delta.get("type") == "thinking_delta":
                    self.emit("thinking", delta.get("thinking", ""))
                if block.get("type") == "tool_use":
                    self.emit("activity", block.get("name", "Working"))
                    self.emit("tool", {"id": block.get("id"), "name": block.get("name", "Tool"), "input": {}})
            if event.get("type") == "result":
                self.usage = event.get("usage", {})
                if event.get("is_error"):
                    raise RuntimeError("Qwen reported a failed turn")
        code = await self.process.wait()
        stderr = await error_reader
        if code:
            raise RuntimeError("Qwen failed: " + stderr.decode(errors="replace")[-500:])

    async def interrupt(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()

    async def close(self):
        await self.interrupt()
        if self.process:
            await self.process.wait()

    async def account_usage(self):
        return {"session": self.usage}
