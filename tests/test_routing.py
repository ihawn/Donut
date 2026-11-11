import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from donut import routing
from donut.terminal import Donut, Draft, RichLog, Static

Donut.AUTO_REFRESH_QUOTAS = False


class RuleTests(unittest.TestCase):
    def models(self):
        claude, codex = {"engine": "claude"}, {"engine": "codex"}
        return [
            ("claude", dict(claude, model="default"), "Default"),
            ("claude", dict(claude, model="opus"), "Opus"),
            ("claude", dict(claude, model="haiku"), "Haiku"),
            ("claude", dict(claude, model="sonnet"), "Sonnet"),
            ("claude", dict(claude, model="opusplan"), "Opus plan"),
            ("claude-second", dict(claude, model="sonnet"), "Sonnet"),  # The same model on a second login.
            ("codex", dict(codex, model="gpt-6.1-mini"), "GPT mini"),
            ("codex", dict(codex, model="gpt-6.1"), "GPT"),
        ]

    def test_the_list_is_built_cheapest_first_and_covers_the_scale(self):
        steps = routing.build(self.models())
        self.assertEqual(
            [(step["profile"]["model"], step["up_to"]) for step in steps],
            [("haiku", 2), ("gpt-6.1-mini", 4), ("sonnet", 6), ("gpt-6.1", 8), ("opus", 10)],
        )
        self.assertEqual([steps[routing.pick(steps, score)]["profile"]["model"] for score in (1, 2, 3, 6, 7, 9, 10)],
                         ["haiku", "haiku", "gpt-6.1-mini", "sonnet", "gpt-6.1", "opus", "opus"])  # fmt: skip
        self.assertEqual(steps[routing.fallback(steps)]["profile"]["model"], "sonnet")  # Unrated: the middle.
        self.assertEqual(routing.position(steps, "claude", {"model": "sonnet"}), 2)
        self.assertIsNone(routing.position(steps, "claude", {"model": "other"}))

    def test_the_configuration_can_state_the_list_and_the_classifier(self):
        config = {
            "aliases": {"claude": {"engine": "claude", "model": "sonnet", "effort": "high"}, "codex": {"engine": "codex"}},
            "routing": {
                "models": [
                    {"alias": "codex", "model": "gpt-6.1-mini", "up_to": 5},
                    {"alias": "gone", "model": "x", "up_to": 7},  # An alias that is no longer configured is skipped.
                    {"alias": "claude", "model": "opus", "up_to": 9},
                ]
            },
        }  # fmt: skip
        steps = routing.configured(config)
        self.assertEqual([(s["alias"], s["profile"]["model"], s["up_to"]) for s in steps],
                         [("codex", "gpt-6.1-mini", 5), ("claude", "opus", 10)])  # fmt: skip
        # By default the strongest model rates requests, at low effort.
        self.assertEqual(
            routing.classifier(config, steps), ("claude", {"engine": "claude", "model": "opus", "effort": "low"})
        )
        config["routing"]["classifier"] = {"alias": "codex", "model": "gpt-6.1-mini"}
        self.assertEqual(routing.classifier(config, steps), ("codex", {"engine": "codex", "model": "gpt-6.1-mini"}))
        self.assertEqual(routing.configured({"aliases": {}}), [])

    def test_scores_are_read_from_the_answer(self):
        for answer, score in (("7", 7), ("10", 10), ("Difficulty: 3", 3), ("**4**\n", 4), ("I rate it 6/10.", 6),
                              ("", None), ("eleven", None), ("42", None), ("2.5", None)):  # fmt: skip
            self.assertEqual(routing.parse_score(answer), score, answer)

    def test_the_classifier_sees_only_the_start_of_the_request(self):
        prompt = routing.classifier_prompt("x" * 10_000 + "THE END")
        self.assertNotIn("THE END", prompt)
        self.assertLess(len(prompt), routing.REQUEST_LIMIT + 2500)
        self.assertIn("<request>\nfix the typo\n</request>", routing.classifier_prompt("fix the typo"))
        for word in ("business", "engineer", "profile"):
            self.assertNotIn(word, routing.RUBRIC.lower())  # Who is asking is never part of it.


class Provider:
    """Stands in for a provider session: lists models, rates requests when asked to, and answers."""

    made = []
    score = "2"
    info = {}

    def __init__(self, project, profile, emit, approve):
        self.profile, self.emit = dict(profile), emit
        self.prompts, self.switched, self.closed = [], [], False
        self.read_only = False
        Provider.made.append(self)

    async def connect(self):
        pass

    async def query(self, text):
        self.prompts.append(text)
        if "Rate how difficult" in text:
            self.emit("text", Provider.score)
        else:
            self.emit("text", f"{self.profile.get('model')} answered")
        await asyncio.sleep(0)

    async def switch_model(self, model):
        self.switched.append(model)
        self.profile["model"] = model

    async def set_permission(self, mode):
        pass

    async def close(self):
        self.closed = True

    async def models(self):
        return [{"id": name, "name": name.capitalize()} for name in ("default", "opus", "haiku", "sonnet")]


