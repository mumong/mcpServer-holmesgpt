"""Offline contracts for bash and temporary-Pod command execution."""

import asyncio
import hashlib
import json
import shlex
import subprocess
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "servers"))
import bash_server


class BashServerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.run = self.stack.enter_context(patch.object(bash_server.subprocess, "run"))
        self.run.return_value = Mock(returncode=0, stdout="fixture output\n", stderr="")
        self.stack.enter_context(patch.object(bash_server.shutil, "which", return_value="/usr/bin/kubectl"))
        self.stack.enter_context(patch.object(bash_server, "log_tool_call"))
        self.log = self.stack.enter_context(patch.object(bash_server, "log_command"))
        self.arguments = {"namespace": "fixture", "pod_name": "probe", "image": "busybox:1"}
        self.base_command = [
            "kubectl",
            "run",
            "probe",
            "--image=busybox:1",
            "--namespace=fixture",
            "--rm",
            "--attach",
            "--restart=Never",
        ]

    def invoke(self, arguments=None, name="kubectl_run_image"):
        result = asyncio.run(bash_server.call_tool(name, self.arguments if arguments is None else arguments))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].type, "text")
        return json.loads(result[0].text)

    def command_tokens(self):
        command = self.run.call_args.args[0]
        return shlex.split(command) if isinstance(command, str) else command

    def test_default_and_empty_commands_keep_image_entrypoint(self):
        for command in (None, []):
            with self.subTest(command=command):
                result = self.invoke({**self.arguments, "command": command})
                self.assertEqual(self.command_tokens(), self.base_command)
                self.assertEqual(self.run.call_args.kwargs["timeout"], 120)
                self.assertEqual(
                    result, {"success": True, "stdout": "fixture output\n", "stderr": "", "returncode": 0}
                )

    def test_tools_list_names_descriptions_and_schemas_are_unchanged(self):
        tools = asyncio.run(bash_server.list_tools())
        serialized = json.dumps(
            [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(
            hashlib.sha256(serialized.encode()).hexdigest(),
            "4d46abc8428b4bf3f9a76deedba3dc94b19fab9bdfe630a004d0a6b57d5ec8d7",
        )

    def test_container_arguments_are_literal_argv_not_host_shell_syntax(self):
        command = [
            "sh",
            "-c",
            "printf '%s' \"hello world\"; echo container-only",
            "argument with spaces",
            "quote'and\"quote",
            "; echo literal",
            "$(printf literal)",
            "`printf literal`",
            "",
            "a\nb",
            "$VARIABLE",
        ]
        self.invoke({**self.arguments, "command": command})
        self.run.assert_called_once_with(
            self.base_command + ["--", *command],
            shell=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(self.log.call_args.args, (" ".join(self.base_command + ["--", *command]),))

    def test_empty_command_also_bypasses_host_shell(self):
        self.invoke()
        self.run.assert_called_once_with(
            self.base_command,
            shell=False,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_simple_command_list_and_explicit_timeout(self):
        self.invoke({**self.arguments, "command": ["echo", "hello"], "timeout_seconds": 19})
        self.assertEqual(self.command_tokens(), self.base_command + ["--", "echo", "hello"])
        self.assertEqual(self.run.call_args.kwargs["timeout"], 19)
        self.assertTrue(self.run.call_args.kwargs["capture_output"])
        self.assertTrue(self.run.call_args.kwargs["text"])
        self.assertEqual(self.log.call_args.args, (" ".join(self.base_command + ["--", "echo", "hello"]),))

    def test_nonzero_exit_preserves_output_and_returncode(self):
        self.run.return_value = Mock(returncode=7, stdout="partial", stderr="fixture error")
        self.assertEqual(
            self.invoke(), {"success": False, "stdout": "partial", "stderr": "fixture error", "returncode": 7}
        )

    def test_timeout_preserves_return_and_log_contract(self):
        self.run.side_effect = subprocess.TimeoutExpired("fixture", 120)
        self.assertEqual(
            self.invoke(),
            {"success": False, "stdout": "", "stderr": "命令执行超时 (120秒)", "returncode": -1},
        )
        self.assertEqual(self.log.call_args.kwargs["returncode"], -1)
        self.assertEqual(self.log.call_args.kwargs["stderr"], "命令执行超时 (120秒)")

    def test_process_error_preserves_return_structure(self):
        error = OSError("fixture unavailable")
        self.run.side_effect = error
        self.assertEqual(
            self.invoke(), {"success": False, "stdout": "", "stderr": "fixture unavailable", "returncode": -1}
        )
        self.assertIs(self.log.call_args.kwargs["error"], error)

    def test_non_string_command_argument_is_rejected_before_execution(self):
        result = self.invoke({**self.arguments, "command": ["echo", 42]})
        self.assertEqual(
            result, {"success": False, "error": "命令列表中的元素必须为字符串", "stdout": "", "stderr": ""}
        )
        self.run.assert_not_called()

    def test_run_bash_command_keeps_explicit_shell_execution(self):
        with patch.object(bash_server, "make_command_safe", return_value=(True, "")):
            result = self.invoke({"command": "printf hello | wc -c"}, name="run_bash_command")
        self.run.assert_called_once_with(
            "printf hello | wc -c",
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertTrue(result["success"])


class BlockedCommandNameTests(unittest.TestCase):
    """BASH_BLOCKED_COMMANDS names are blocked wherever they would run: as the leading
    command, after ``kubectl exec ... --``, inside ``sh -c "..."``, by full path, and in
    kubectl_run_image's command list. Boss's test: "通过对话让它 rm 一个文件"."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(bash_server.os.environ, {"BASH_BLOCKED_COMMANDS": "rm,dd,mkfs*"}))
        self.run = self.stack.enter_context(patch.object(bash_server.subprocess, "run"))
        self.run.return_value = Mock(returncode=0, stdout="", stderr="")
        self.stack.enter_context(patch.object(bash_server.shutil, "which", return_value="/usr/bin/kubectl"))
        self.stack.enter_context(patch.object(bash_server, "log_tool_call"))
        self.stack.enter_context(patch.object(bash_server, "log_command"))

    def test_blocked_names_are_caught_in_every_executing_position(self):
        for command in (
            "rm /tmp/claude-guard-test.txt",
            "kubectl exec -n aiops pod/x -c app -- rm /tmp/claude-guard-test.txt",
            "kubectl exec pod/x -- sh -c 'ls /tmp; rm -f /tmp/a'",
            "/bin/rm -f /tmp/a",
            "cd /tmp && rm a",
            'kubectl exec pod/x -- bash -c "dd if=/dev/zero of=/dev/sda"',
            "kubectl exec pod/x -- mkfs.ext4 /dev/sdb",
            "kubectl exec pod/x -- sh -c 'cat /tmp/a;rm /tmp/a'",
        ):
            ok, err = bash_server.make_command_safe(command)
            self.assertFalse(ok, command)
            self.assertIn("拒绝", err)

    def test_ordinary_commands_still_pass(self):
        for command in (
            "ls -l /tmp",
            "kubectl exec -n aiops pod/x -- cat /tmp/claude-guard-test.txt",
            "kubectl get pods -n aiops -o wide",
            "echo removed",
            "kubectl rollout restart deployment/api -n demo",
            "kubectl exec pod/x -n demo -- sh -c 'ls /tmp/x; cat /tmp/y'",  # 7.0.51 recursed forever here
        ):
            self.assertEqual(bash_server.make_command_safe(command), (True, ""), command)

    def test_run_image_command_list_is_checked_before_any_kubectl_call(self):
        result = asyncio.run(
            bash_server.call_tool(
                "kubectl_run_image",
                {
                    "namespace": "demo",
                    "pod_name": "probe",
                    "image": "busybox:1",
                    "command": ["sh", "-c", "rm -rf /data"],
                },
            )
        )
        payload = json.loads(result[0].text)
        self.assertFalse(payload["success"])
        self.assertIn("rm", payload["error"])
        self.run.assert_not_called()
