import contextlib
import io
import json
import os
from unittest.mock import patch
import unittest
from donut.ui import Progress, paint, welcome, LOGO, CodeStream
from donut.runner import render_event


class UITests(unittest.TestCase):
    def test_clear_does_not_erase_streamed_tokens(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), patch.object(stream, "isatty", return_value=True):
            progress = Progress()
            progress.clear()
            self.assertEqual(stream.getvalue(), "")
            progress.tick()
            self.assertIn("Working", stream.getvalue())
            progress.clear()
            self.assertTrue(stream.getvalue().endswith("\r\033[2K"))

    def test_no_color(self):
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertEqual(paint("hello"), "hello")

    def test_summary_hides_reasoning_but_keeps_answer(self):
        event = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "private summary reasoning"},
            },
        }
        self.assertEqual(render_event(json.dumps(event), show_thinking=False), "")
        self.assertIn("private summary reasoning", render_event(json.dumps(event)))
        event["event"]["delta"] = {"type": "text_delta", "text": "| 025 | Done | Tested |"}
        self.assertIn("025", render_event(json.dumps(event), show_thinking=False))

    def test_welcome_is_ascii_and_plain_when_redirected(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), patch("donut.ui.version", return_value="1.2.3"):
            welcome(installed=True)
        output = stream.getvalue()
        self.assertTrue(output.isascii())
        self.assertNotIn("\x1b", output)
        self.assertIn("do the thing", output)
        self.assertIn("1.2.3", output)
        self.assertIn("donut setup", output)
        self.assertLessEqual(max(map(len, LOGO.splitlines())), 72)  # Fits an 80-column terminal with its indent.

    def test_logo_renders_the_same_in_any_monospace_font(self):
        lines = LOGO.splitlines()
        # ~ is drawn differently by different fonts, which broke up the outline.
        self.assertNotIn("~", LOGO)
        # The speech bubble's three rows share one right edge.
        self.assertEqual(len({len(line.rstrip()) for line in lines[:3]}), 1)
        # Each donut row is mirror-symmetric apart from its sprinkles.
        for line in lines:
            donut = line[:36].rstrip()
            outline = "".join(" " if c in "*+o" else c for c in donut)
            swapped = outline.translate(str.maketrans("/\\", "\\/"))[::-1]
            self.assertEqual(outline.strip(), swapped.strip(), line)

    def test_setup_welcome_does_not_claim_new_installation(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            welcome()
        self.assertIn("SETUP", stream.getvalue())
        self.assertNotIn("Installation complete", stream.getvalue())

    def test_code_highlighting_survives_split_deltas(self):
        text = 'Explanation `inline`\n```python\ndef example():\n    return "hello" + 42\n```\nDone\n'
        stream = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stdout(stream),
            patch.object(stream, "isatty", return_value=True),
        ):
            renderer = CodeStream()
            output = "".join(renderer.feed(character) for character in text) + renderer.feed("", final=True)
        import re

        self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", output), text)
        self.assertIn("38;2;255;94;78mdef", output)
        self.assertIsNone(renderer.fence)

    def test_code_highlighting_respects_no_color_and_flushes(self):
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            renderer = CodeStream()
            output = renderer.feed("```cs\nreturn 1;") + renderer.feed("", final=True)
        self.assertEqual(output, "```cs\nreturn 1;")

    def test_thinking_tokens_and_errors_use_palette(self):
        stream = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stdout(stream),
            patch.object(stream, "isatty", return_value=True),
        ):
            renderer = CodeStream()
            output = renderer.feed("Use `file.py` with 42 (optional) {value}\n[Error] Failed 3 tests\n")
        self.assertIn("38;2;255;168;24m`file.py`", output)
        self.assertIn("38;2;255;94;78m42", output)
        self.assertIn("38;2;23;184;190m(optional)", output)
        self.assertIn("38;2;23;184;190m{value}", output)
        self.assertIn("38;2;255;94;78m[Error] Failed 3 tests", output)
        import re

        self.assertEqual(
            re.sub(r"\x1b\[[0-9;]*m", "", output), "Use `file.py` with 42 (optional) {value}\n[Error] Failed 3 tests\n"
        )

    def test_multiline_error_body_stays_red_until_next_section(self):
        stream = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stdout(stream),
            patch.object(stream, "isatty", return_value=True),
        ):
            output = CodeStream().feed("[Error · Tool result]\nCommand failed\n[model]\nRetrying\n")
        self.assertIn("38;2;255;94;78mCommand failed", output)
        self.assertNotIn("38;2;255;94;78mRetrying", output)

    def test_tool_errors_are_labeled_as_errors(self):
        event = {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "is_error": True, "content": "Failed"}]},
        }
        self.assertIn("[Error", render_event(json.dumps(event)))

    def test_model_labels(self):
        event = {
            "type": "stream_event",
            "event": {"type": "content_block_start", "content_block": {"type": "text", "text": "Hello"}},
        }
        rendered = render_event(json.dumps(event), model="qwen3.8:27b")
        self.assertIn("[qwen3.8:27b]", rendered)
        self.assertNotIn("[Agent]", rendered)

    def test_codex_output(self):
        output = render_event(
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "READY"}})
        )
        self.assertIn("READY", output)


if __name__ == "__main__":
    unittest.main()
