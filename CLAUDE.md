# CLAUDE.md

本文档为 Claude Code (claude.ai/code) 在此代码库中工作提供指导。

---

## 项目概述

**MCP Server Manager** - 统一管理 MCP (Model Context Protocol) 服务器的工具，将其转换为 SSE (Server-Sent Events) 端点，供 HolmesGPT 等 AI 服务集成使用。

---

## 代码结构

| 文件/目录 | 职责 |
|-----------|------|
| `start.py` | `MCPServerManager`：读取 YAML、启动子进程、日志（stderr）、崩溃后指数退避重启（1s 起翻倍，上限 60s） |
| `launch_config.py` | 纯函数：子进程环境变量、`mcp-proxy` 命令拼装 |
| `servers/*_server.py` | 各 MCP 服务入口（stdio），由 `mcp-proxy` 暴露为 SSE |
| `servers/holmes_tools/` | 工具实现（kubectl/prometheus/runbook 等）、`server_adapter.call_text_tool_in_thread`（阻塞调用放到工作线程）、`mcp_logger`（只写 stderr） |
| `servers/aiops_observability/` | AIOps 观测采集（collectors）与查询（query，输出 `aiops.fact-ledger.v1`） |
| `mcp_client.py` | SSE 调试客户端 |
| `scripts/test_unit.py` | 离线单元/合同测试（`make test-unit`） |

### 扩展新服务类型

- 本地服务：新增 `servers/<name>_server.py`，在配置 `basicmcp` 中登记 `path`/`port`。
- 第三方包：在 `customermcp` 中登记（`type: npm` 或 `type: uv` + `directory`）；新的包类型在 `start.py` 的 `start_package_server()` 中加分支。

---

## 架构说明

### 支持的 MCP 服务类型

1. **npm 包** - `npx -y <package>`
2. **uv 包** - `uv --directory <dir> run <package>`
3. **本地自定义服务器** - `servers/` 下的 Python 脚本（`sys.executable <path>`）

所有类型统一由 `mcp-proxy`（镜像内预装，缺失时回退 `npx -y mcp-proxy`）包装：
`mcp-proxy --port <port> --server sse -- <inner command>`，SSE 端点为 `http://localhost:<port>/sse`。

---

## 常用命令

### 本地开发

```bash
# 使用默认配置运行 (mcp_config.yaml)
python start.py

# 使用指定配置运行
python start.py --config path/to/config.yaml

# 列出配置的服务
python start.py --list
# 或
make list
```

### Docker & Kubernetes

```bash
# 构建并推送镜像
make build-push

# 部署到 K8s（自动从 VERSION 文件同步版本号）
make deploy

# 仅更新 ConfigMap 并重启（无需重建镜像）
make reload

# 重启 pods
make restart

# 查看状态
make status

# 查看日志
make logs
```

### 镜像信息

- 基础镜像：`python:3.11-slim`
- 仓库：`xnet.registry.io:8443/xnet-cloud/mcp-server-manager`
- 版本：从 `VERSION` 文件读取，`make deploy` 时自动同步到 `deploy/deployment.yaml`

---

## 添加新 MCP 工具

1. **创建 MCP 服务** - 在 `servers/<name>_server.py` 编写，或选择第三方 npm/uv 包
2. **添加配置** - 同步更新两个配置文件：
   - `config/mcp_config.yaml`（本地开发用）
   - `deploy/configmap.yaml`（生产环境）
3. **更新 K8s 清单** - 在以下文件中添加端口：
   - `deploy/deployment.yaml` - 添加 `containerPort`
   - `deploy/service.yaml` - 添加 `port` 和 `targetPort`
4. **更新 aiops 配置** - 在消费者 configmap 中添加新工具的 SSE URL
5. **重新部署** - `make build-push && make deploy`

---

### 端口映射同步

添加新工具时，必须同步修改三个文件的端口配置：

| 文件 | 字段位置 |
|------|----------|
| `deploy/configmap.yaml` | `customermcp` 或 `basicmcp` 中的 `port` |
| `deploy/deployment.yaml` | `spec.template.spec.containers[0].ports` 下的 `containerPort` |
| `deploy/service.yaml` | `spec.ports` 下的 `port` 和 `targetPort` |

---

## 环境变量

- 管理器自动设置 `NODE_TLS_REJECT_UNAUTHORIZED=0`，以避免 Node.js HTTPS 请求时的自签名证书问题。
- `MCP_TOOL_MAX_OUTPUT_BYTES`（默认 1000000）：命令类工具返回输出上限，超出时追加截断标记。
- `MCP_TOOL_MAX_TIMEOUT_SECONDS`（默认 600）：调用方传入的 `timeout_seconds`/`timeout` 上限。
- `MCP_LOG_LEVEL` / `MCP_LOG_FORMAT`（text|json）：服务端日志级别与格式。

---

## 当前已配置的工具

以 `deploy/configmap.yaml` 为准（生产）：

| 名称 | 类型 | 端口 | 路径/包 |
|------|------|------|---------|
| elasticsearch | npm（disabled） | 8088 | `@elastic/mcp-server-elasticsearch@0.3.1` |
| aiops-case | 本地 | 8089 | `servers/aiops_case_server.py` |
| aiops-observability-fine | 本地 | 8090 | `servers/aiops_observability_fine_server.py` |
| test-tools | 本地 | 8091 | `servers/test_server.py` |
| helm-tools | 本地 | 8092 | `servers/helm_server.py` |
| k8s-core | 本地 | 8093 | `servers/k8s_core_server.py` |
| bash_command | 本地 | 8094 | `servers/bash_server.py` |
| prometheus | 本地 | 8095 | `servers/prometheus_server.py` |
| internet | 本地 | 8096 | `servers/internet_server.py` |
| connectivity_check | 本地 | 8097 | `servers/connectivity_server.py` |
| core-investigation | 本地 | 8098 | `servers/core_investigation_server.py` |
| runbook | 本地 | 8099 | `servers/runbook_server.py` |
| aiops-observability-query | 本地 | 8100 | `servers/aiops_observability_query_server.py` |

---

## 消费者 SSE URL 格式

```
http://mcp-server-manager.mcp:<port>/sse
```

`mcp` 为 namespace 名称，根据实际部署配置可能有所不同。
