#!/usr/bin/env python3
"""
自定义 MCP Server 示例

这是一个使用 mcp 库编写的自定义 MCP 工具服务器模板。
可以基于此创建自己的 MCP 工具。

运行方式:
    # 直接运行 (stdio 模式，用于调试)
    python test_server.py

    # 通过 mcp-proxy 暴露为 SSE（与启动器一致）
    npx -y mcp-proxy --port 8091 --server sse -- python test_server.py
"""

import asyncio
import json
from datetime import datetime
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


# 创建 MCP Server
server = Server("test-mcp-server")


@server.list_tools()
async def list_tools():
    """定义可用的工具"""
    return [
        Tool(
            name="get_time",
            description="获取当前时间",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {
                        "type": "string",
                        "description": "时间格式 (默认: %Y-%m-%d %H:%M:%S)"
                    }
                }
            }
        ),
        Tool(
            name="echo",
            description="回显输入的消息",
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "要回显的消息"
                    }
                },
                "required": ["message"]
            }
        ),
        Tool(
            name="test_tool",
            description="只有当用户需要测试的时候才运行这个测试工具",
            inputSchema={
                "type": "object",
                "properties": {
                    "test_param": {
                        "type": "string",
                        "description": "测试参数"
                    }
                },
                "required": ["test_param"]
            }
        ),  
        Tool(
            name="calculate",
            description="简单计算器",
            inputSchema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式 (如: 1+2*3)"
                    }
                },
                "required": ["expression"]
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """处理工具调用"""
    
    if name == "get_time":
        fmt = arguments.get("format", "%Y-%m-%d %H:%M:%S")
        result = datetime.now().strftime(fmt)
        return [TextContent(type="text", text=result)]
    
    elif name == "echo":
        message = arguments.get("message", "")
        return [TextContent(type="text", text=f"Echo: {message}")]
    
    elif name == "test_tool":
        user_input = arguments.get("test_param", "")
        return [TextContent(type="text", text=f"这是测试: {user_input} 运行成功")]
    
    elif name == "calculate":
        expr = arguments.get("expression", "0")
        try:
            # 简单安全检查
            allowed = set("0123456789+-*/.() ")
            if all(c in allowed for c in expr):
                result = eval(expr)
                return [TextContent(type="text", text=str(result))]
            else:
                return [TextContent(type="text", text="错误: 不允许的字符")]
        except Exception as e:
            return [TextContent(type="text", text=f"错误: {e}")]
    
    return [TextContent(type="text", text=f"未知工具: {name}")]


async def main():
    """运行服务器"""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

