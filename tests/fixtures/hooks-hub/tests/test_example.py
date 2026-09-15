"""A hub-owned suite executed by the reusable workflow's optional test job."""

import json
from pathlib import Path
import subprocess
import sys
import unittest


class ExampleHookTests(unittest.TestCase):
    def test_hook_response(self):
        script = Path(__file__).resolve().parents[1] / "assets/hooks/example/run.py"
        result = subprocess.run(
            [sys.executable, str(script)],
            input='{"event": "session_start"}',
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), {"context": "session_start"})
