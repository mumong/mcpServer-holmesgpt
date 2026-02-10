#!/usr/bin/env python3
"""
Bash / Kubectl MCP Server

提供在受控环境下执行 shell 命令和 kubectl run 的能力。
- run_bash_command: 执行单条 bash 命令，带安全校验。
- kubectl_run_image: 在指定 namespace 用镜像跑临时 pod 并执行命令。

运行方式:
    # 直接运行 (stdio 模式，用于调试)
    python bash_server.py

    # 由启动器通过 mcp-proxy 暴露为 SSE（见 deploy/configmap.yaml basicmcp）
    npx -y mcp-proxy --port 8094 --server sse -- python bash_server.py
"""

import asyncio
import json
import os
import re
import subprocess
import shutil
from typing import Tuple

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


# 内置危险命令/模式（命中则拒绝）。无需配置即生效。
# 若需“额外禁止的敏感命令”，只需配置 BASH_BLOCKED_COMMANDS="rm,dd,mkfs" 这种逗号分隔即可。
_DEFAULT_UNSAFE_PATTERNS = [
    # rm 删除根或系统路径
    r"\brm\s+(-rf?|-\s*rf?)\s+/",
    r"\brm\s+(-rf?|-\s*rf?)\s+/\s",
    # 磁盘/分区/块设备
    r"\bmkfs\.?\w*\s",
    r"\bdd\s+.*(if=.*of=|of=.*if=)",
    r"\bdd\s+.*of=\s*\/dev\/",
    r">\s*/dev/(sd|nvme|hd|loop)\w*",
    r"\bfdisk\s",
    r"\bparted\s+.*\b(rm|mklabel)\b",
    r"\bwipefs\s+.*-a",
    r"\bmkswap\s+.*/dev/",
    r"\bsfdisk\s+.*\/dev\/",
    r"\bblockdev\s+.*(--flushbufs|--rereadpt)\s",
    # fork 炸弹
    r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}",  # :(){ :|:& };:
    # 提权/权限
    r"\bchmod\s+[-+]?s\b",
    r"\bchmod\s+[0-7]{3,4}\s+\/",
    r"\bchown\s+.*\b(root|0)\s",
    # 管道下载执行（远程代码）
    r"\bcurl\s+[^|]*\|\s*(bash|sh)\s*$",
    r"\bwget\s+[^|]*\|\s*(bash|sh)\s*$",
    # 覆盖设备/关键路径
    r"\bshred\s+.*-f.*/dev/",
    r"\bshred\s+.*\/",
    # 系统控制
    r"\bsystemctl\s+(stop|restart|reboot|halt|poweroff)\s",
    r"\binit\s+[06]",  # reboot/halt
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\bshutdown\s+-(r|h|P)",
    # 防火墙/网络
    r"\biptables\s+(-F|--flush)\b",
    r"\bip6tables\s+(-F|--flush)\b",
    # 挂载危险
    r"\bmount\s+.*-o\s+.*remount.*\/\s",
    r"\bumount\s+\/",
    # 用户/密码/权限
    r"\bpasswd\s+.*(root|-u\s+0)\b",
    r"\busermod\s+.*-o\s+-u\s+0\b",
    r"\buserdel\s+-f\s+root\b",
    # 覆盖系统关键文件
    r">\s*/etc/(shadow|passwd|sudoers)",
    r"\b:>\s*/",
    # 内核/模块
    r"\bsysctl\s+-w\s+kernel\.(core_pattern|sysrq)",
    r"\bmodprobe\s+-r\s+(ext4|xfs|nfs)\b",
    # SELinux/AppArmor 关闭
    r"\bsetenforce\s+0\b",
    r"\bapparmor_parser\s+-R\b",
]


def _get_simple_blocked_commands() -> list:
    """
    从环境变量 BASH_BLOCKED_COMMANDS 读取简单黑名单：逗号分隔的命令名。
    例如: BASH_BLOCKED_COMMANDS="rm,dd,mkfs,fdisk"
    会禁止以这些命令开头的执行（含管道/分号后的子命令）。
    """
    raw = os.environ.get("BASH_BLOCKED_COMMANDS", "").strip()
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]


def _get_blocked_patterns() -> list:
    """
    黑名单正则列表。
    - 默认使用内置 _DEFAULT_UNSAFE_PATTERNS（覆盖 rm/dd/磁盘/重启等危险操作）。
    - 若设置了 BASH_BLOCKED_COMMANDS（逗号分隔），会额外禁止以这些命令开头的执行，例如：
      env: BASH_BLOCKED_COMMANDS: "rm,dd,mkfs,fdisk"  即可，无需写正则。
    - 高级：BASH_BLOCKED_PATTERNS 为 JSON 数组时可完全替换内置列表（一般不推荐）。
    """
    base = _DEFAULT_UNSAFE_PATTERNS
    raw_pat = os.environ.get("BASH_BLOCKED_PATTERNS", "").strip()
    if raw_pat:
        try:
            arr = json.loads(raw_pat)
            if isinstance(arr, list) and all(isinstance(x, str) for x in arr):
                base = arr
        except (json.JSONDecodeError, TypeError):
            pass

    # 简单配置：逗号分隔的命令名，自动转成“行首或 |;& 后出现该命令”即拒绝
    for cmd in _get_simple_blocked_commands():
        base = base + [r"(^|[|;&])\s*" + re.escape(cmd) + r"\b"]
    return base


NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
IMAGE_RE = re.compile(r"^[\w./\-:]+$")


def make_command_safe(command_str: str) -> Tuple[bool, str]:
    """
    简单安全校验。通过返回 (True, "")，不通过返回 (False, "原因")。
    若环境变量 BASH_TOOL_UNSAFE_ALLOW_ALL=1 则放行。
    """
    if os.environ.get("BASH_TOOL_UNSAFE_ALLOW_ALL") == "1":
        return True, ""
    cmd = (command_str or "").strip()
    if not cmd:
        return False, "命令为空"
    patterns = _get_blocked_patterns()
    for pat in patterns:
        if re.search(pat, cmd, re.IGNORECASE):
            return False, "命令包含不允许的模式，拒绝执行"
    return True, ""


def execute_bash_command(cmd: str, timeout: int = 60) -> dict:
    """执行 bash 命令，返回 { "success", "stdout", "stderr", "returncode" }"""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"命令执行超时 ({timeout}秒)",
            "returncode": -1,
        }
    except Exception as e:
        return {
            "success": False,
            "stdout": "",
            "stderr": str(e),
            "returncode": -1,
        }


def validate_image_and_commands(image: str, command_list: list) -> Tuple[bool, str]:
    """校验镜像名和命令列表。通过 (True, "")，不通过 (False, 原因)。"""
    if not image or not IMAGE_RE.match(image.strip()):
        return False, "镜像名不合法"
    for c in (command_list or []):
        if not isinstance(c, str):
            return False, "命令列表中的元素必须为字符串"
    return True, ""


server = Server("bash-mcp-server")


@server.list_tools()
async def list_tools():
    """定义 Bash / Kubectl 工具"""
    return [
        Tool(
            name="run_bash_command",
            description="在受控环境下执行一条 bash 命令。会做安全校验，危险命令会被拒绝。适用于执行只读或低风险命令（如 cat、grep、ls、date）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的单条 bash 命令",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "超时秒数，默认 60",
                        "default": 60,
                    },
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="kubectl_run_image",
            description="在指定 Kubernetes 命名空间中，使用给定镜像创建临时 Pod 并执行命令，执行完后自动删除（--rm --attach）。适用于跑一次性任务或调试镜像。",
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "目标命名空间",
                    },
                    "pod_name": {
                        "type": "string",
                        "description": "Pod 名称（需符合 K8s 命名规范）",
                    },
                    "image": {
                        "type": "string",
                        "description": "容器镜像，如 nginx:alpine 或 myreg.io/myimg:v1",
                    },
                    "command": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要执行的命令列表，如 [\"sh\", \"-c\", \"echo hello\"]；不填则用镜像默认 entrypoint",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "超时秒数，默认 120",
                        "default": 120,
                    },
                },
                "required": ["namespace", "pod_name", "image"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """处理工具调用"""
    if name == "run_bash_command":
        command = arguments.get("command", "").strip()
        timeout = int(arguments.get("timeout_seconds") or 60)
        ok, err = make_command_safe(command)
        if not ok:
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": err,
                "stdout": "",
                "stderr": "",
            }, ensure_ascii=False))]
        out = execute_bash_command(command, timeout=timeout)
        return [TextContent(type="text", text=json.dumps(out, ensure_ascii=False))]

    if name == "kubectl_run_image":
        namespace = (arguments.get("namespace") or "").strip()
        pod_name = (arguments.get("pod_name") or "").strip()
        image = (arguments.get("image") or "").strip()
        command_list = arguments.get("command") or []
        timeout = int(arguments.get("timeout_seconds") or 120)

        if not namespace or not NAMESPACE_RE.match(namespace):
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": "namespace 不合法或为空",
                "stdout": "",
                "stderr": "",
            }, ensure_ascii=False))]
        if not pod_name or not NAMESPACE_RE.match(pod_name):
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": "pod_name 不合法或为空",
                "stdout": "",
                "stderr": "",
            }, ensure_ascii=False))]
        ok, err = validate_image_and_commands(image, command_list)
        if not ok:
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": err,
                "stdout": "",
                "stderr": "",
            }, ensure_ascii=False))]

        if not shutil.which("kubectl"):
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": "kubectl 未找到",
                "stdout": "",
                "stderr": "",
            }, ensure_ascii=False))]

        cmd_parts = [
            "kubectl", "run", pod_name,
            f"--image={image}",
            f"--namespace={namespace}",
            "--rm",
            "--attach",
            "--restart=Never",
        ]
        if command_list:
            cmd_parts.extend(["--", *command_list])
        full_cmd = " ".join(cmd_parts)
        out = execute_bash_command(full_cmd, timeout=timeout)
        return [TextContent(type="text", text=json.dumps(out, ensure_ascii=False))]

    return [TextContent(type="text", text=json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
