# MCP Server Manager

将 MCP 工具转换为 SSE 端点，供 HolmesGPT 等 AIOps 服务集成使用。  
通过 **mcp-proxy** 将 stdio MCP 转为 HTTP SSE，每个 MCP 对应一个端口上的 `/sse`。

---

## 如何扩展并运行新 MCP 工具（简要）

1. **写 MCP 服务**：在 `servers/` 下写 Python MCP（或使用第三方 npm/uv 包）。
2. **写配置**：在 `config/mcp_config.yaml`（本地）或 `deploy/configmap.yaml`（K8s）里增加一项，写好 `name`、`path`/`package`、`port`、`enabled`。
3. **端口同步**：若部署到 K8s，在 `deploy/deployment.yaml` 和 `deploy/service.yaml` 中增加对应端口。
4. **构建与运行**：本地用 `make run-local` 或 `python start.py`；K8s 用 `make build-push`、`make deploy`。
5. **AIOps 侧**：在 AIOps 的 `config.yaml` / ConfigMap 的 `mcp_servers` 中增加该 MCP 的 `url: http://mcp-server-manager.<ns>:<port>/sse`、`mode: sse`，重载配置。

详细步骤见 **[docs/EXTENDING.md](docs/EXTENDING.md)**。

---

## 目录结构

```
mcpstander/
├── start.py                 # 统一启动器（读配置，为每个 MCP 起 mcp-proxy 子进程）
├── mcp_client.py             # SSE 测试客户端
├── config/                   # 配置文件（本地开发）
│   ├── mcp_config.yaml       # 默认配置
│   ├── mcp_config.example.yaml   # 本地覆盖示例（复制为 mcp_config.local.yaml 使用）
│   └── mcp_config.local.yaml    # 本地覆盖（可选，已 gitignore）
├── servers/                  # 本地自定义 MCP Server（stdio 协议）
│   ├── test_server.py
│   ├── helm_server.py
│   ├── k8s_core_server.py
│   └── holmes_tools/         # 能力实现（k8s/helm/prometheus 等）
├── deploy/                   # K8s 部署
│   ├── configmap.yaml        # 生产配置（内嵌 mcp_config.yaml 内容）
│   ├── deployment.yaml
│   └── service.yaml
├── docs/
│   └── EXTENDING.md          # 扩展与部署详细指南
├── scripts/
│   └── run-local.sh         # 本地一键启动（优先使用 config/mcp_config.local.yaml）
├── Dockerfile
├── Makefile
└── VERSION
```

---

## 快速开始

### 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 使用默认配置 config/mcp_config.yaml 启动
python start.py
# 或
make run
```

本地调试时若不想改仓库内配置，可复制示例后使用本地配置：

```bash
cp config/mcp_config.example.yaml config/mcp_config.local.yaml
# 编辑 config/mcp_config.local.yaml 后：
make run-local
```

`make run-local` 会自动优先使用 `config/mcp_config.local.yaml`（若存在），并检查 `.venv` 与依赖。

### 列出已配置服务

```bash
python start.py --list
# 或
make list
```

### K8s 部署

```bash
make build-push    # 构建并推送镜像
make deploy        # 部署到 K8s（namespace: mcp）
make status        # 查看 Pod/Service
```

---
 
## 配置说明

- **本地**：编辑 `config/mcp_config.yaml`（或你的 `config/mcp_config.local.yaml`）。  
- **K8s**：编辑 `deploy/configmap.yaml` 中 `data.mcp_config.yaml` 下的 YAML 内容；容器内挂载路径为 `/app/config/mcp_config.yaml`。

配置结构：

- **customermcp**：第三方 MCP（npm 或 uv 包），每项需 `name`、`type`（npm/uv）、`package`、`port`、`enabled`，uv 需 `directory`，可选 `env`。
- **basicmcp**：本地 Python MCP，每项需 `name`、`path`（相对项目根，如 `servers/test_server.py`）、`port`、`enabled`，可选 `env`。

启动器会为每一项启动：`mcp-proxy --port <port> --server sse -- <inner_cmd>`，其中本地为 `python <path>`，npm 为 `npx -y <package>` 等。

---

## HolmesGPT / AIOps 集成

SSE 端点格式：`http://mcp-server-manager.<namespace>:<port>/sse`。  
若集群内 namespace 为 `mcp`，则示例：

```yaml
mcp_servers:
  elasticsearch:
    description: "Elasticsearch MCP - 查询索引、搜索日志"
    config:
      url: "http://mcp-server-manager.mcp:8088/sse"
      mode: "sse"
    enabled: true

  test-tools:
    description: "测试工具集"
    config:
      url: "http://mcp-server-manager.mcp:8091/sse"
      mode: "sse"
    enabled: true
```

端口以你在 ConfigMap 与 deployment/service 中配置的 `port` 为准。

---

## Makefile 命令

| 命令 | 说明 |
|------|------|
| `make build` | 构建镜像 |
| `make push` | 推送镜像 |
| `make build-push` | 构建并推送 |
| `make deploy` | 部署到 K8s |
| `make delete` | 删除部署资源（保留 namespace） |
| `make reload` | 更新 ConfigMap 并重启 |
| `make status` | 查看 Pod/Service/ConfigMap |
| `make logs` | 查看日志 |
| `make restart` | 重启 Deployment |
| `make run` | 本地运行（默认 config/mcp_config.yaml） |
| `make run-local` | 本地运行（优先 config/mcp_config.local.yaml） |
| `make list` | 列出配置中的服务 |
| `make test` | 运行 mcp_client 测试 |

---

## 注意事项

1. **端口同步**：修改 ConfigMap 中某 MCP 的 `port` 后，需在 `deploy/deployment.yaml` 的 `ports` 和 `deploy/service.yaml` 的 `ports` 中同步增加或修改对应端口。
2. **镜像更新**：修改 `start.py` 或 `servers/` 下代码后，需重新 `make build-push` 并 `make deploy` 或 `make restart`。
3. **仅改配置**：只改 ConfigMap 时，执行 `make reload` 即可。

更多扩展步骤、故障排查与验证方式见 **[docs/EXTENDING.md](docs/EXTENDING.md)**。
