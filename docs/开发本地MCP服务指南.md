# 开发本地 MCP 服务指南

本文档说明如何在本项目中**开发并接入一个本地 MCP 服务**（如 `test_server`、`helm_server`、`bash_command`），从编写代码到部署对外提供 SSE 端点。

---

## 一、本地 MCP 服务是什么

- **位置**：代码放在 `servers/` 目录下，使用 Python + `mcp` 库。
- **协议**：以 **stdio** 方式运行（标准输入/输出），由启动器通过 **mcp-proxy** 转为 HTTP SSE，对外提供 `/sse` 端点。
- **参考实现**：`servers/test_server.py`、`servers/helm_server.py`、`servers/bash_server.py`。

---

## 二、开发步骤概览

1. 在 `servers/` 下新建一个 Python 文件（如 `bash_server.py`）。
2. 使用 `mcp` 库：`Server` + `stdio_server`，实现 `list_tools` 和 `call_tool`。
3. 在 `deploy/configmap.yaml` 的 **basicmcp** 中注册（path、port）。
4. 在 `deploy/deployment.yaml` 和 `deploy/service.yaml` 中增加对应端口。
5. 部署后通过 `http://mcp-server-manager.mcp:<port>/sse` 使用。

---

## 三、代码模板（最小可用）

```python
#!/usr/bin/env python3
"""
我的 MCP Server

运行方式:
    # 直接运行 (stdio 模式，用于调试)
    python my_server.py

    # 由启动器通过 mcp-proxy 暴露为 SSE（生产环境）
    # 见 deploy/configmap.yaml 中 basicmcp 配置
"""

import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("my-mcp-server")


@server.list_tools()
async def list_tools():
    """声明对外提供的工具"""
    return [
        Tool(
            name="my_tool",
            description="工具描述，供 AI 理解何时调用",
            inputSchema={
                "type": "object",
                "properties": {
                    "param1": {"type": "string", "description": "参数说明"}
                },
                "required": ["param1"]
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """处理工具调用"""
    if name == "my_tool":
        result = f"收到: {arguments.get('param1')}"
        return [TextContent(type="text", text=result)]
    return [TextContent(type="text", text=f"未知工具: {name}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

要点：
- 用 `Server("名称")` 创建服务，用 `@server.list_tools()` / `@server.call_tool()` 注册工具。
- 每个工具需要 `name`、`description`、`inputSchema`（JSON Schema）。
- 返回内容用 `TextContent(type="text", text=...)`，可返回 JSON 字符串便于调用方解析。

---

## 四、以 bash_command 为例：新增第三个本地服务

### 1. 新建 `servers/bash_server.py`

- 实现两个工具：`run_bash_command`、`kubectl_run_image`。
- `run_bash_command`：对命令做简单安全校验（禁止 `rm -rf /`、`mkfs` 等），通过后用 `subprocess.run(..., executable="/bin/bash")` 执行，返回 stdout+stderr。
- `kubectl_run_image`：校验 namespace、镜像与命令，拼出 `kubectl run ... --rm --attach -- <command>` 并执行。

（具体实现见项目中的 `servers/bash_server.py`。）

### 2. 在 ConfigMap 中注册（basicmcp）

文件：`deploy/configmap.yaml`，在 `basicmcp` 下增加：

```yaml
basicmcp:
  - name: test-tools
    path: "servers/test_server.py"
    port: 8091
    enabled: true
  - name: helm-tools
    path: "servers/helm_server.py"
    port: 8092
    enabled: true
  - name: bash_command
    path: "servers/bash_server.py"
    port: 8094
    enabled: true
```

- `name`：服务名，会出现在日志和 AIOPS 配置中。
- `path`：相对项目根或相对配置文件所在目录的路径，启动器会解析为 `/app/servers/bash_server.py`（容器内）。
- `port`：独占端口，需与 deployment、service 一致（如 8094）。

### 3. 在 Deployment 中暴露端口

文件：`deploy/deployment.yaml`，在 `containers[0].ports` 中增加：

```yaml
- name: bash-mcp
  containerPort: 8094
```

### 4. 在 Service 中暴露端口

文件：`deploy/service.yaml`，在 `spec.ports` 中增加：

```yaml
- name: bash-mcp
  port: 8094
  targetPort: 8094
```

### 5. 部署与验证

```bash
make deploy
kubectl logs -f -n mcp deployment/mcp-server-manager
```

期望看到类似：`[bash_command] starting server on port 8094`。

在 AIOPS 中配置该 MCP：

```yaml
bash_command:
  description: "Bash / kubectl 执行工具"
  config:
    url: "http://mcp-server-manager.mcp:8094/sse"
    mode: "sse"
  enabled: true
```

---

## 五、本地运行与调试

- **仅跑 MCP（stdio）**：在项目根执行  
  `python servers/bash_server.py`  
  可与支持 stdio 的 MCP 客户端直接对接调试。
- **模拟生产**：使用 mcp-proxy 暴露为 SSE：  
  `npx -y mcp-proxy --port 8094 --server sse -- python servers/bash_server.py`  
  然后用浏览器或 AIOPS 连 `http://localhost:8094/sse` 测试。

---

## 六、检查清单（新增一个本地 MCP）

- [ ] 在 `servers/` 下新建 `xxx_server.py`，实现 `list_tools` + `call_tool`，入口 `asyncio.run(main())` + `stdio_server`。
- [ ] 在 `deploy/configmap.yaml` 的 **basicmcp** 中增加 `name`、`path`、`port`、`enabled`。
- [ ] 在 `deploy/deployment.yaml` 的 **ports** 中增加对应 `containerPort`。
- [ ] 在 `deploy/service.yaml` 的 **ports** 中增加对应 `port`/`targetPort`。
- [ ] 三处端口一致，且未被其他服务占用。
- [ ] 执行 `make deploy` 并查看日志确认启动；如需在 AIOPS 使用，在对应 config 中增加 url 与 enabled。

---

## 七、当前端口与服务对应

| 端口 | 服务名       | 说明           |
|------|--------------|----------------|
| 8088 | elasticsearch| 第三方 npm     |
| 8091 | test-tools   | 本地 test_server |
| 8092 | helm-tools   | 本地 helm_server |
| 8093 | kubernetes   | 第三方 npm     |
| 8094 | bash_command | 本地 bash_server |

新本地服务建议从 8095 起选用未占用端口。
