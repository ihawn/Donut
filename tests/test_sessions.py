import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from donut.sessions import CodexSession, ClaudeSession, claude_cost


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_session_operations_keep_one_owner_task(self):
        owner_tasks = []
        session = ClaudeSession(Path("."), {}, lambda *args: None, None)

        async def connect():
            owner_tasks.append(asyncio.current_task())

        async def query(text):
            owner_tasks.append(asyncio.current_task())
            return text

        async def close():
            owner_tasks.append(asyncio.current_task())

        session._connect, session._query, session._close = connect, query, close
        await session.connect()
        self.assertEqual(await asyncio.create_task(session.query("one")), "one")
        self.assertEqual(await asyncio.create_task(session.query("two")), "two")
        await session.close()
        self.assertEqual(len(set(owner_tasks)), 1)

    async def test_codex_protocol_models_usage_approval_and_persistent_thread(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "fake_codex.py"
            script.write_text("""import sys,json
for line in sys.stdin:
 e=json.loads(line); m=e.get('method'); ident=e.get('id')
 if m=='initialize': result={}
 elif m=='model/list': result={'data':[{'model':'test','displayName':'Test model','description':'x'*100000}]}
 elif m=='account/rateLimits/read': result={'rateLimits':{'primary':{'usedPercent':25}}}
 elif m=='thread/start': result={'thread':{'id':'persistent'}}
 elif m=='turn/start':
  assert e['params']['threadId']=='persistent'
  print(json.dumps({'id':ident,'result':{}}),flush=True)
  print(json.dumps({'id':999,'method':'item/commandExecution/requestApproval','params':{'command':'test'}}),flush=True)
  continue
 elif ident==999:
  assert e['result']['decision']=='decline'
  print(json.dumps({'method':'item/agentMessage/delta','params':{'delta':'READY'}}),flush=True)
  print(json.dumps({'method':'turn/completed','params':{'turn':{'status':'completed'}}}),flush=True)
  continue
 else: continue
 print(json.dumps({'id':ident,'result':result}),flush=True)
""")
            import sys

            original = asyncio.create_subprocess_exec

            async def launch(*args, **kwargs):
                return await original(sys.executable, str(script), **kwargs)

            approvals, events = [], []

            async def approve(name, details):
                approvals.append(name)
                return False

            session = CodexSession(root, {}, lambda *event: events.append(event), approve)
            with patch("donut.sessions.asyncio.create_subprocess_exec", side_effect=launch):
                await session.connect()
            try:
                self.assertEqual((await session.models())[0]["id"], "test")
                self.assertEqual((await session.account_usage())["rateLimits"]["primary"]["usedPercent"], 25)
                await asyncio.wait_for(session.query("first"), 5)
                await asyncio.wait_for(session.query("second"), 5)
                self.assertEqual(approvals, ["item/commandExecution/requestApproval"] * 2)
                self.assertEqual([event for event in events if event[0] == "text"], [("text", "READY")] * 2)
                usage = [event[1] for event in events if event[0] == "usage"]
                self.assertEqual(usage[0][0]["percent"], 25.0)  # Reported at connect for the usage bar.
            finally:
                await session.close()


class UsageParsingTests(unittest.TestCase):
    def test_a_claude_response_is_priced_from_its_tokens_while_the_turn_runs(self):
        from claude_agent_sdk import AssistantMessage, TextBlock

        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 100_000,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation_input_tokens": 100_000,
        }
        self.assertAlmostEqual(claude_cost("claude-opus-5-5", usage), 4 + 2 + 0.2 + 0.5)
        self.assertAlmostEqual(claude_cost("sonnet", {"output_tokens": 1_000_000}), 10)
        self.assertEqual(claude_cost("gpt-6.1-mini", usage), 0.0)
        events = []
        session = ClaudeSession(Path("."), {}, lambda *args: events.append(args), None)
        session._handle(AssistantMessage([TextBlock("hi")], "claude-haiku-5-5", usage=usage, message_id="m1"), {})
        self.assertEqual(events[0][0], "spend")
        self.assertEqual(events[0][1]["id"], "m1")
        self.assertAlmostEqual(events[0][1]["dollars"], 0.1 + 0.05 + 0.01 + 0.0125)

    def test_claude_and_codex_windows(self):
        from types import SimpleNamespace
        from donut.sessions import claude_usage, codex_usage

        info = SimpleNamespace(
            raw={
                "unifiedWindows": {"five_hour": {"utilization": 0.18, "resetsAt": 1}, "seven_day": {"utilization": 0.3}}
            },
            utilization=None,
            rate_limit_type="five_hour",
            resets_at=1,
        )
        self.assertEqual([(w["window"], round(w["percent"])) for w in claude_usage(info)], [("5h", 18), ("7d", 30)])
        snapshot = {
            "primary": {"usedPercent": 12, "windowDurationMins": 300},
            "secondary": {"usedPercent": 40, "windowDurationMins": 10080},
        }
        self.assertEqual([(w["window"], w["percent"]) for w in codex_usage(snapshot)], [("5h", 12.0), ("7d", 40.0)])
        self.assertEqual(codex_usage(None), [])

    def test_claude_get_usage_answer(self):
        from donut.sessions import claude_plan_usage

        answer = {
            "rate_limits_available": True,
            "rate_limits": {
                "five_hour": {"utilization": 18.0, "resets_at": "2026-11-09T03:40:00Z"},
                "seven_day": {"utilization": 30.0, "resets_at": None},
                "seven_day_opus": None,
            },
        }
        windows = claude_plan_usage(answer)
        self.assertEqual([(w["window"], w["percent"]) for w in windows], [("5h", 18.0), ("7d", 30.0)])
        self.assertEqual(windows[0]["resets_at"], 1794195600.0)
        self.assertEqual(claude_plan_usage({"rate_limits_available": False, "rate_limits": None}), [])
        self.assertIsNone(claude_plan_usage({"rate_limits_available": True, "rate_limits": None}))
        self.assertIsNone(claude_plan_usage("unexpected"))


class ResumePlumbingTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_resumes_its_thread(self):
        session = CodexSession(Path("."), {"model": "gpt-6.1"}, lambda *event: None, None)
        session.resume_id = "thread-9"
        sent = []

        async def request(method, params=None):
            sent.append((method, params))
            if method == "thread/resume":
                return {"thread": {"id": params["threadId"]}}
            session.turn_done.set_result(None)
            return {}

        session.request = request
        await asyncio.wait_for(session.query("continue"), 2)
        self.assertEqual(sent[0][0], "thread/resume")
        self.assertEqual(sent[0][1]["threadId"], "thread-9")
        self.assertEqual(sent[1][1]["threadId"], "thread-9")
        self.assertEqual(session.provider_id, "thread-9")

    async def test_qwen_and_claude_resume_ids(self):
        from unittest.mock import patch
        from donut.sessions import QwenSession

        qwen = QwenSession(Path("."), {"engine": "ollama", "model": "m"}, lambda *event: None, None)
        qwen.resume_id = "qwen-sid"
        await qwen.connect()
        self.assertEqual(qwen.provider_id, "qwen-sid")

        captured = {}

        class Client:
            def __init__(self, options):
                captured["resume"] = options.resume

            async def connect(self):
                pass

            async def get_server_info(self):
                return {}

            async def disconnect(self):
                pass

        claude = ClaudeSession(Path("."), {}, lambda *args: None, None)
        claude.resume_id = "claude-sid"
        with patch("claude_agent_sdk.ClaudeSDKClient", Client):
            await claude.connect()
            await claude.close()
        self.assertEqual(captured["resume"], "claude-sid")


class ConversationStoreTests(unittest.TestCase):
    def test_store_keeps_recent_messages_and_is_private(self):
        import os
        import tempfile
        from unittest.mock import patch
        from donut import conversations

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XDG_STATE_HOME": temp}):
            record = None
            for turn in range(10):
                record = conversations.record_turn(
                    record, temp, "plan", "plan", "claude", "opus", "sid", f"ask {turn}", "x" * 5000
                )
            saved = conversations.get(record)
            self.assertEqual(saved["turns"], 10)
            self.assertEqual(saved["title"], "ask 0")
            self.assertEqual(len(saved["messages"]), conversations.KEEP_MESSAGES)
            self.assertTrue(all(len(m["text"]) <= conversations.MESSAGE_CHARS for m in saved["messages"]))
            if os.name != "nt":
                self.assertEqual(conversations.store_path().stat().st_mode & 0o777, 0o600)
            self.assertEqual(conversations.for_project(os.path.join(temp, "elsewhere")), [])
            # /resume shows every exchange in full, from the transcript kept beside the list.
            shown = conversations.transcript(saved)
            self.assertEqual(len(shown), 10)
            # Each exchange is logged with the model that answered and its cost, for later analysis.
            first = json.loads(conversations.transcript_path(record).read_text().splitlines()[0])
            self.assertEqual((first["model"], first["dollars"]), ("opus", None))
            self.assertEqual(shown[0], ("ask 0", "x" * 5000))
            if os.name != "nt":
                self.assertEqual(conversations.transcript_path(record).stat().st_mode & 0o777, 0o600)
            # A conversation saved before transcripts were kept shows the exchanges its record still has.
            conversations.transcript_path(record).unlink()
            self.assertEqual(len(conversations.transcript(saved)), conversations.KEEP_MESSAGES // 2)
            # A renamed session names its conversations, even ones saved with no name of their own.
            self.assertIsNone(conversations.display_name({"session": "20261109-1712"}))
            self.assertIsNone(conversations.display_name({"session": "20261109-1712-2"}))
            self.assertEqual(conversations.display_name({"session": "fix-login-bug"}), "fix-login-bug")
            self.assertEqual(conversations.display_name({"session": "fix-login", "name": "fix login"}), "fix login")
        self.assertEqual(conversations.ago(1000, now=1000 + 7200), "2h ago")
