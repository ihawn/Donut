import unittest

from donut.highlight import CORAL, ORANGE, TEAL, Highlighter


def styles(text):
    """{substring: style} for each styled span of a Rich Text."""
    return {text.plain[span.start : span.end]: str(span.style) for span in text.spans}


class HighlighterTests(unittest.TestCase):
    def test_text_is_never_changed(self):
        lines = [
            "[qwen3 · Connected · preparing project context]",
            "[1/3] 047_20261108-0831_sandbox-wall-section.md — fresh context",
            "Project: /Users/me/doodle_quest",
            "```python",
            "def jump(height=2.5):  # comment",
            '    return f"{height}"',
            "```",
            "| 084 lightning-fire | Done | Fire burns 5s then smokes |",
            "|---|---|---|",
            "- Ran `pytest -q`: 12 passed, 1 failed in 3.2s",
            "[Error] Command failed",
        ]
        highlighter = Highlighter()
        for line in lines:
            self.assertEqual(highlighter.line(line).plain, line)

    def test_code_blocks_use_the_language_lexer(self):
        highlighter = Highlighter()
        highlighter.line("```python")
        spans = styles(highlighter.line("def jump(height=2):  # go up"))
        self.assertEqual(spans["def"], CORAL)
        self.assertEqual(spans["jump"], f"bold {ORANGE}")
        self.assertEqual(spans["2"], ORANGE)
        self.assertIn("italic", spans["# go up"])
        highlighter.line("```")
        self.assertIsNone(highlighter.fence)

    def test_line_comments_and_absolute_paths(self):
        highlighter = Highlighter()
        highlighter.line("```csharp")
        comment = highlighter.line("    // One collider per section")
        self.assertEqual(comment.plain, "    // One collider per section")
        self.assertIn("italic", styles(comment)["// One collider per section"])
        highlighter.line("```")
        spans = styles(highlighter.line("Logs: /Users/me/.local/state/donut/run-1"))
        self.assertIn("/Users/me/.local/state/donut/run-1", spans)

    def test_unknown_language_still_gets_basic_tokens(self):
        highlighter = Highlighter()
        highlighter.line("```")
        spans = styles(highlighter.line('return "ok" + 42'))
        self.assertEqual(spans["return"], CORAL)
        self.assertEqual(spans['"ok"'], TEAL)

    def test_prose_highlights_brackets_code_numbers_and_status_words(self):
        spans = styles(Highlighter().line("Ran `pytest -q` (unit): 12 passed, 1 failed [slow] in 3.2s"))
        self.assertEqual(spans["`pytest -q`"], ORANGE)
        self.assertEqual(spans["(unit)"], TEAL)
        self.assertEqual(spans["[slow]"], TEAL)
        self.assertEqual(spans["12"], CORAL)
        self.assertEqual(spans["3.2s"], CORAL)
        self.assertEqual(spans["passed"], f"bold {TEAL}")
        self.assertEqual(spans["failed"], f"bold {CORAL}")

    def test_runner_lines_and_tables(self):
        highlighter = Highlighter()
        ticket = styles(highlighter.line("[1/3] 047_sandbox-wall.md — fresh context"))
        self.assertEqual(ticket["[1/3]"], f"bold {ORANGE}")
        self.assertEqual(ticket["047_sandbox-wall.md"], f"bold {TEAL}")
        row = styles(highlighter.line("| 086 install-thunderstorm | Done | Full storm cycle |"))
        self.assertEqual(row["Done"], f"bold {TEAL}")
        self.assertNotIn(ORANGE, row["|"])
        self.assertEqual(str(highlighter.line("PLANNING COMPLETE").style), f"bold {ORANGE}")

    def test_thinking_sections_are_dimmed_until_the_next_label(self):
        highlighter = Highlighter()
        highlighter.line("[qwen3 · Thinking]")
        self.assertIn("italic", str(highlighter.line("Let me look at the wall code.").style))
        highlighter.line("[qwen3]")
        self.assertNotIn("italic", str(highlighter.line("Done.").style))


if __name__ == "__main__":
    unittest.main()