class FakeClaude(Provider):
    pass


class RoutingSetup(unittest.IsolatedAsyncioTestCase):
    profile = None

    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        environment = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
                "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                "CODEX_HOME": str(self.root / "codex"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.config = {
            "aliases": {"claude": {"engine": "claude", "model": "opus"}},
            "planner": "claude",
            "roles": {"solo": {"alias": "auto", "model": ""}},
        }
        if self.profile:
            self.config["profile"] = self.profile
        Provider.made, Provider.score = [], "2"
        for item in (
            patch("donut.terminal.load", return_value=self.config),
            patch("donut.terminal.install"),
            patch("donut.terminal.ClaudeSession", FakeClaude),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.app = Donut(self.root)

    async def send(self, pilot, text):
        draft = self.app.query_one(Draft)
        draft.clear()
        draft.insert(text)
        await pilot.press("enter")
        for _ in range(150):
            await pilot.pause(0.02)
            if not self.app.busy:
                return

    def body(self):
        return "\n".join(strip.text for strip in self.app.query_one(RichLog).lines)

    def raters(self):
        return [p for p in Provider.made if any("Rate how difficult" in prompt for prompt in p.prompts)]

    def talkers(self):
        return [p for p in Provider.made if any("Rate how difficult" not in prompt for prompt in p.prompts)]

    def log(self):
        return [json.loads(line) for line in routing.log_path().read_text().splitlines()]


class AutoTests(RoutingSetup):
    async def test_the_first_request_picks_the_model_and_it_stays(self):
        async with self.app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.send(pilot, "/solo")
            self.assertEqual(self.app.mode, "solo")  # Auto needs no model picked first.
            await self.send(pilot, "fix the typo in the README")
            (rater,) = self.raters()
            self.assertEqual((rater.profile["model"], rater.profile["effort"]), ("opus", "low"))
            self.assertTrue(rater.read_only and rater.closed)  # A throwaway session, outside the conversation.
            self.assertIn("<request>\nfix the typo in the README\n</request>", rater.prompts[0])
            (talker,) = self.talkers()
            self.assertEqual(talker.profile["model"], "haiku")  # Rated 2: the cheapest model takes it.
            self.assertEqual(talker.prompts, ["fix the typo in the README"])  # The request, exactly as typed.
            self.assertIn("auto · difficulty 2/10 → Haiku", self.body())
            self.assertIn("auto → haiku · 2/10", self.app.query_one("#status", Static).render().plain)
            # Later messages are not rated again, however hard they are.
            Provider.score = "9"
            await self.send(pilot, "now redesign the whole storage layer")
            self.assertEqual(len(self.raters()), 1)
            self.assertEqual(talker.prompts[-1], "now redesign the whole storage layer")
            self.assertEqual(talker.switched, [])
            # /harder moves one model up, in the same conversation.
            await self.send(pilot, "/harder")
            await self.send(pilot, "try that again")
            self.assertEqual(talker.switched, ["sonnet"])
            self.assertEqual(len(self.talkers()), 1)
            await self.send(pilot, "/harder")
            await self.send(pilot, "/harder")
            self.assertIn("already the strongest model", self.body())
            self.assertEqual(self.app.profiles["solo"]["model"], "opus")
            # A new conversation is rated afresh.
            await self.send(pilot, "/clear")
            self.assertIsNone(self.app.route)
            await self.send(pilot, "design a lock-free queue")
            self.assertEqual(len(self.raters()), 2)
            self.assertEqual(self.talkers()[-1].profile["model"], "opus")
        events = self.log()
        self.assertEqual([e["event"] for e in events], ["route", "harder", "harder", "route"])
        self.assertEqual((events[0]["score"], events[0]["model"], events[0]["fallback"]), (2, "haiku", False))
        self.assertEqual((events[1]["model"], events[1]["was"]), ("sonnet", "haiku"))
        self.assertEqual((events[3]["score"], events[3]["model"]), (9, "opus"))
        self.assertNotIn("typo", routing.log_path().read_text())  # Requests are never written to the log.

    async def test_an_unrated_request_gets_the_middle_model(self):
        Provider.score = "I cannot rate this."
        async with self.app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.send(pilot, "/solo")
            await self.send(pilot, "do the thing")
            self.assertEqual(self.talkers()[-1].profile["model"], "sonnet")
            self.assertIn("could not be rated; using the middle model → Sonnet", self.body())
        self.assertTrue(self.log()[0]["fallback"])

    async def test_cancelling_while_the_request_is_rated_sends_nothing(self):
        async def slow(rater, text):
            rater.prompts.append(text)
            await asyncio.sleep(0.4)

        async with self.app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.send(pilot, "/solo")
            with patch.object(FakeClaude, "query", slow), patch.object(FakeClaude, "interrupt", create=True) as stop:
                stop.side_effect = lambda: asyncio.sleep(0)
                draft = self.app.query_one(Draft)
                draft.insert("do something slow")
                await pilot.press("enter")
                await pilot.pause(0.15)
                await pilot.press("ctrl+c")
                for _ in range(100):
                    await pilot.pause(0.02)
                    if not self.app.busy:
                        break
            self.assertIn("Cancelled before a model was chosen; nothing was sent.", self.body())
            self.assertIsNone(self.app.route)
            self.assertEqual(len(Provider.made), 2)  # The model lister and the rater: no conversation was opened.

    async def test_choosing_a_model_turns_auto_off_and_auto_can_be_chosen_again(self):
        from donut.config import load

        async with self.app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.send(pilot, "/solo")
            await self.send(pilot, "/soloist claude")
            self.assertFalse(self.app.auto)
            await self.send(pilot, "hello")
            self.assertEqual(self.raters(), [])  # A chosen model is used as it is.
            self.assertEqual(self.talkers()[-1].profile["model"], "opus")
            await self.send(pilot, "/harder")
            self.assertIn("/harder works when the model is auto", self.body())
            await self.send(pilot, "/soloist auto")
            self.assertTrue(self.app.auto)
            self.assertEqual(load()["roles"]["solo"], {"alias": "auto", "model": ""})
            await self.send(pilot, "/routing")
            body = self.body()
            self.assertIn("Auto chooses from (cheapest first)", body)
            self.assertIn("1–3   Haiku", body)
            self.assertIn("Requests are rated by opus at low effort", body)


class BusinessProfileTests(RoutingSetup):
    profile = "business"

    async def test_the_business_profile_is_one_conversation_with_auto(self):
        from donut.config import load

        async with self.app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(self.app.mode, "solo")
            names = [row[0] for row in self.app.slash_rows()]
            for hidden in ("/plan", "/work", "/review", "/planner", "/worker", "/reviewer", "/soloist", "/session"):
                self.assertNotIn(hidden, names)
            for shown in ("/harder", "/profile", "/model", "/clear", "/resume"):
                self.assertIn(shown, names)
            status = self.app.query_one("#status", Static).render().plain
            self.assertIn("model auto", status)
            for word in ("planner", "worker", "reviewer", "soloist", "PLAN", "session"):
                self.assertNotIn(word, status)
            self.assertNotIn("planner", self.body())
            self.assertNotIn("soloist", self.app.query_one(Draft).placeholder)
            await self.send(pilot, "/plan")
            self.assertEqual(self.app.mode, "solo")
            self.assertIn("/plan belongs to the engineer profile", self.body())
            await pilot.press("f3")
            await pilot.pause(0.1)
            self.assertEqual(self.app.mode, "solo")
            await self.send(pilot, "summarise the notes file")
            self.assertEqual(self.talkers()[-1].profile["model"], "haiku")
            # The user can switch profiles; the engineer interface comes back.
            await self.send(pilot, "/profile engineer")
            self.assertFalse(self.app.business)
            self.assertIn("/plan", [row[0] for row in self.app.slash_rows()])
            self.assertIn("planner", self.app.query_one("#status", Static).render().plain)
            self.assertEqual(load()["profile"], "engineer")
            await self.send(pilot, "/profile business")
            self.assertTrue(self.app.business and self.app.auto)
            self.assertEqual(load()["profile"], "business")


class ProfileIsOnlyInterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def prompts(self, profile):
        case = RoutingSetup("run")
        case.profile = profile
        await case.asyncSetUp()
        try:
            async with case.app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                if profile != "business":
                    await case.send(pilot, "/solo")
                await case.send(pilot, "draft a reply to the supplier")
                await case.send(pilot, "/harder")
                await case.send(pilot, "make it firmer")
            return [(p.profile, p.read_only, p.prompts) for p in Provider.made if p.prompts]
        finally:
            case.doCleanups()

    async def test_models_are_told_the_same_under_both_profiles(self):
        engineer = await self.prompts("engineer")
        business = await self.prompts("business")
        self.assertEqual(engineer, business)
        self.assertEqual(len(engineer), 2)  # The rater, then the conversation.
        for _, _, prompts in business:
            for prompt in prompts:
                for word in ("business", "engineer", "profile"):
                    self.assertNotIn(word, prompt.lower())


if __name__ == "__main__":
    unittest.main()
