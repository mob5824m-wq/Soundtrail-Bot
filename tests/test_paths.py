"""
Tests for runtime path resolution (paths.py).

Run with either:
    python -m pytest tests/test_paths.py
    python tests/test_paths.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import paths  # noqa: E402


class PathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("SOUNDTRAIL_SUPPORT_DATA")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SOUNDTRAIL_SUPPORT_DATA", None)
        else:
            os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._old
        self._tmp.cleanup()

    def test_env_var_redirects_all_state(self):
        os.environ["SOUNDTRAIL_SUPPORT_DATA"] = str(Path(self._tmp.name) / "state")
        self.assertEqual(paths.data_dir(), Path(self._tmp.name) / "state")
        self.assertTrue(paths.data_dir().is_dir())
        self.assertEqual(paths.db_path().parent, paths.data_dir())
        self.assertEqual(paths.log_path().parent, paths.data_dir())
        self.assertEqual(paths.config_path().parent, paths.data_dir())

    def test_default_data_dir_is_inside_repo(self):
        os.environ.pop("SOUNDTRAIL_SUPPORT_DATA", None)
        self.assertEqual(paths.data_dir(), REPO_ROOT / "data")

    def test_template_config_is_never_the_live_config(self):
        os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._tmp.name
        self.assertNotEqual(paths.config_path(), paths.TEMPLATE_CONFIG_PATH)

    def test_version_file_is_read(self):
        self.assertNotEqual(paths.app_version(), "")
        self.assertNotEqual(paths.app_version(), "unknown")


if __name__ == "__main__":
    unittest.main()
