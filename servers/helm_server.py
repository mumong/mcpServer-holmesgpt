#!/usr/bin/env python3
"""
Helm Chart MCP Server

提供 Helm 相关的核心工具能力：列出、安装、卸载、升级、回滚、搜索。

运行方式:
    # 直接运行 (stdio 模式，用于调试)
    python helm_server.py

    # 通过 mcp-proxy 暴露为 SSE（与启动器一致）
    npx -y mcp-proxy --port 8092 --server sse -- python helm_server.py
"""

import asyncio
import json
import shutil
import subprocess
from typing import Any, Dict, List

import anyio.to_thread
from holmes_tools.arg_utils import sanitize_arguments_for_tools
from holmes_tools.mcp_logger import log_tool_call
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

_SERVER = "helm-mcp"


# 创建 MCP Server
server = Server("helm-mcp-server")


def run_helm_command(args: List[str], timeout: int = 120) -> Dict[str, Any]:
    """
    执行 helm 命令并返回结果
    
    Args:
        args: helm 命令参数列表
        timeout: 超时时间（秒），安装/升级可能较慢
    
    Returns:
        包含 success, data/error 的字典
    """
    if not shutil.which("helm"):
        return {"success": False, "error": "helm 命令未找到，请确保已安装 helm"}
    
    try:
        cmd = ["helm"] + args
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        
        if result.returncode == 0:
            return {"success": True, "data": result.stdout.strip()}
        else:
            return {"success": False, "error": result.stderr.strip() or result.stdout.strip() or "命令执行失败"}
    
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"命令执行超时 ({timeout}秒)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def parse_helm_json(output: str) -> Any:
    """解析 helm JSON 输出"""
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


def _text(text: str) -> List[TextContent]:
    return [TextContent(type="text", text=text)]


def _missing(arguments: Dict[str, Any], *keys: str):
    """必填参数缺失时返回错误文本，否则返回 None。"""
    if all(arguments.get(key) for key in keys):
        return None
    return _text(f"错误: 缺少 {' 或 '.join(keys)} 参数")


def _flags(arguments: Dict[str, Any], options) -> List[str]:
    """按声明顺序把真值参数映射为 helm 选项；(key, flag, takes_value)。"""
    args: List[str] = []
    for key, flag, takes_value in options:
        if arguments.get(key):
            args.extend([flag, arguments[key]] if takes_value else [flag])
    return args


def _json_or_text(result: Dict[str, Any], transform=None) -> List[TextContent]:
    if not result["success"]:
        return _text(f"错误: {result['error']}")
    parsed = parse_helm_json(result["data"])
    if isinstance(parsed, list):
        return _text(json.dumps(transform(parsed) if transform else parsed, indent=2, ensure_ascii=False))
    return _text(result["data"])


def _plain(result: Dict[str, Any]) -> List[TextContent]:
    if result["success"]:
        return _text(result["data"])
    return _text(f"错误: {result['error']}")


_NAMESPACE = ("namespace", "--namespace", True)
_VERSION = ("version", "--version", True)
_WAIT = ("wait", "--wait", False)
_DRY_RUN = ("dry_run", "--dry-run", False)


def _list_releases(arguments: Dict[str, Any]) -> List[TextContent]:
    args = ["list", "--output", "json"]
    if arguments.get("all_namespaces"):
        args.append("--all-namespaces")
    elif arguments.get("namespace"):
        args.extend(["--namespace", arguments["namespace"]])
    args += _flags(arguments, [("filter", "--filter", True)])

    def simplify(releases):
        # 简化输出，只保留关键信息
        keys = ("name", "namespace", "revision", "status", "chart", "app_version", "updated")
        return [{key: r.get(key) for key in keys} for r in releases]

    return _json_or_text(run_helm_command(args), simplify)


def _search_repo(arguments: Dict[str, Any]) -> List[TextContent]:
    error = _missing(arguments, "keyword")
    if error:
        return error
    args = ["search", "repo", arguments["keyword"], "--output", "json"]
    args += _flags(arguments, [("versions", "--versions", False)])
    return _json_or_text(run_helm_command(args))


def _mutation(verb: str, required, positional, options, with_set_values: bool):
    """写操作（install/uninstall/upgrade/rollback）：参数校验 → 拼装 argv → 统一结果文本。"""
    def handler(arguments: Dict[str, Any]) -> List[TextContent]:
        error = _missing(arguments, *required)
        if error:
            return error
        args = positional(arguments) + _flags(arguments, options)
        if with_set_values:
            set_values = arguments.get("set_values", {})
            for key, value in set_values.items():
                args.extend(["--set", f"{key}={value}"])
        result = run_helm_command(args)
        if result["success"]:
            return _text(f"✅ {verb}成功\n\n{result['data']}")
        return _text(f"❌ {verb}失败: {result['error']}")
    return handler


