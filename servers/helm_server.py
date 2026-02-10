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
import subprocess
import shutil
from typing import List, Dict, Any
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


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


@server.list_tools()
async def list_tools():
    """定义可用的 Helm 工具"""
    return [
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
        Tool(
            name="helm_values",
            description="Get Helm release values as JSON for any released helm chart.",
            inputSchema={
                "type": "object",
                "properties": {
                    "release_name": {"type": "string", "description": "Release 名称"},
                    "namespace": {"type": "string", "description": "命名空间"}
                },
                "required": ["release_name", "namespace"]
            }
        ),
        Tool(
            name="helm_status",
            description="Check the status of a Helm release.",
            inputSchema={
                "type": "object",
                "properties": {
                    "release_name": {"type": "string", "description": "Release 名称"},
                    "namespace": {"type": "string", "description": "命名空间"}
                },
                "required": ["release_name", "namespace"]
            }
        ),
        Tool(
            name="helm_history",
            description="Get the revision history of a Helm release.",
            inputSchema={
                "type": "object",
                "properties": {
                    "release_name": {"type": "string", "description": "Release 名称"},
                    "namespace": {"type": "string", "description": "命名空间"}
                },
                "required": ["release_name", "namespace"]
            }
        ),
        Tool(
            name="helm_manifest",
            description="Fetch the generated Kubernetes manifest for a Helm release.",
            inputSchema={
                "type": "object",
                "properties": {
                    "release_name": {"type": "string", "description": "Release 名称"},
                    "namespace": {"type": "string", "description": "命名空间"}
                },
                "required": ["release_name", "namespace"]
            }
        ),
        Tool(
            name="helm_hooks",
            description="Get the hooks for a Helm release.",
            inputSchema={
                "type": "object",
                "properties": {
                    "release_name": {"type": "string", "description": "Release 名称"},
                    "namespace": {"type": "string", "description": "命名空间"}
                },
                "required": ["release_name", "namespace"]
            }
        ),
        Tool(
            name="helm_chart",
            description="Show the chart used to create a Helm release.",
            inputSchema={
                "type": "object",
                "properties": {
                    "release_name": {"type": "string", "description": "Release 名称"},
                    "namespace": {"type": "string", "description": "命名空间"}
                },
                "required": ["release_name", "namespace"]
            }
        ),
        Tool(
            name="helm_notes",
            description="Show the notes provided by the Helm chart.",
            inputSchema={
                "type": "object",
                "properties": {
                    "release_name": {"type": "string", "description": "Release 名称"},
                    "namespace": {"type": "string", "description": "命名空间"}
                },
                "required": ["release_name", "namespace"]
            }
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """处理工具调用"""
    
    # ============================================================
    # helm_list_releases - 列出 releases
    # ============================================================
    if name == "helm_list_releases":
        args = ["list", "--output", "json"]
        
        if arguments.get("all_namespaces"):
            args.append("--all-namespaces")
        elif arguments.get("namespace"):
            args.extend(["--namespace", arguments["namespace"]])
        
        if arguments.get("filter"):
            args.extend(["--filter", arguments["filter"]])
        
        result = run_helm_command(args)
        
        if result["success"]:
            releases = parse_helm_json(result["data"])
            if isinstance(releases, list):
                # 简化输出，只保留关键信息
                simplified = []
                for r in releases:
                    simplified.append({
                        "name": r.get("name"),
                        "namespace": r.get("namespace"),
                        "revision": r.get("revision"),
                        "status": r.get("status"),
                        "chart": r.get("chart"),
                        "app_version": r.get("app_version"),
                        "updated": r.get("updated")
                    })
                return [TextContent(type="text", text=json.dumps(simplified, indent=2, ensure_ascii=False))]
            return [TextContent(type="text", text=result["data"])]
        return [TextContent(type="text", text=f"错误: {result['error']}")]
    
    # ============================================================
    # helm_install - 安装 chart
    # ============================================================
    elif name == "helm_install":
        release_name = arguments.get("release_name")
        chart = arguments.get("chart")
        
        if not release_name or not chart:
            return [TextContent(type="text", text="错误: 缺少 release_name 或 chart 参数")]
        
        args = ["install", release_name, chart]
        
        if arguments.get("namespace"):
            args.extend(["--namespace", arguments["namespace"]])
        
        if arguments.get("create_namespace"):
            args.append("--create-namespace")
        
        if arguments.get("version"):
            args.extend(["--version", arguments["version"]])
        
        if arguments.get("wait"):
            args.append("--wait")
        
        if arguments.get("dry_run"):
            args.append("--dry-run")
        
        # 处理 set values
        set_values = arguments.get("set_values", {})
        for key, value in set_values.items():
            args.extend(["--set", f"{key}={value}"])
        
        result = run_helm_command(args)
        
        if result["success"]:
            return [TextContent(type="text", text=f"✅ 安装成功\n\n{result['data']}")]
        return [TextContent(type="text", text=f"❌ 安装失败: {result['error']}")]
    
    # ============================================================
    # helm_uninstall - 卸载 release
    # ============================================================
    elif name == "helm_uninstall":
        release_name = arguments.get("release_name")
        
        if not release_name:
            return [TextContent(type="text", text="错误: 缺少 release_name 参数")]
        
        args = ["uninstall", release_name]
        
        if arguments.get("namespace"):
            args.extend(["--namespace", arguments["namespace"]])
        
        if arguments.get("keep_history"):
            args.append("--keep-history")
        
        if arguments.get("dry_run"):
            args.append("--dry-run")
        
        result = run_helm_command(args)
        
        if result["success"]:
            return [TextContent(type="text", text=f"✅ 卸载成功\n\n{result['data']}")]
        return [TextContent(type="text", text=f"❌ 卸载失败: {result['error']}")]
    
    # ============================================================
    # helm_upgrade - 升级 release
    # ============================================================
    elif name == "helm_upgrade":
        release_name = arguments.get("release_name")
        chart = arguments.get("chart")
        
        if not release_name or not chart:
            return [TextContent(type="text", text="错误: 缺少 release_name 或 chart 参数")]
        
        args = ["upgrade", release_name, chart]
        
        if arguments.get("namespace"):
            args.extend(["--namespace", arguments["namespace"]])
        
        if arguments.get("version"):
            args.extend(["--version", arguments["version"]])
        
        if arguments.get("reuse_values"):
            args.append("--reuse-values")
        
        if arguments.get("reset_values"):
            args.append("--reset-values")
        
        if arguments.get("wait"):
            args.append("--wait")
        
        if arguments.get("dry_run"):
            args.append("--dry-run")
        
        # 处理 set values
        set_values = arguments.get("set_values", {})
        for key, value in set_values.items():
            args.extend(["--set", f"{key}={value}"])
        
        result = run_helm_command(args)
        
        if result["success"]:
            return [TextContent(type="text", text=f"✅ 升级成功\n\n{result['data']}")]
        return [TextContent(type="text", text=f"❌ 升级失败: {result['error']}")]
    
    # ============================================================
    # helm_rollback - 回滚 release
    # ============================================================
    elif name == "helm_rollback":
        release_name = arguments.get("release_name")
        
        if not release_name:
            return [TextContent(type="text", text="错误: 缺少 release_name 参数")]
        
        args = ["rollback", release_name]
        
        # 如果指定了版本号，添加到参数中
        if arguments.get("revision"):
            args.append(str(arguments["revision"]))
        
        if arguments.get("namespace"):
            args.extend(["--namespace", arguments["namespace"]])
        
        if arguments.get("wait"):
            args.append("--wait")
        
        if arguments.get("dry_run"):
            args.append("--dry-run")
        
        result = run_helm_command(args)
        
        if result["success"]:
            return [TextContent(type="text", text=f"✅ 回滚成功\n\n{result['data']}")]
        return [TextContent(type="text", text=f"❌ 回滚失败: {result['error']}")]
    
    # ============================================================
    # helm_search_repo - 搜索仓库获取 chart 信息
    # ============================================================
    elif name == "helm_search_repo":
        keyword = arguments.get("keyword")
        
        if not keyword:
            return [TextContent(type="text", text="错误: 缺少 keyword 参数")]
        
        args = ["search", "repo", keyword, "--output", "json"]
        
        if arguments.get("versions"):
            args.append("--versions")
        
        result = run_helm_command(args)
        
        if result["success"]:
            charts = parse_helm_json(result["data"])
            if isinstance(charts, list):
                return [TextContent(type="text", text=json.dumps(charts, indent=2, ensure_ascii=False))]
            return [TextContent(type="text", text=result["data"])]
        return [TextContent(type="text", text=f"错误: {result['error']}")]
    
    # ============================================================
    # helm/core 只读工具
    # ============================================================
    elif name == "helm_list":
        result = run_helm_command(["list", "-A"])
        if result["success"]:
            return [TextContent(type="text", text=result["data"])]
        return [TextContent(type="text", text=f"错误: {result['error']}")]
    
    elif name == "helm_values":
        release_name = arguments.get("release_name")
        namespace = arguments.get("namespace")
        if not release_name or not namespace:
            return [TextContent(type="text", text="错误: 缺少 release_name 或 namespace 参数")]
        args = ["get", "values", "-a", release_name, "-n", namespace, "-o", "json"]
        result = run_helm_command(args)
        if result["success"]:
            return [TextContent(type="text", text=result["data"])]
        return [TextContent(type="text", text=f"错误: {result['error']}")]
    
    elif name == "helm_status":
        release_name = arguments.get("release_name")
        namespace = arguments.get("namespace")
        if not release_name or not namespace:
            return [TextContent(type="text", text="错误: 缺少 release_name 或 namespace 参数")]
        args = ["status", release_name, "-n", namespace]
        result = run_helm_command(args)
        if result["success"]:
            return [TextContent(type="text", text=result["data"])]
        return [TextContent(type="text", text=f"错误: {result['error']}")]
    
    elif name == "helm_history":
        release_name = arguments.get("release_name")
        namespace = arguments.get("namespace")
        if not release_name or not namespace:
            return [TextContent(type="text", text="错误: 缺少 release_name 或 namespace 参数")]
        args = ["history", release_name, "-n", namespace]
        result = run_helm_command(args)
        if result["success"]:
            return [TextContent(type="text", text=result["data"])]
        return [TextContent(type="text", text=f"错误: {result['error']}")]
    
    elif name == "helm_manifest":
        release_name = arguments.get("release_name")
        namespace = arguments.get("namespace")
        if not release_name or not namespace:
            return [TextContent(type="text", text="错误: 缺少 release_name 或 namespace 参数")]
        args = ["get", "manifest", release_name, "-n", namespace]
        result = run_helm_command(args)
        if result["success"]:
            return [TextContent(type="text", text=result["data"])]
        return [TextContent(type="text", text=f"错误: {result['error']}")]
    
    elif name == "helm_hooks":
        release_name = arguments.get("release_name")
        namespace = arguments.get("namespace")
        if not release_name or not namespace:
            return [TextContent(type="text", text="错误: 缺少 release_name 或 namespace 参数")]
        args = ["get", "hooks", release_name, "-n", namespace]
        result = run_helm_command(args)
        if result["success"]:
            return [TextContent(type="text", text=result["data"])]
        return [TextContent(type="text", text=f"错误: {result['error']}")]
    
    elif name == "helm_chart":
        release_name = arguments.get("release_name")
        namespace = arguments.get("namespace")
        if not release_name or not namespace:
            return [TextContent(type="text", text="错误: 缺少 release_name 或 namespace 参数")]
        args = ["get", "chart", release_name, "-n", namespace]
        result = run_helm_command(args)
        if result["success"]:
            return [TextContent(type="text", text=result["data"])]
        return [TextContent(type="text", text=f"错误: {result['error']}")]
    
    elif name == "helm_notes":
        release_name = arguments.get("release_name")
        namespace = arguments.get("namespace")
        if not release_name or not namespace:
            return [TextContent(type="text", text="错误: 缺少 release_name 或 namespace 参数")]
        args = ["get", "notes", release_name, "-n", namespace]
        result = run_helm_command(args)
        if result["success"]:
            return [TextContent(type="text", text=result["data"])]
        return [TextContent(type="text", text=f"错误: {result['error']}")]
    
    # ============================================================
    # 未知工具
    # ============================================================
    return [TextContent(type="text", text=f"未知工具: {name}")]


async def main():
    """运行服务器"""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
