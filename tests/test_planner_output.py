import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from donut.planner_output import PlannerOutput, ticket_snapshot, show_created_tickets


class PlannerOutputTests(unittest.TestCase):
    def test_activity_is_concise_and_payloads_are_hidden(self):
        progress = Mock()
        view = PlannerOutput(progress)
        output = io.StringIO()
        events = [
            {'type': 'stream_event', 'event': {'type': 'content_block_start', 'content_block': {'type': 'thinking', 'thinking': 'NOISY REASONING'}}},
            {'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'thinking_delta', 'thinking': 'MORE REASONING'}}},
            {'type': 'stream_event', 'event': {'type': 'content_block_start', 'content_block': {'type': 'tool_use', 'name': 'read_file'}}},
            {'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'input_json_delta', 'partial_json': 'RAW ARGUMENTS'}}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'HUGE FILE CONTENTS'}]}},
        ]
        with contextlib.redirect_stdout(output):
            for event in events + events: view.feed(json.dumps(event))
        self.assertEqual(output.getvalue().count('Reading project files'), 1)
        for payload in ('REASONING', 'RAW ARGUMENTS', 'HUGE FILE'): self.assertNotIn(payload, output.getvalue())
        progress.tick.assert_any_call('Thinking')

    def test_final_answer_is_brief_and_not_duplicated(self):
        view = PlannerOutput(Mock())
        output = io.StringIO()
        answer = 'Planned a settings menu. Added focused tickets. Extra details should stay hidden.'
        events = [
            {'type': 'stream_event', 'event': {'type': 'content_block_start', 'content_block': {'type': 'text'}}},
            {'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': answer}}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': answer}]}},
            {'type': 'result', 'result': answer},
        ]
        with contextlib.redirect_stdout(output):
            for event in events: view.feed(json.dumps(event))
            self.assertEqual(output.getvalue(), '')
            view.finish(True)
        self.assertEqual(output.getvalue().strip(), 'Planned a settings menu. Added focused tickets.')

    def test_codex_errors_and_summary_limits(self):
        view = PlannerOutput(Mock())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            view.feed(json.dumps({'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'VERBOSE COMMAND'}}))
            view.feed(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution', 'exit_code': 1, 'aggregated_output': 'VERBOSE OUTPUT'}}))
            view.feed(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'word ' * 200}}))
            view.finish(True)
        self.assertIn('Inspecting the project', output.getvalue())
        self.assertIn('Error:', output.getvalue())
        self.assertNotIn('VERBOSE', output.getvalue())
        self.assertLessEqual(len(output.getvalue().splitlines()[-1]), 300)
        failed = io.StringIO()
        with contextlib.redirect_stdout(failed): view.finish(False)
        self.assertEqual(failed.getvalue(), '')

    def test_ticket_list_ignores_old_edited_and_renumbered_tickets(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            lane = project/'swim_lanes/to_do'
            lane.mkdir(parents=True)
            old = lane/'001_existing.md'
            old.write_text('**Slug:** `existing`\n')
            before = ticket_snapshot(project)
            old.rename(lane/'002_existing.md')
            (lane/'002_existing.md').write_text('**Slug:** `existing`\nUpdated requirements\n')
            (lane/'001_new.md').write_text('**Slug:** `new`\n')
            output = io.StringIO()
            with contextlib.redirect_stdout(output): show_created_tickets(project, before)
            self.assertIn('Tickets created: 1', output.getvalue())
            self.assertIn('001_new.md', output.getvalue())
            self.assertNotIn('existing.md', output.getvalue())
            unchanged = io.StringIO()
            with contextlib.redirect_stdout(unchanged): show_created_tickets(project, ticket_snapshot(project))
            self.assertIn('Tickets created: 0', unchanged.getvalue())

    def test_released_hold_ticket_is_not_reported_as_new(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            held = project/'swim_lanes/blocked'
            todo = project/'swim_lanes/to_do'
            held.mkdir(parents=True)
            todo.mkdir()
            ticket = held/'001_held.md'
            ticket.write_text('**Slug:** `held`\n')
            before = ticket_snapshot(project)
            ticket.rename(todo/ticket.name)
            output = io.StringIO()
            with contextlib.redirect_stdout(output): show_created_tickets(project, before)
            self.assertIn('Tickets created: 0', output.getvalue())


if __name__ == '__main__': unittest.main()