def _release_read(argv):
    """helm/core 只读工具：release_name + namespace 必填，原样返回 helm 输出。"""
    def handler(arguments: Dict[str, Any]) -> List[TextContent]:
        error = _missing(arguments, "release_name", "namespace")
        if error:
            return error
        return _plain(run_helm_command(argv(arguments["release_name"], arguments["namespace"])))
    return handler


# helm/core 只读工具（与 Holmes helm/core 一致）：name -> (description, argv)
_RELEASE_READ_TOOLS = {
    "helm_values": ("Get Helm release values as JSON for any released helm chart.",
                    lambda r, ns: ["get", "values", "-a", r, "-n", ns, "-o", "json"]),
    "helm_status": ("Check the status of a Helm release.", lambda r, ns: ["status", r, "-n", ns]),
    "helm_history": ("Get the revision history of a Helm release.", lambda r, ns: ["history", r, "-n", ns]),
    "helm_manifest": ("Fetch the generated Kubernetes manifest for a Helm release.",
                      lambda r, ns: ["get", "manifest", r, "-n", ns]),
    "helm_hooks": ("Get the hooks for a Helm release.", lambda r, ns: ["get", "hooks", r, "-n", ns]),
    "helm_chart": ("Show the chart used to create a Helm release.", lambda r, ns: ["get", "chart", r, "-n", ns]),
    "helm_notes": ("Show the notes provided by the Helm chart.", lambda r, ns: ["get", "notes", r, "-n", ns]),
}

_HANDLERS = {
    "helm_list_releases": _list_releases,
    "helm_install": _mutation(
        "安装", ("release_name", "chart"),
        lambda a: ["install", a["release_name"], a["chart"]],
        [_NAMESPACE, ("create_namespace", "--create-namespace", False), _VERSION, _WAIT, _DRY_RUN],
        with_set_values=True,
    ),
    "helm_uninstall": _mutation(
        "卸载", ("release_name",),
        lambda a: ["uninstall", a["release_name"]],
        [_NAMESPACE, ("keep_history", "--keep-history", False), _DRY_RUN],
        with_set_values=False,
    ),
    "helm_upgrade": _mutation(
        "升级", ("release_name", "chart"),
        lambda a: ["upgrade", a["release_name"], a["chart"]],
        [_NAMESPACE, _VERSION, ("reuse_values", "--reuse-values", False),
         ("reset_values", "--reset-values", False), _WAIT, _DRY_RUN],
        with_set_values=True,
    ),
    "helm_rollback": _mutation(
        "回滚", ("release_name",),
        # 如果指定了版本号，添加到参数中
        lambda a: ["rollback", a["release_name"]] + ([str(a["revision"])] if a.get("revision") else []),
        [_NAMESPACE, _WAIT, _DRY_RUN],
        with_set_values=False,
    ),
    "helm_search_repo": _search_repo,
    "helm_list": lambda arguments: _plain(run_helm_command(["list", "-A"])),
    **{name: _release_read(argv) for name, (_, argv) in _RELEASE_READ_TOOLS.items()},
}


