import subprocess
import sys
import unittest
from donut.platform import process_options, stop_process


class PlatformTests(unittest.TestCase):
    def test_stop_running_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **process_options())
        try:
            stop_process(process)
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_stopping_finished_process_is_safe(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"], **process_options())
        process.wait()
        stop_process(process)
        self.assertEqual(process.returncode, 0)
