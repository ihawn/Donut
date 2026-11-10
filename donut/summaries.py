"""Readable summaries of provider status replies (/mcp, /context) instead of raw JSON."""

import re

from rich.text import Text

from .highlight import CORAL, DIM, GRAY, ORANGE, TEAL

STATUS_LABELS = {
    # Claude Code (McpServerConnectionStatus)
    "connected": ("connected", TEAL),
    "failed": ("failed", CORAL),
    "needs-auth": ("needs login", ORANGE),
    "pending": ("starting", ORANGE),
    "disabled": ("disabled", DIM),
    # Codex (McpServerConnectionStatus)
    "notStarted": ("not started", DIM),
    "starting": ("starting", ORANGE),
    "authenticationRequired": ("needs login", ORANGE),
    "cancelled": ("cancelled", DIM),
}


def short_error(message, width=90):
    """Keep the human part of a provider error: drop Rust type paths and keep the last "error:" clause."""
    if not message:
        return ""
    text = re.sub(r"\[[^\[\]]*::[^\[\]]*\]", "", str(message))  # [codex_rmcp_client::...<...>] type names
    text = re.sub(r"<[^<>]*::[^<>]*>", "", text)
    text = " ".join(text.split())
    clauses = re.split(r"\berror:\s*", text, flags=re.I)
    if len(clauses) > 1 and clauses[-1].strip():
        text = clauses[-1].strip()
    text = text.strip(" :;,")
    return text if len(text) <= width else text[: width - 1] + "…"


def mcp_servers(data):
    """Normalize Claude's get_mcp_status and Codex's mcpServerStatus/list into one shape."""
    rows = []
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), list):  # Claude
        for server in data["mcpServers"]:
            if not isinstance(server, dict):
                continue
            info = server.get("serverInfo") or {}
            tools = server.get("tools")
            rows.append(
                {
                    "name": server.get("name", "?"),
                    "status": server.get("status") or "unknown",
                    "tools": len(tools) if isinstance(tools, list) else None,
                    "source": server.get("scope") or "",
                    "version": info.get("version", "") if isinstance(info, dict) else "",
                    "reason": short_error(server.get("error")),
                }
            )
    elif isinstance(data, dict) and isinstance(data.get("data"), list):  # Codex
        for server in data["data"]:
            if not isinstance(server, dict):
                continue
            tools = server.get("tools")
            error = server.get("toolsError")
            status = server.get("runtimeStatus")
            if not status:
                if server.get("authStatus") == "notLoggedIn":
                    status = "needs-auth"
                elif error:
                    status = "failed"
                elif tools:
                    status = "connected"
                else:
                    status = "unknown"
            if status == "failed" and server.get("authStatus") == "notLoggedIn":
                status = "needs-auth"
            plugin = server.get("pluginId") or ""
            rows.append(
                {
                    "name": server.get("name", "?"),
                    "status": status,
                    "tools": len(tools) if isinstance(tools, (dict, list)) else None,
                    "source": plugin.split("@")[0] if plugin else "",
                    "version": "",
                    "reason": short_error(error),
                }
            )
    return rows


def parse_mcp_list(text):
    """[(name, status)] from `claude mcp list`, whose lines read "name: address - ✓ Connected".

    Names can hold colons (plugin:postman:postman) but never ": ", so the first ": " ends the name.
    """
    servers = []
    for line in (text or "").splitlines():
        if ": " not in line or " - " not in line:
            continue
        name, rest = line.split(": ", 1)
        status = rest.rsplit(" - ", 1)[1].strip()
        servers.append((name.strip(), status))
    return servers


def is_connected(status):
    return "connected" in status.lower() and "fail" not in status.lower() and "not" not in status.lower()


