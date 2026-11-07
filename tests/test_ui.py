import contextlib
import io
import json
import os
from unittest.mock import patch
import unittest
from donut.ui import Progress, paint
from donut.runner import render_event

class UITests(unittest.TestCase):
    def test_clear_does_not_erase_streamed_tokens(self):
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream),patch.object(stream,'isatty',return_value=True):
            progress=Progress()
            progress.clear()
            self.assertEqual(stream.getvalue(),'')
            progress.tick()
            self.assertIn('Working',stream.getvalue())
            progress.clear()
            self.assertTrue(stream.getvalue().endswith('\r\033[2K'))

    def test_no_color(self):
        with patch.dict(os.environ,{'NO_COLOR':'1'}):
            self.assertEqual(paint('hello'),'hello')

    def test_summary_hides_reasoning_but_keeps_answer(self):
        event = {'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'private summary reasoning'}}}
        self.assertEqual(render_event(json.dumps(event), show_thinking=False), '')
        self.assertIn('private summary reasoning', render_event(json.dumps(event)))
        event['event']['delta'] = {'type':'text_delta','text':'| 025 | Done | Tested |'}
        self.assertIn('025', render_event(json.dumps(event), show_thinking=False))

    def test_codex_output(self):
        output=render_event(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'READY'}}))
        self.assertIn('READY',output)

if __name__=='__main__':unittest.main()
