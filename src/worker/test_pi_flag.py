#!/usr/bin/env python3
"""Worker hot-read path: _pi_enabled() must resolve true when the .env holds
PI_AGENT_ENABLED=true — the bare value the autonomy save now writes. Regression
for the 08-17 bug where autonomous mode silently degraded to the judge because
the wizard never set the flag.

Run from src/worker:
    python3 -m unittest test_pi_flag -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))            # worker/
# APPEND (not insert-0) so `import main` resolves to src/worker/main.py,
# not src/api/main.py (the FastAPI app).
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "api"))   # api/

_TMP = tempfile.mkdtemp(prefix="pi-flag-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

import main as worker  # noqa: E402


class PiEnabledTest(unittest.TestCase):
    def _enabled(self, file_env: dict, proc_pi: str) -> bool:
        with patch("llm_providers.read_env_file", return_value=dict(file_env)), \
             patch.dict(os.environ, {"PI_AGENT_ENABLED": proc_pi}):
            return worker._pi_enabled()

    def test_file_true_wins(self):
        # The autonomy save writes this bare value — the hot-read must resolve true.
        self.assertTrue(self._enabled({"PI_AGENT_ENABLED": "true"}, "false"))

    def test_file_false_disables(self):
        self.assertFalse(self._enabled({"PI_AGENT_ENABLED": "false"}, "true"))

    def test_env_fallback(self):
        # No file value -> process env fallback (env_file: injection).
        self.assertTrue(self._enabled({}, "1"))
        self.assertFalse(self._enabled({}, "0"))

    def test_default_off(self):
        self.assertFalse(self._enabled({}, ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