def render_mcp(servers, provider):
    """Lines for the transcript: a one-line tally, then one line per server."""
    if not servers:
        return [Text(f"  No MCP servers configured for {provider}.", style=DIM)]
    tally = {}
    for server in servers:
        label, _ = STATUS_LABELS.get(server["status"], (server["status"], GRAY))
        tally[label] = tally.get(label, 0) + 1
    header = Text(f"  MCP servers · {provider}  ", style=f"bold {ORANGE}")
    header.append(" · ".join(f"{count} {label}" for label, count in tally.items()), style=GRAY)
    lines = [header]
    width = max(len(server["name"]) for server in servers)
    for server in sorted(servers, key=lambda s: (s["status"] != "connected", s["name"].lower())):
        label, color = STATUS_LABELS.get(server["status"], (server["status"], GRAY))
        line = Text("    ● ", style=color)
        line.append(server["name"].ljust(width), style=f"bold {GRAY}")
        line.append(f"  {label:<11}", style=color)
        if server["tools"]:
            line.append(f"  {server['tools']} tool{'s' if server['tools'] != 1 else ''}", style=DIM)
        if server["source"]:
            line.append(f"  {server['source']}", style=DIM)
        if server["reason"]:
            line.append(f"  {server['reason']}", style=CORAL if server["status"] == "failed" else DIM)
        lines.append(line)
    if any(server["status"] in ("needs-auth", "authenticationRequired", "failed") for server in servers):
        lines.append(Text("  Use /native to log in to or fix these servers in the provider's own client.", style=DIM))
    return lines


def _tokens(count):
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


# Categories Claude reports that are not things taking up the context window.
NOT_USAGE = ("free space", "autocompact buffer")


def _bar(percent, width=24):
    filled = max(0, min(width, round(percent / 100 * width)))
    color = TEAL if percent < 60 else ORANGE if percent < 85 else CORAL
    bar = Text("━" * filled, style=color)
    bar.append("─" * (width - filled), style="#3b4652")
    return bar, color


def render_context(used, window, categories=(), model="", note=""):
    """A context-window bar, then the largest categories when the provider reports them."""
    lines = []
    header = Text("  Context", style=f"bold {ORANGE}")
    if model:
        header.append(f" · {model}", style=GRAY)
    lines.append(header)
    if window:
        percent = used / window * 100
        bar, color = _bar(percent)
        line = Text("    ")
        line.append_text(bar)
        line.append(f"  {_tokens(used)} / {_tokens(window)} tokens", style=GRAY)
        line.append(f"  {percent:.0f}% used", style=color)
        lines.append(line)
    elif used:
        lines.append(Text(f"    {_tokens(used)} tokens in context (window size not reported)", style=GRAY))
    shown = sorted((c for c in categories if c.get("tokens")), key=lambda c: -c["tokens"])[:6]
    if shown:
        width = max(len(c.get("name", "")) for c in shown)
        for category in shown:
            line = Text("    " + category.get("name", "?").ljust(width), style=GRAY)
            line.append(f"  {_tokens(category['tokens']):>7}", style=DIM)
            if window:
                line.append(f"  {category['tokens'] / window * 100:4.1f}%", style=DIM)
            lines.append(line)
    if note:
        lines.append(Text("  " + note, style=DIM))
    return lines


def claude_context(data):
    if not isinstance(data, dict):
        return [Text("  Claude did not report context usage.", style=DIM)]
    categories = [
        c
        for c in data.get("categories") or []
        if isinstance(c, dict) and not c.get("isDeferred") and str(c.get("name", "")).lower() not in NOT_USAGE
    ]
    return render_context(
        data.get("totalTokens") or 0,
        data.get("maxTokens") or data.get("rawMaxTokens") or 0,
        categories,
        data.get("model", ""),
        "Auto-compact is on." if data.get("isAutoCompactEnabled") else "",
    )


def codex_context(usage, model=""):
    """Codex reports token usage per turn; the last turn's total is what fills the window."""
    if not isinstance(usage, dict) or not usage:
        return [Text("  No Codex conversation yet: send a message first.", style=DIM)]
    last = usage.get("last") or {}
    total = usage.get("total") or {}
    lines = render_context(last.get("totalTokens") or 0, usage.get("modelContextWindow") or 0, model=model)
    if total.get("totalTokens"):
        lines.append(
            Text(
                f"    this conversation: {_tokens(total['totalTokens'])} tokens"
                f" ({_tokens(total.get('inputTokens', 0))} in, {_tokens(total.get('outputTokens', 0))} out)",
                style=DIM,
            )
        )
    return lines
