import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from donut.platform import acquire_lock, process_options, stop_process


class PlatformTests(unittest.TestCase):
    def test_lock_excludes_other_process_and_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'project.lock'
            lock = acquire_lock(path)
            try:
                code = 'from donut.platform import acquire_lock; import sys; from pathlib import Path; lock=acquire_lock(Path(sys.argv[1]))'
                result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            finally:
                lock.close()
            second = acquire_lock(path)
            second.close()

    def test_stop_running_process(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], **process_options())
        try:
            stop_process(process)
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None: process.kill(); process.wait()

    def test_stopping_finished_process_is_safe(self):
        process = subprocess.Popen([sys.executable, '-c', 'pass'], **process_options())
        process.wait()
        stop_process(process)
        self.assertEqual(process.returncode, 0)
