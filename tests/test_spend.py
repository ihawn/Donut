import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from donut import spend
from donut.terminal import RUNNER_SPEND, Donut


class SpendTests(unittest.IsolatedAsyncioTestCase):
    def test_tokens_are_priced_with_the_cached_part_at_its_own_rate(self):
        # A real Codex report: the input count includes the cached tokens.
        usage = {"input_tokens": 21706, "cached_input_tokens": 13312, "output_tokens": 5, "reasoning_output_tokens": 0}
        self.assertAlmostEqual(spend.cost("gpt-6.1-sol", usage), (8394 * 2 + 13312 * 0.1 + 5 * 10) / 1e6)
        thread = {"inputTokens": 1_000_000, "cachedInputTokens": 0, "outputTokens": 100_000}  # The app server's names.
        self.assertAlmostEqual(spend.cost("gpt-6-astra", thread), 10 + 5)
        self.assertIsNone(spend.cost("some-new-model", usage))  # No price: never a guess.
        config = {
            "prices": {"some-new-model": {"input": 1, "cached_input": 0.5, "output": 4}, "gpt-6.1-sol": [4, 1, 20]}
        }
        self.assertAlmostEqual(
            spend.cost("some-new-model", {"prompt_tokens": 2_000_000, "completion_tokens": 0}, config), 2
        )
        self.assertAlmostEqual(spend.cost("gpt-6.1-sol", thread, config), 4 + 2)  # The configuration's price wins.
        self.assertEqual(spend.rates("gpt-6.1-sol-pro", {"prices": {"gpt-6.1-sol-pro": [9, 9, 9]}}), (9.0, 9.0, 9.0))

    def test_the_platform_decides_whether_work_is_counted(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"CODEX_HOME": home}):
            self.assertEqual(spend.platform({"engine": "codex"}), "api")
            Path(home, "auth.json").write_text('{"auth_mode": "chatgpt"}')
            self.assertEqual(spend.platform({"engine": "codex"}), "plan")
        self.assertEqual(spend.platform({"engine": "ollama"}), "local")
        self.assertEqual(spend.platform({"engine": "claude"}, "none"), "plan")
        self.assertEqual(spend.platform({"engine": "claude"}, "ANTHROPIC_API_KEY"), "api")
        self.assertEqual(spend.platform({"engine": "openai"}), "api")
        report = {"claude-opus-5-5": {"provider": "bedrock", "costUSD": 1.0}}
        self.assertEqual(
            spend.platform({"engine": "claude"}, spend.claude_account({"apiKeySource": "none"}, report)), "bedrock"
        )
        self.assertEqual(spend.claude_account({"apiKeySource": "none"}, {"m": {"provider": "firstParty"}}), "none")
        self.assertFalse(spend.counted("local"))
        self.assertTrue(spend.counted("plan"))  # At the list price, unless the configuration says plans are free.
        self.assertFalse(spend.counted("plan", {"spend": {"plans": "free"}}))
        self.assertTrue(spend.counted("bedrock", {"spend": {"plans": "free"}}))

    async def test_every_provider_adds_to_the_one_figure(self):
        home = {"CODEX_HOME": "", "XDG_CONFIG_HOME": "", "XDG_STATE_HOME": ""}
        quiet = (patch.object(Donut, "AUTO_REFRESH_QUOTAS", False), patch.object(Donut, "ASK_INTERFACE", False))
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(os.environ, dict.fromkeys(home, temp)),
            quiet[0],
            quiet[1],
        ):
            config = {"aliases": {"claude": {"engine": "claude"}}, "planner": "claude"}
            with patch("donut.terminal.load", return_value=config):
                app = Donut(Path(temp))
            async with app.run_test() as pilot:
                await pilot.pause()
                claude = SimpleNamespace(profile={"engine": "claude", "model": "opus"}, account="key", usage={})
                claude.usage = {"reported_cost_usd": 0.50}
                app.add_spent(claude)
                codex = SimpleNamespace(profile={"engine": "codex", "model": "gpt-6.1-sol"}, usage={})
                codex.usage = {"total": {"inputTokens": 100_000, "cachedInputTokens": 0, "outputTokens": 10_000}}
                app.add_spent(codex)  # 0.20 + 0.10
                codex.usage = {"total": {"inputTokens": 200_000, "cachedInputTokens": 0, "outputTokens": 20_000}}
                app.add_spent(codex)  # The thread's total grew: only the difference is added.
                local = SimpleNamespace(
                    profile={"engine": "ollama", "model": "qwen3"}, usage={"input_tokens": 9_000_000}
                )
                app.add_spent(local)
                unknown = SimpleNamespace(profile={"engine": "openai", "model": "mystery"}, usage={"input_tokens": 5})
                app.add_spent(unknown)
                line = RUNNER_SPEND.match("Spend: $0.4000 (gpt-6.1-terra, api)")  # A worker of a /work run.
                app.count_spend(line["model"], line["where"], float(line["dollars"]))
                app.add_spent(None)
                self.assertAlmostEqual(app.spent, 0.50 + 0.60 + 0.40)
                self.assertEqual(app.query_one("#draft").border_subtitle, "$1.50 spent")
                self.assertIsNone(app.spend_by[("mystery", "api")])  # Listed as not priced by /usage.
                self.assertEqual(app.spend_by[("qwen3", "local")], 0.0)
                self.assertIsNone(RUNNER_SPEND.match("Spend: not priced (codex (provider default), plan)")["dollars"])
                # With plans counted as free, a plan login adds nothing.
                app.config["spend"] = {"plans": "free"}
                plan = SimpleNamespace(profile={"engine": "claude", "model": "opus"}, account="none", usage={})
                plan.usage = {"reported_cost_usd": 3.0}
                app.add_spent(plan)
                self.assertAlmostEqual(app.spent, 1.50)


if __name__ == "__main__":
    unittest.main()
