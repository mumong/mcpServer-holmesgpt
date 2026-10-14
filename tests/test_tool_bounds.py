"""Output and timeout bounds default to no-ops for normal results."""

import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "servers"))
from holmes_tools import _command_runner
from holmes_tools.arg_utils import clamp_timeout


class ToolBoundsTests(unittest.TestCase):
    def run_command(self, stdout, env):
        done = SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        with patch.dict(os.environ, env), patch.object(_command_runner.subprocess, "run", return_value=done), \
                patch.object(_command_runner, "_render", return_value="echo 1"):
            return _command_runner.run_command("echo {{ x }}", {"x": "1"})

    def test_default_output_cap_keeps_normal_output(self):
        self.assertEqual(self.run_command("a" * 5000 + "\n", {}), "a" * 5000)

    def test_output_cap_truncates_on_utf8_boundary_with_marker(self):
        out = self.run_command("输出" * 10, {"MCP_TOOL_MAX_OUTPUT_BYTES": "7"})
        self.assertTrue(out.startswith("输出\n...[output truncated: 60 bytes exceeds limit of 7 bytes]"))

    def test_timeout_clamp(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_TOOL_MAX_TIMEOUT_SECONDS", None)
            self.assertEqual(clamp_timeout(60), 60)
            self.assertEqual(clamp_timeout(10_000), 600)
            self.assertIsInstance(clamp_timeout(10_000), int)
            self.assertEqual(clamp_timeout(3.0), 3.0)
        with patch.dict(os.environ, {"MCP_TOOL_MAX_TIMEOUT_SECONDS": "30"}):
            self.assertEqual(clamp_timeout(120), 30)


if __name__ == "__main__":
    unittest.main()
