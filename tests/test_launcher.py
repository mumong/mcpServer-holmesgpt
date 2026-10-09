"""Launcher characterization: mock every process and thread boundary."""

from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import start
from launch_config import build_proxy_command, build_server_environment


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(StringIO()))
        self.stack.enter_context(patch.dict(start.os.environ, {"EXISTING": "kept"}, clear=True))
        self.which = self.stack.enter_context(patch.object(start.shutil, "which", return_value="/bin/mcp-proxy"))
        self.popen = self.stack.enter_context(patch.object(start.subprocess, "Popen"))
        self.thread = self.stack.enter_context(patch.object(start.threading, "Thread"))
        self.manager = start.MCPServerManager("config/example.yaml")

    def assert_spawn(self, expected_command):
        self.assertEqual(self.popen.call_args.args, (expected_command,))
        kwargs = self.popen.call_args.kwargs
        self.assertEqual(set(kwargs), {"shell", "env", "stdout", "stderr", "text", "bufsize"})
        self.assertTrue(kwargs["shell"])
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["bufsize"], 1)
        self.assertEqual(kwargs["env"], {"EXISTING": "kept", "NODE_TLS_REJECT_UNAUTHORIZED": "0", "npm_config_cache": "/tmp/npm-cache-8123", "NPX_HOME": "/tmp/npx-8123", "CUSTOM": "7"})
        self.thread.assert_called_once_with(target=self.manager._stream_output, args=("fixture", self.popen.return_value), daemon=True)
        self.thread.return_value.start.assert_called_once_with()

    def test_npm_command_environment_and_thread(self):
        result = self.manager.start_package_server({"name": "fixture", "package": "sample@1", "port": 8123, "env": {"CUSTOM": 7}})
        self.assertEqual(result, ("fixture", self.popen.return_value))
        self.assert_spawn("mcp-proxy --port 8123 --server sse -- npx -y sample@1")

    def test_uv_and_proxy_fallback(self):
        self.which.return_value = None
        self.manager.start_package_server({"name": "fixture", "package": "sample", "type": "uv", "directory": "/fixture", "port": 8123, "env": {"CUSTOM": 7}})
        self.assert_spawn("npx -y mcp-proxy --port 8123 --server sse -- uv --directory /fixture run sample")

    def test_local_absolute_path_keeps_existing_command_and_no_cwd(self):
        self.manager.start_local_server({"name": "fixture", "path": "/fixture/server.py", "port": 8123, "env": {"CUSTOM": 7}})
        self.assert_spawn(f"mcp-proxy --port 8123 --server sse -- {sys.executable} /fixture/server.py")

    def test_local_relative_path_prefers_project_before_config(self):
        self.manager.start_local_server({"name": "fixture", "path": "servers/test_server.py", "port": 8123, "env": {"CUSTOM": 7}})
        self.assert_spawn(f"mcp-proxy --port 8123 --server sse -- {sys.executable} {Path(start.__file__).parent / 'servers/test_server.py'}")

    def test_environment_overrides_are_stringified_last(self):
        self.manager.start_package_server({"package": "sample", "port": 8123, "env": {"NODE_TLS_REJECT_UNAUTHORIZED": 1, "NPX_HOME": "/override"}})
        env = self.popen.call_args.kwargs["env"]
        self.assertEqual(env["NODE_TLS_REJECT_UNAUTHORIZED"], "1")
        self.assertEqual(env["NPX_HOME"], "/override")

    def test_invalid_configuration_does_not_spawn(self):
        for config in ({}, {"package": "sample"}, {"package": "sample", "port": 8123, "type": "uv"}):
            self.assertIsNone(self.manager.start_package_server(config))
        for config in ({}, {"path": "sample"}, {"path": "missing-fixture.py", "port": 8123}):
            self.assertIsNone(self.manager.start_local_server(config))
        self.popen.assert_not_called()

    def test_spawn_failure_preserves_none_result(self):
        self.popen.side_effect = OSError("fixture failure")
        self.assertIsNone(self.manager.start_package_server({"package": "sample", "port": 8123}))
        self.assertIsNone(self.manager.start_local_server({"path": "/fixture.py", "port": 8123}))
        self.thread.assert_not_called()

    def test_relative_path_falls_back_to_configuration_directory(self):
        expected = Path("config/fixture.py")
        with patch.object(Path, "exists", lambda path: path == expected):
            self.manager.start_local_server({"name": "fixture", "path": "fixture.py", "port": 8123, "env": {"CUSTOM": 7}})
        self.assert_spawn(f"mcp-proxy --port 8123 --server sse -- {sys.executable} {expected}")

    def test_stop_waits_then_kills_only_failed_wait(self):
        stopped, stuck = Mock(), Mock()
        stuck.wait.side_effect = start.subprocess.TimeoutExpired("fixture", 5)
        self.manager.processes = [("stopped", stopped), ("stuck", stuck)]
        self.manager.stop_all()
        for process in (stopped, stuck):
            process.terminate.assert_called_once_with()
            process.wait.assert_called_once_with(timeout=5)
        stopped.kill.assert_not_called()
        stuck.kill.assert_called_once_with()

    def test_disabled_servers_are_skipped_and_started_processes_are_tracked(self):
        package, local = Mock(), Mock()
        self.manager.config = {
            "customermcp": [{"name": "disabled", "enabled": False}, {"name": "package"}],
            "basicmcp": [{"name": "local"}, {"name": "disabled", "enabled": False}],
        }
        with patch.object(self.manager, "load_config", return_value=True), patch.object(self.manager, "start_package_server", return_value=("package", package)) as start_package, patch.object(self.manager, "start_local_server", return_value=("local", local)) as start_local, patch.object(self.manager, "_wait_for_exit") as wait:
            self.manager.start_all()
        start_package.assert_called_once_with({"name": "package"})
        start_local.assert_called_once_with({"name": "local"})
        self.assertEqual(self.manager.processes, [("package", package), ("local", local)])
        wait.assert_called_once_with()

    def test_pure_launch_configuration_does_not_mutate_input(self):
        base = {"EXISTING": "kept"}
        overrides = {"CUSTOM": None}
        result = build_server_environment(base, 8123, overrides)
        self.assertEqual(result["CUSTOM"], "None")
        self.assertEqual(base, {"EXISTING": "kept"})
        self.assertEqual(overrides, {"CUSTOM": None})
        self.assertEqual(build_proxy_command(8123, "unmodified --args", proxy_available=True), "mcp-proxy --port 8123 --server sse -- unmodified --args")

    def test_runtime_image_copies_extracted_launch_module(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY start.py launch_config.py ./", dockerfile)
        self.assertIn(
            "RUN npm_config_fetch_timeout=30000 npm_config_fetch_retries=1 "
            "npm install -g mcp-proxy@${MCP_PROXY_VERSION}", dockerfile,
        )
        self.assertNotIn("ENV npm_config_fetch", dockerfile)

    def test_version_metadata_is_after_dependency_installation(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        dependencies = dockerfile.index("RUN pip install --no-cache-dir -r requirements.txt")
        self.assertGreater(dockerfile.index("ARG VERSION=1.0.0"), dependencies)
        self.assertGreater(dockerfile.index("ENV VERSION=${VERSION}"), dependencies)
