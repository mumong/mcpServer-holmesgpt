"""Pin helm tool wire definitions and argv construction (table-driven handlers)."""

import asyncio
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "servers"))
import helm_server

TOOLS_DIGEST = "c4a27a466aeb73e36cd6ae475eee465c85904b4e634407bd2091792ce1af676f"


class HelmServerTests(unittest.TestCase):
    def call(self, name, arguments, result=None):
        result = result or {"success": True, "data": "out"}
        with patch.object(helm_server, "run_helm_command", return_value=result) as run, \
                patch.object(helm_server, "log_tool_call"):
            text = asyncio.run(helm_server.call_tool(name, arguments))[0].text
        return text, [c.args[0] for c in run.call_args_list]

    def test_tool_definitions_are_unchanged(self):
        tools = asyncio.run(helm_server.list_tools())
        serialized = json.dumps(
            [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        self.assertEqual(hashlib.sha256(serialized.encode()).hexdigest(), TOOLS_DIGEST)

    def test_mutation_argv_order_and_texts(self):
        text, argv = self.call("helm_upgrade", {
            "release_name": "r", "chart": "c", "namespace": "ns", "version": "1", "reuse_values": True,
            "reset_values": True, "wait": True, "dry_run": True,
        })
        self.assertEqual(argv, [["upgrade", "r", "c", "--namespace", "ns", "--version", "1",
                                 "--reuse-values", "--reset-values", "--wait", "--dry-run"]])
        self.assertEqual(text, "✅ 升级成功\n\nout")
        text, argv = self.call("helm_rollback", {"release_name": "r", "revision": 3},
                               {"success": False, "error": "boom"})
        self.assertEqual(argv, [["rollback", "r", "3"]])
        self.assertEqual(text, "❌ 回滚失败: boom")

    def test_read_tools_and_errors(self):
        text, argv = self.call("helm_values", {"release_name": "r", "namespace": "ns"})
        self.assertEqual(argv, [["get", "values", "-a", "r", "-n", "ns", "-o", "json"]])
        self.assertEqual(self.call("helm_notes", {"release_name": "r"})[0], "错误: 缺少 release_name 或 namespace 参数")
        self.assertEqual(self.call("helm_install", {"release_name": "r"})[0], "错误: 缺少 release_name 或 chart 参数")
        self.assertEqual(self.call("missing", {})[0], "未知工具: missing")


if __name__ == "__main__":
    unittest.main()
