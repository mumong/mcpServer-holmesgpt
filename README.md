# MCP Server 统一管理工具

将 MCP 工具转换为 SSE 端点，供其他服务（如 HolmesGPT）集成使用。

## 目录结构

```
mcpstander/
├── start.py             # 统一启动器
├── mcp_config.yaml      # 配置文件
├── mcp_client.py        # 测试客户端
├── servers/             # 本地自定义 MCP 工具
│   └── test_server.py   # 示例工具
└── requirements.txt
```

## 工作原理

```
┌─────────────────────────────────────────────────────────────┐
│                     mcp_config.yaml                         │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              customermcp (第三方包)                 │    │
│  │   ┌─────────────┐        ┌─────────────┐           │    │
│  │   │  npm 包     │        │   uv 包     │           │    │
│  │   │ npx -y xxx  │        │ uv run xxx  │           │    │
│  │   └──────┬──────┘        └──────┬──────┘           │    │
│  └──────────┼──────────────────────┼──────────────────┘    │
│  ┌──────────┼──────────────────────┼──────────────────┐    │
│  │          │   basicmcp (本地工具) │                 │    │
│  │          │     ┌─────────────┐   │                 │    │
│  │          │     │ python xxx  │   │                 │    │
│  │          │     └──────┬──────┘   │                 │    │
│  └──────────┼────────────┼──────────┼─────────────────┘    │
└─────────────┼────────────┼──────────┼──────────────────────┘
              │            │          │
              ▼            ▼          ▼
        ┌─────────────────────────────────────┐
        │           Supergateway              │
        │         (stdio → SSE 转换)          │
        └────────────────┬────────────────────┘
                         ▼
        ┌─────────────────────────────────────┐
        │           SSE 端点                  │
        │   http://localhost:<port>/sse       │
        └────────────────┬────────────────────┘
                         ▼
        ┌─────────────────────────────────────┐
        │      HolmesGPT / 其他服务           │
        │      (只需配置 SSE URL 即可)        │
        └─────────────────────────────────────┘
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 编辑配置文件
vim mcp_config.yaml

# 3. 启动所有服务
python start.py

# 4. 测试连接
python mcp_client.py http://localhost:8082/sse
```

## 扩展新的 MCP 工具

### 方式一：第三方 npm 包

```yaml
customermcp:
  - name: github
    type: npm                                      # npm 类型
    package: "@modelcontextprotocol/server-github" # npm 包名
    port: 8083
    enabled: true
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_xxx"
```

**常用 npm 包**:

| 包名 | 描述 |
|------|------|
| `@elastic/mcp-server-elasticsearch` | Elasticsearch |
| `@modelcontextprotocol/server-github` | GitHub |
| `@modelcontextprotocol/server-postgres` | PostgreSQL |
| `@modelcontextprotocol/server-slack` | Slack |

### 方式二：第三方 uv 包 (Python)

需要先下载/克隆项目到本地，然后配置 `directory` 指向项目路径。

```bash
# 1. 下载项目
git clone https://github.com/designcomputer/mysql_mcp_server.git /opt/mcp/mysql_mcp_server
```

```yaml
# 2. 配置
customermcp:
  - name: mysql
    type: uv                                   # uv 类型
    package: "mysql_mcp_server"                # 包名/命令
    directory: "/opt/mcp/mysql_mcp_server"     # 必填: 项目目录
    port: 8084
    enabled: true
    env:
      MYSQL_HOST: "localhost"
      MYSQL_PORT: "3306"
      MYSQL_USER: "root"
      MYSQL_PASSWORD: "password"
      MYSQL_DATABASE: "mydb"
```

**uv 包来源**: [ModelScope MCP Servers](https://modelscope.cn/mcp/servers)

### 方式三：本地自定义工具

1. **在 `servers/` 目录下创建 Python 文件**

```python
# servers/my_tools.py
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
import asyncio

server = Server("my-tools")

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="my_tool",
            description="我的自定义工具",
            inputSchema={
                "type": "object",
                "properties": {
                    "param1": {"type": "string", "description": "参数1"}
                },
                "required": ["param1"]
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "my_tool":
        result = f"处理结果: {arguments.get('param1')}"
        return [TextContent(type="text", text=result)]
    return [TextContent(type="text", text=f"未知工具: {name}")]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
```

2. **在 `mcp_config.yaml` 添加配置**

```yaml
basicmcp:
  - name: my-tools
    path: "servers/my_tools.py"
    port: 8091
    enabled: true
```

## 配置文件格式

```yaml
# 第三方包 (npm / uv)
customermcp:
  - name: <服务名>
    type: npm | uv           # npm 或 uv (Python)
    package: "<包名>"        # npm 包名 或 命令名
    directory: "<目录>"      # uv 必填: 项目目录
    port: <端口>
    enabled: true | false
    env:
      KEY: "value"

# 本地自定义工具
basicmcp:
  - name: <服务名>
    path: "servers/<文件名>.py"
    port: <端口>
    enabled: true | false
```

## 命令参考

```bash
python start.py                    # 启动所有服务
python start.py --list             # 列出配置
python start.py --config xxx.yaml  # 指定配置文件

python mcp_client.py <sse_url>     # 测试连接
python mcp_client.py <url> --call <tool>  # 调用工具
```

## 其他服务集成

启动后，其他服务只需配置 SSE URL：

```yaml
# HolmesGPT 配置示例
mcp_servers:
  elasticsearch:
    config:
      url: "http://localhost:8082/sse"
      mode: "sse"
    enabled: true
```

详见 [HOLMESGPT_CONFIG.md](HOLMESGPT_CONFIG.md)
