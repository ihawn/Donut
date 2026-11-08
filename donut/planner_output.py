"""Compact planner presentation; detailed events remain in the run logs."""
import json
import re
import time
from .ui import paint


class PlannerOutput:
    def __init__(self, progress):
        self.progress = progress
        self.activity = 'Thinking'
        self.seen = set()
        self.answer = ''
        self.last_tick = 0

    def tick(self):
        now = time.monotonic()
        if now - self.last_tick >= 0.2:
            self.progress.tick(self.activity)
            self.last_tick = now

    def set_activity(self, text):
        self.activity = text
        if text not in self.seen:
            self.progress.clear()
            print(paint('  · ' + text, 'teal'), flush=True)
            self.seen.add(text)

    def feed(self, line):
        try:
            item = json.loads(line)
        except ValueError:
            if re.match(r'\s*(error|fatal|traceback)\b', line, re.I): self.error(line)
            self.tick()
            return
        if not isinstance(item, dict): return
        kind = item.get('type')
        if kind in ('error', 'turn.failed') or item.get('is_error'):
            self.error(item.get('error', item.get('result', 'Planner reported an error; see logs.')))
        elif kind == 'system' and item.get('subtype') == 'init':
            self.set_activity('Reading project context')
        elif kind in ('item.started', 'item.completed'):
            entry = item.get('item', {})
            if entry.get('type') == 'reasoning': self.activity = 'Thinking'
            elif entry.get('type') == 'agent_message': self.answer = entry.get('text', '')
            elif entry.get('type') == 'file_change': self.set_activity('Writing tickets')
            elif entry.get('type') == 'command_execution': self.set_activity('Inspecting the project')
            if entry.get('exit_code') not in (None, 0): self.error('A planner command failed; see logs.')
        elif kind == 'stream_event':
            event = item.get('event', {})
            block = event.get('content_block', {})
            delta = event.get('delta', {})
            if event.get('type') == 'content_block_start':
                if block.get('type') == 'thinking': self.activity = 'Thinking'
                elif block.get('type') == 'text':
                    self.answer = block.get('text', '')
                    self.activity = 'Preparing the plan'
                elif block.get('type') == 'tool_use':
                    name = block.get('name', '').lower()
                    if any(word in name for word in ('write', 'edit', 'patch')): activity = 'Writing tickets'
                    elif any(word in name for word in ('read', 'search', 'grep', 'glob', 'list')): activity = 'Reading project files'
                    else: activity = 'Inspecting the project'
                    self.set_activity(activity)
            elif delta.get('type') == 'text_delta': self.answer += delta.get('text', '')
            elif delta.get('type') == 'thinking_delta': self.activity = 'Thinking'
        elif kind == 'assistant':
            content = item.get('message', {}).get('content', [])
            if isinstance(content, list):
                text = ''.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text')
                if text: self.answer = text
        elif kind == 'result' and isinstance(item.get('result'), str):
            self.answer = item['result']
        elif kind == 'user':
            content = item.get('message', {}).get('content', [])
            if isinstance(content, list) and any(isinstance(b, dict) and b.get('is_error') for b in content):
                self.error('A planner tool failed; see logs.')
        self.tick()

    def error(self, message):
        self.progress.clear()
        if isinstance(message, dict): message = message.get('message', 'Planner error; see logs.')
        print(paint('  Error: ' + ' '.join(str(message).split())[:200], 'coral'), flush=True)

    def finish(self, success):
        self.progress.clear()
        if not success: return
        text = ' '.join(self.answer.split())
        if text:
            sentences = re.split(r'(?<=[.!?])\s+', text)
            text = ' '.join(sentences[:2])
            if len(text) > 300: text = text[:297].rsplit(' ', 1)[0] + '…'
            print('\n' + text)


def ticket_snapshot(project):
    tickets = {}
    for lane in ('to_do', 'in_progress', 'on_hold', 'blocked', 'done'):
        for path in sorted((project/'swim_lanes'/lane).glob('[0-9]*_*.md')):
            text = path.read_text(encoding='utf-8')
            match = re.search(r'^\*\*Slug:\*\*\s*`?([^`\n]+)', text, re.M)
            identity = match[1].strip() if match else path.name
            tickets[identity] = path
    return tickets


def show_created_tickets(project, before):
    created = [path for identity, path in ticket_snapshot(project).items() if identity not in before]
    print(paint(f'\nTickets created: {len(created)}', 'teal'))
    for path in sorted(created):
        print('  - ' + path.relative_to(project).as_posix())
