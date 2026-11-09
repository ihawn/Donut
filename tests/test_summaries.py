import unittest

from donut.summaries import claude_context, codex_context, mcp_servers, render_mcp, short_error

RUST_ERROR = (
    "MCP startup failed: handshaking with MCP server failed: Send message error Transport "
    "[codex_rmcp_client::tool_list_changes::ToolListChanges<codex_rmcp_client::event_notification_transport"
    "<rmcp::transport::worker::WorkerTransport<rmcp::transport::streamable_http_client::StreamableHttpClientWorker"
    "<codex_rmcp_client::client_adapter::StreamableHttpClientAdapter>>>>] error: Auth required, when send initialize request"
)


def plain(lines):
    return "\n".join(line.plain for line in lines)


class McpSummaryTests(unittest.TestCase):
    def test_rust_errors_are_shortened_to_the_human_part(self):
        self.assertEqual(short_error(RUST_ERROR), "Auth required, when send initialize request")
        self.assertEqual(short_error(None), "")
        self.assertTrue(len(short_error("x" * 500)) <= 90)

    def test_codex_reply_becomes_one_line_per_server(self):
        reply = {
            "data": [
                {
                    "name": "github",
                    "runtimeStatus": None,
                    "pluginId": None,
                    "tools": {"a": {}, "b": {}},
                    "toolsError": None,
                    "resources": [],
                    "resourceTemplates": [],
                    "authStatus": "unsupported",
                },
                {
                    "name": "postman-mcp",
                    "runtimeStatus": None,
                    "pluginId": "postman@claude-plugins-official",
                    "httpOrigin": "https://mcp.postman.com",
                    "tools": {},
                    "toolsError": RUST_ERROR,
                    "resources": [],
                    "resourceTemplates": [],
                    "authStatus": "notLoggedIn",
                },
            ],
            "nextCursor": None,
        }
        text = plain(render_mcp(mcp_servers(reply), "codex"))
        self.assertIn("MCP servers · codex", text)
        self.assertIn("1 connected · 1 needs login", text)
        self.assertRegex(text, r"github\s+connected\s+2 tools")
        self.assertRegex(text, r"postman-mcp\s+needs login\s+postman\s+Auth required")
        self.assertNotIn("codex_rmcp_client", text)
        self.assertNotIn("{", text)
        self.assertIn("/native", text)
        self.assertLessEqual(len(text.splitlines()), 4)

    def test_claude_reply(self):
        reply = {
            "mcpServers": [
                {"name": "linear", "status": "connected", "scope": "user", "tools": [{"name": "a"}]},
                {"name": "broken", "status": "failed", "error": "spawn npx ENOENT"},
            ]
        }
        text = plain(render_mcp(mcp_servers(reply), "claude"))
        self.assertRegex(text, r"linear\s+connected\s+1 tool\s+user")
        self.assertRegex(text, r"broken\s+failed\s+spawn npx ENOENT")
        self.assertIn("No MCP servers configured", plain(render_mcp([], "claude")))


class ContextSummaryTests(unittest.TestCase):
    def test_claude_context(self):
        reply = {
            "categories": [
                {"name": "System prompt", "tokens": 3100, "color": "x"},
                {"name": "Messages", "tokens": 41000, "color": "x"},
                {"name": "Free space", "tokens": 150000, "color": "x"},
                {"name": "Autocompact buffer", "tokens": 33000, "color": "x"},
                {"name": "System tools (deferred)", "tokens": 19200, "color": "x", "isDeferred": True},
            ],
            "totalTokens": 44100,
            "maxTokens": 200000,
            "rawMaxTokens": 200000,
            "percentage": 22.0,
            "model": "claude-opus-5-5",
            "isAutoCompactEnabled": True,
        }
        text = plain(claude_context(reply))
        self.assertIn("Context · claude-opus-5-5", text)
        self.assertIn("44.1k / 200.0k tokens  22% used", text)
        self.assertLess(text.index("Messages"), text.index("System prompt"))
        self.assertNotIn("Free space", text)
        self.assertNotIn("Autocompact buffer", text)
        self.assertNotIn("deferred", text)

    def test_large_windows_read_in_millions(self):
        text = plain(claude_context({"totalTokens": 14900, "maxTokens": 1_000_000, "categories": []}))
        self.assertIn("14.9k / 1.0M tokens  1% used", text)

    def test_codex_context(self):
        usage = {
            "last": {"totalTokens": 30000},
            "total": {"totalTokens": 90000, "inputTokens": 80000, "outputTokens": 10000},
            "modelContextWindow": 272000,
        }
        text = plain(codex_context(usage, "gpt-6.1-terra"))
        self.assertIn("30.0k / 272.0k tokens  11% used", text)
        self.assertIn("this conversation: 90.0k tokens (80.0k in, 10.0k out)", text)
        self.assertIn("No Codex conversation yet", plain(codex_context({})))


if __name__ == "__main__":
    unittest.main()
