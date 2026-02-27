# MCP Server Manager 扩展与部署指南

本文档说明如何添加新的 MCP 工具（第三方 npm/uv 包或本地 Python Server），并完成本地与 K8s 部署及 AIOps 集成。**不修改现有代码逻辑，仅通过配置与约定扩展。**

---

## 一、扩展方式概览

| 方式 | 配置位置 | 说明 |
|------|----------|------|
| 第三方 npm 包 | `customermcp` | 使用 `mcp-proxy` + `npx -y <package>` 或 uv 启动 |
| 本地 Python MCP | `basicmcp` | 使用 `mcp-proxy` + `python servers/xxx_server.py`（stdio） |

所有 MCP 均通过 **mcp-proxy** 暴露为 `http://host:port/sse`，与 HolmesGPT 的 SSE 模式兼容。

---

## 二、添加第三方 npm 包

### 2.1 本地配置（config/mcp_config.yaml）

在 `customermcp` 中增加一项，例如：

```yaml
customermcp:
  - name: elasticsearch
    type: npm
    package: "@elastic/mcp-server-elasticsearch@0.3.1"
    port: 8088
    enabled: true
    env:
      ES_URL: "https://elasticsearch:9200"
      ES_USERNAME: "elastic"
      ES_PASSWORD: "changeme"
```

- **type: uv** 时需增加 `directory: "/path/to/uv-project"`，启动命令为 `uv --directory <dir> run <package>`。

### 2.2 K8s 配置（deploy/configmap.yaml）

在 `data.mcp_config.yaml` 的 `customermcp` 下添加同样结构（端口与 K8s 暴露的端口一致，见下文）。

### 2.3 若为新端口：同步 K8s 端口

1. **deploy/deployment.yaml**：在 `containers[0].ports` 中增加：
   ```yaml
   - name: xxx-mcp
     containerPort: 8088
   ```
2. **deploy/service.yaml**：在 `spec.ports` 中增加：
   ```yaml
   - name: xxx-mcp
     port: 8088
     targetPort: 8088
   ```

端口号需与配置中的 `port` 一致。

---

## 三、添加本地 Python MCP Server

### 3.1 编写 MCP Server（stdio）

在 `servers/` 下新建文件，例如 `servers/my_server.py`，使用 `mcp` 库的 stdio 协议：

- 使用 `Server("name")`、`@server.list_tools()`、`@server.call_tool()`。
- 入口使用 `async with stdio_server() as (r, w): await server.run(r, w, ...)`。

可参考 `servers/test_server.py` 或 `servers/k8s_core_server.py`（后者将逻辑放在 `holmes_tools` 中）。

### 3.2 本地配置（config/mcp_config.yaml）

在 `basicmcp` 中增加：

```yaml
basicmcp:
  - name: my-tools
    path: "servers/my_server.py"
    port: 8098
    enabled: true
    # env: 可选
    #   MY_VAR: "value"
```

`path` 为相对**项目根目录**的路径；启动器会先按 `Path(__file__).parent / path` 解析（即项目根下的路径）。

### 3.3 K8s 配置与端口

1. 在 **deploy/configmap.yaml** 的 `basicmcp` 中增加同样一项（端口与 K8s 一致）。
2. 若为新端口，在 **deploy/deployment.yaml** 和 **deploy/service.yaml** 中增加对应 `containerPort` 和 `ports` 项。

### 3.4 镜像与代码

- 修改 `servers/` 下代码后，需重新构建镜像：`make build-push`，再 `make deploy` 或 `make restart`。
- 仅改 ConfigMap 时，`make reload` 即可。

---

## 四、AIOps / HolmesGPT 侧配置

在 AIOps 项目的配置（如 `config/config.yaml` 或 K8s ConfigMap）中，为每个要使用的 MCP 增加一项：

```yaml
mcp_servers:
  my-tools:
    description: "我的 MCP 工具集"
    config:
      url: "http://mcp-server-manager.mcp:8098/sse"
      mode: "sse"
    enabled: true
```

- `url` 中 `<namespace>` 替换为 MCP 实际部署的 namespace（如 `mcp`），`<port>` 与 ConfigMap 及 Service 端口一致。
- 修改后需重载 AIOps 的 ConfigMap 或重启 Agent，以加载新工具。

---

## 五、本地调试与配置覆盖

- **默认配置**：`config/mcp_config.yaml`（`python start.py` 或 `make run` 使用）。
- **本地覆盖**：复制 `config/mcp_config.example.yaml` 为 `config/mcp_config.local.yaml`，按需关闭服务、改 `PROMETHEUS_URL` 等；`make run-local` 会优先使用 `config/mcp_config.local.yaml`。
- **指定配置**：`python start.py --config config/mcp_config.local.yaml`。

Docker/K8s 部署不读仓库内文件，仅使用 ConfigMap 挂载的 `/app/config/mcp_config.yaml`，逻辑与本地一致。

---

## 六、验证方式（确保修改未影响逻辑）

按下面顺序做一次即可确认“配置与路径”无误。

### 6.1 本地：列出配置

```bash
cd /root/huhu/agent/mcpstander
pip install -r requirements.txt   # 如未安装
python start.py --list
```

应看到 `config/mcp_config.yaml` 中的 customermcp 与 basicmcp 列表，且无报错。

### 6.2 本地：启动并测一个 SSE

```bash
# 仅启动（可用 Ctrl+C 结束）
python start.py
# 或使用本地覆盖配置
make run-local
```

在另一终端用测试客户端连一个已启用服务的端口（例如 test-tools 8090）：

```bash
python mcp_client.py http://localhost:8090/sse
```

能连上并完成 initialize / list tools 即表示启动与配置路径正确。

### 6.3 K8s：部署与连通性（可选）

```bash
make build-push
make deploy
make status
```

在集群内或从可访问集群的机器上，用 curl 测一个端口（以 8091 为例）：

```bash
kubectl run curl-test --rm -it --image=curlimages/curl -n mcp -- \
  curl -s -o /dev/null -w "%{http_code}" http://mcp-server-manager:8091/sse
```

返回 200 或 SSE 流即表示部署与端口配置正确。

---

## 七、故障排查简表

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| 配置文件不存在 | 使用了默认 `config/mcp_config.yaml` 但文件被删/移动 | 确认 `config/mcp_config.yaml` 存在，或使用 `--config` 指定 |
| 某服务启动失败 | 端口占用、path 错误、依赖缺失 | 看启动日志；path 相对项目根；检查 Node/Python 依赖 |
| K8s 内连不上 /sse | Service 未暴露该端口、ConfigMap 未更新 | 检查 deployment/service 的 ports 与 ConfigMap 的 port 一致；`make reload` |
| AIOps 显示 0 个工具 | url/namespace/port 错误、AIOps 未重载 | 核对 mcp_servers.*.config.url 与 K8s Service 的 namespace 和 port；重载 AIOps 配置 |

完成上述步骤后，扩展新工具不会改变现有代码逻辑，仅通过配置与端口约定接入；用“列出配置 + 本地启动 + 客户端测 SSE”即可验证修改未影响行为。
