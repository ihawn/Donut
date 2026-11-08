import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "release_version", Path(__file__).parents[1] / "scripts/release_version.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReleaseVersionTests(unittest.TestCase):
    def test_first_release(self):
        self.assertEqual(module.next_version("0.1.0", []), "0.1.0")

    def test_patch_increment_ignores_nonstable_tags(self):
        self.assertEqual(module.next_version("0.1.0", ["v0.1.2", "v0.1.9", "v0.2.0-rc1", "other"]), "0.1.10")

    def test_explicit_new_series(self):
        self.assertEqual(module.next_version("0.2.0", ["v0.1.9"]), "0.2.0")

    def test_retry_reuses_commit_tag(self):
        self.assertEqual(module.next_version("0.1.0", ["v0.1.9", "v0.1.10"], ["v0.1.9"]), "0.1.9")

    def test_invalid_base_rejected(self):
        with self.assertRaises(ValueError):
            module.next_version("broken", [])