TOOLS: List[Tool] = [
    # ============================================================
    # helm_list_releases - 列出 releases
    # ============================================================
    Tool(
        name="helm_list_releases",
        description="列出所有 Helm releases。可以按命名空间过滤，支持查看所有命名空间。",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "指定命名空间（可选，不填则使用当前上下文的命名空间）"
                },
                "all_namespaces": {
                    "type": "boolean",
                    "description": "是否列出所有命名空间的 releases（默认: false）"
                },
                "filter": {
                    "type": "string",
                    "description": "按 release 名称过滤（支持正则表达式）"
                }
            }
        }
    ),
    
    # ============================================================
    # helm_install - 安装 chart
    # ============================================================
    Tool(
        name="helm_install",
        description="安装一个 Helm chart。可以从仓库安装或从本地路径安装。",
        inputSchema={
            "type": "object",
            "properties": {
                "release_name": {
                    "type": "string",
                    "description": "Release 名称"
                },
                "chart": {
                    "type": "string",
                    "description": "Chart 名称（如 bitnami/nginx）或本地路径"
                },
                "namespace": {
                    "type": "string",
                    "description": "目标命名空间（可选）"
                },
                "create_namespace": {
                    "type": "boolean",
                    "description": "如果命名空间不存在，是否自动创建（默认: false）"
                },
                "version": {
                    "type": "string",
                    "description": "指定 chart 版本（可选）"
                },
                "set_values": {
                    "type": "object",
                    "description": "要设置的 values（key-value 对象，如 {\"replicaCount\": 2}）"
                },
                "wait": {
                    "type": "boolean",
                    "description": "是否等待所有资源就绪（默认: false）"
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "是否只模拟安装，不实际执行（默认: false）"
                }
            },
            "required": ["release_name", "chart"]
        }
    ),
    
    # ============================================================
    # helm_uninstall - 卸载 release
    # ============================================================
    Tool(
        name="helm_uninstall",
        description="卸载一个 Helm release，删除所有相关的 Kubernetes 资源。",
        inputSchema={
            "type": "object",
            "properties": {
                "release_name": {
                    "type": "string",
                    "description": "要卸载的 Release 名称"
                },
                "namespace": {
                    "type": "string",
                    "description": "Release 所在的命名空间（可选）"
                },
                "keep_history": {
                    "type": "boolean",
                    "description": "是否保留 release 历史记录（默认: false）"
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "是否只模拟卸载，不实际执行（默认: false）"
                }
            },
            "required": ["release_name"]
        }
    ),
    
    # ============================================================
    # helm_upgrade - 升级 release
    # ============================================================
    Tool(
        name="helm_upgrade",
        description="升级一个已安装的 Helm release 到新版本或新配置。",
        inputSchema={
            "type": "object",
            "properties": {
                "release_name": {
                    "type": "string",
                    "description": "Release 名称"
                },
                "chart": {
                    "type": "string",
                    "description": "Chart 名称（如 bitnami/nginx）或本地路径"
                },
                "namespace": {
                    "type": "string",
                    "description": "Release 所在的命名空间（可选）"
                },
                "version": {
                    "type": "string",
                    "description": "指定 chart 版本（可选）"
                },
                "set_values": {
                    "type": "object",
                    "description": "要设置的 values（key-value 对象）"
                },
                "reuse_values": {
                    "type": "boolean",
                    "description": "是否复用上次安装的 values（默认: false）"
                },
                "reset_values": {
                    "type": "boolean",
                    "description": "是否重置为 chart 默认 values（默认: false）"
                },
                "wait": {
                    "type": "boolean",
                    "description": "是否等待所有资源就绪（默认: false）"
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "是否只模拟升级，不实际执行（默认: false）"
                }
            },
            "required": ["release_name", "chart"]
        }
    ),
    
    # ============================================================
    # helm_rollback - 回滚 release
    # ============================================================
    Tool(
        name="helm_rollback",
        description="将 Helm release 回滚到之前的版本。",
        inputSchema={
            "type": "object",
            "properties": {
                "release_name": {
                    "type": "string",
                    "description": "Release 名称"
                },
                "revision": {
                    "type": "integer",
                    "description": "要回滚到的版本号（可选，不填则回滚到上一个版本）"
                },
                "namespace": {
                    "type": "string",
                    "description": "Release 所在的命名空间（可选）"
                },
                "wait": {
                    "type": "boolean",
                    "description": "是否等待所有资源就绪（默认: false）"
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "是否只模拟回滚，不实际执行（默认: false）"
                }
            },
            "required": ["release_name"]
        }
    ),
    
    # ============================================================
    # helm_search_repo - 搜索仓库获取 chart 信息
    # ============================================================
    Tool(
        name="helm_search_repo",
        description="在已添加的仓库中搜索 chart，获取 chart 的版本、描述等信息。",
        inputSchema={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词（chart 名称或部分名称）"
                },
                "versions": {
                    "type": "boolean",
                    "description": "是否显示所有可用版本（默认只显示最新版本）"
                }
            },
            "required": ["keyword"]
        }
    ),
    # ============================================================
    # helm/core 只读工具（与 Holmes helm/core 一致）
    # ============================================================
    Tool(
        name="helm_list",
        description="List all current Helm releases (all namespaces). Use to get all helm releases.",
        inputSchema={"type": "object", "properties": {}}
    ),
] + [
    Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": {
                "release_name": {"type": "string", "description": "Release 名称"},
                "namespace": {"type": "string", "description": "命名空间"}
            },
            "required": ["release_name", "namespace"]
        }
    )
    for name, (description, _) in _RELEASE_READ_TOOLS.items()
]


@server.list_tools()
async def list_tools():
    """定义可用的 Helm 工具"""
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """处理工具调用（helm 子进程在工作线程中执行，避免阻塞事件循环）"""
    log_tool_call(_SERVER, name, arguments)
    return await anyio.to_thread.run_sync(_call_tool_sync, name, arguments)


def _call_tool_sync(name: str, arguments: dict):
    # MCP 入口层清洗：仅保留标量，避免误传对象导致命令注入/语法错误（与 Holmes 内置工具改造一致）
    arguments = sanitize_arguments_for_tools(arguments or {})
    handler = _HANDLERS.get(name)
    if handler is None:
        return _text(f"未知工具: {name}")
    return handler(arguments)


async def main():
    """运行服务器"""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
