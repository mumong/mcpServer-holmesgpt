# MCP Server Manager

[English](README.en.md) | **中文**

本项目负责把多个 stdio MCP Server 统一拉起，并通过 `mcp-proxy` 暴露成 HTTP SSE 端点，供 HolmesGPT / AIOps 等服务通过 `mcp_servers` 接入。

核心链路：

```text
AIOps/HolmesGPT mcp_servers.url
  -> http://mcp-server-manager.<mcp-namespace>:<port>/sse
  -> Kubernetes Service mcp-server-manager
  -> mcp-proxy
  -> stdio MCP Server
  -> 可选外部依赖服务，如 Prometheus / Elasticsearch / Runbook 目录
```

注意区分两类 URL：

- **MCP SSE URL**：AIOps 连接本项目的地址，例如 `http://mcp-server-manager.mcp:8095/sse`。
- **外部依赖服务 URL**：某个 MCP 工具内部访问的后端服务地址，例如 Prometheus 的 `PROMETHEUS_URL`。

这两类 URL 的 namespace 可以不同，改 namespace 时也要改对应的 URL。

---

## 当前项目结构

```text
mcpstander/
├── start.py                      # 统一启动器：读取配置，为每个 MCP 启动 mcp-proxy 子进程
├── launch_config.py              # 纯函数：子进程环境与 proxy 命令构造
├── mcp_client.py                 # SSE 测试客户端
├── config/                       # 本地运行配置（mcp_config.yaml / .example.yaml）
├── servers/                      # 本地 Python MCP Server（stdio 协议）
│   ├── *_server.py               # 每个 MCP 一个入口（k8s_core、prometheus、bash、helm、aiops_observability_query 等）
│   ├── cluster_scope.py          # 多集群：每次工具调用的目标集群（cluster 参数）
│   ├── aiops_edge/               # 多集群：kubectl 替身与 xnet 隧道客户端（Agent 仓库 aiops_edge/ 的同步副本）
│   ├── holmes_tools/             # 工具合同、分派和共享文本入口适配器
│   └── aiops_observability/      # 采集器、case 存储、Pod query 与 Fact Ledger
├── bin/kubectl                   # 镜像里的 kubectl 替身：本集群直接执行，边缘集群转发给 runner
├── deploy/                       # 单集群清单（configmap / deployment / service / rbac / namespace）
│   └── envs/mc/                  # 多集群中心的 kustomize 覆盖（make deploy-mc）
├── scripts/                      # 本地运行、离线测试、语法检查、aiops_edge 同步脚本
├── docs/                         # 扩展新 MCP、Pod 查询与 Fact Ledger 合同
├── tests/                        # unittest 单元与合同回归测试
├── Dockerfile、Makefile、VERSION
└── requirements.txt / requirements-test.txt
```

---

## 已内置的 MCP 服务

以 `deploy/configmap.yaml` 中的部署配置为准，当前主要服务如下：

| MCP 名称 | 端口 | 类型 | 说明 |
|---|---:|---|---|
| `elasticsearch` | 8088 | 第三方 npm MCP | Elasticsearch 查询，当前部署配置中为 disabled |
| `aiops-case` | 8089 | 本地 Python MCP | 本仓库采集器生成、持久化并读取 Kubernetes / Prometheus / logging / DeepFlow / topology case package |
| `aiops-observability-fine` | 8090 | 本地 Python MCP | 按维度采集 Pod 观测证据；部署配置启用，本地默认配置禁用 |
| `test-tools` | 8091 | 本地 Python MCP | 测试工具 |
| `helm-tools` | 8092 | 本地 Python MCP | Helm list / values / status / history / manifest 等 |
| `k8s-core` | 8093 | 本地 Python MCP | Kubernetes describe / get / events / logs / top / jq / lineage 等 |
| `bash_command` | 8094 | 本地 Python MCP | 受控 bash 命令与临时 Pod 调试 |
| `prometheus` | 8095 | 本地 Python MCP | 通用 Prometheus rules / metrics / labels / series / PromQL 查询；实际工具选择由 Agent 路由与任务范围决定，不按 `/ask`、`/query` 入口禁止使用 |
| `internet` | 8096 | 本地 Python MCP | 抓取网页内容 |
| `connectivity_check` | 8097 | 本地 Python MCP | TCP 连通性检查 |
| `core-investigation` | 8098 | 本地 Python MCP | 调查任务规划工具 |
| `runbook` | 8099 | 本地 Python MCP | Runbook 搜索与读取 |
| `aiops-observability-query` | 8100 | 本地 Python MCP | `/ask` Pod 异常诊断使用的 Pod-scoped Metrics / Logging / Tracing / Topology 查询；Metrics 工具为 `execute_pod_promql`，原生发布 canonical Fact Ledger |

本地配置 `config/mcp_config.yaml` 可以与 K8s 部署端口不同，实际运行时请以当前使用的配置文件为准：

```bash
python3 start.py --list
```

### AIOps 自主查询与 Fact Ledger

`aiops-observability-query` 通过 `8100/sse` 暴露四个通用工具：

```text
execute_pod_promql
query_pod_logs
query_pod_tracing
query_pod_topology
```

工具不按故障类型路由。每个结果都绑定精确 namespace、Pod 和可选 Pod
UID，并在最终 6144-byte 有界投影上构建
`aiops.fact-ledger.v1`。canonical Ledger 使用 `source=mcp_canonical`、
`legacy_contract=false`，只发布数据源事实、coverage 和 evidence refs，不包含
diagnosis、root cause、remediation 或 evaluator label。

详细合同、边界和测试方式见
[`docs/AIOPS_OBSERVABILITY_QUERY.md`](docs/AIOPS_OBSERVABILITY_QUERY.md)。

---

## 本地运行

代码使用 Python 3.10+ 语法，镜像基于 Python 3.11。运行依赖锁定在
`requirements.txt`；本仓库仅测试代码直接使用 `jsonschema`，其测试版本约束列在
`requirements-test.txt`。运行镜像不安装该测试依赖文件，但 MCP SDK 自身也会
间接安装 `jsonschema`，因此它不是从运行镜像中排除的包。

离线验证（已有依赖时不执行安装）：

```bash
make check       # 语法检查 + 离线 unittest 合同测试
make test-unit   # 仅离线测试
make lint        # ruff E9/F/I/B；make format 统一格式
```

`make test` 保留原 SSE 客户端检查，需要运行中的服务，并非单测。
测试会 mock 后端和命令执行，离线入口额外阻止意外网络/子进程访问；它不能
替代真实 SSE、Kubernetes 后端或镜像验证。详见 [贡献指南](CONTRIBUTING.md)。

运行边界：`start.py` 负责进程生命周期（日志写 stderr；子进程退出后按 1s 起翻倍、
上限 60s 的退避自动重启，稳定运行 60s 后重置），`launch_config.py` 构造环境与命令；
各 MCP 入口保留工具合同，通过 `holmes_tools` 分派到采集/查询实现。
八个文本入口共用 `holmes_tools/server_adapter.py`，但保留各自参数清洗和
异常策略；阻塞的命令/HTTP 调用在工作线程执行，不阻塞 MCP 事件循环。
query 入口继续独立维护结构化错误与 Fact Ledger 合同。
命令类工具输出上限 `MCP_TOOL_MAX_OUTPUT_BYTES`（默认 1000000），调用方传入的超时
上限 `MCP_TOOL_MAX_TIMEOUT_SECONDS`（默认 600）。

安装依赖并启动默认配置：

```bash
python3 -m pip install -r requirements.txt
python3 start.py
```

或使用 Makefile：

```bash
make run
```

如果本机没有 `python` 命令但有 `python3`，直接运行时使用 `python3`；Makefile 目标沿用仓库现状，依赖 `python` 命令或已激活的 `.venv`。

本地调试建议使用覆盖配置，避免直接改默认配置：

```bash
cp config/mcp_config.example.yaml config/mcp_config.local.yaml
# 按需编辑 config/mcp_config.local.yaml
make run-local
```

`make run-local` 会优先读取 `config/mcp_config.local.yaml`。Prometheus 本地调试可把 `PROMETHEUS_URL` 配成 `http://localhost:9090`，或先对集群 Prometheus 做 port-forward。

---

## K8s 部署

三个服务一起部署（单集群、多集群、边缘 runner）的复制命令见 Agent 仓库 README「部署（复制即用）」。单独部署本服务：

```bash
make deploy                       # 已有镜像：apply + 滚动重启 + 等待就绪，失败自动回滚到上一版本
echo <新版本号> > VERSION && make build push deploy   # 改了代码
make deploy-mc                    # 多集群中心（先在 Agent 仓库执行 make mc-init）
make status                       # 查看 Pod / Service / ConfigMap
```

`make deploy` 每次都会滚动重启，所以只改 `deploy/configmap.yaml` 时执行它也会生效（`make reload` 效果相同）。改了 `start.py`、`launch_config.py`、`servers/` 或 `Dockerfile` 时，要先把 `VERSION` 加一并重新构建镜像。

默认部署 namespace 是 `mcp`，由 `Makefile` 的 `NAMESPACE := mcp` 和 `deploy/*.yaml` 中的 namespace 共同决定（含 `rbac.yaml` 中 ClusterRoleBinding 的 subject），修改时要一起改。

### aiops-case 数据目录挂载

`aiops-case` 不是无状态查询工具。本仓库的
`servers/aiops_observability/case_builder.py` 编排采集器并写入 case package，
不依赖 data 项目或 `AIOPS_DATASET_ROOT` / `AIOPS_COLLECT_SCRIPT`。

当前部署设置 `AIOPS_CASES_DIR=/app/aiops-cases`，将 master 节点上的
`/root/huhu/agent/combine-aiops-mcp/aiops-cases` 以 `hostPath` 挂载到该目录，
并用 `nodeSelector` 固定调度到 `master`；本地默认目录为 `./aiops-cases`。
迁移存储需要单独规划数据保留与挂载方式，不要假设其他节点拥有此目录。

---

## MCP 配置方式

配置文件分两类：

- **本地运行**：`config/mcp_config.yaml` 或 `config/mcp_config.local.yaml`
- **K8s 运行**：`deploy/configmap.yaml` 中 `data.mcp_config.yaml` 的内容，容器内挂载为 `/app/config/mcp_config.yaml`

配置顶层分为两组：

```yaml
customermcp: []  # 第三方 npm / uv MCP
basicmcp: []     # 本仓库 servers/ 下的 Python MCP
```

### 本地 Python MCP

```yaml
basicmcp:
  - name: prometheus
    path: "servers/prometheus_server.py"
    port: 8095
    enabled: true
    env:
      PROMETHEUS_URL: "http://observability-prometheus.xnet.svc:9090"
      PROMETHEUS_TIMEOUT: "30"
```

字段说明：

| 字段 | 说明 |
|---|---|
| `name` | MCP 服务名，日志和配置识别用 |
| `path` | Python server 文件路径，相对项目根目录 |
| `port` | 该 MCP 暴露的 SSE 端口 |
| `enabled` | 是否启动 |
| `env` | 传给该 MCP 子进程的环境变量 |

启动器实际执行类似：

```bash
mcp-proxy --port 8095 --server sse -- python servers/prometheus_server.py
```

### 第三方 npm MCP

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
      ES_SSL_SKIP_VERIFY: "true"
```

`type: npm` 会通过 `npx -y <package>` 启动。`type: uv` 时还需要配置 `directory`，启动命令为 `uv --directory <directory> run <package>`。

### K8s 端口同步规则

新增 MCP 或修改端口时，必须同步四处：

1. `deploy/configmap.yaml` 中该 MCP 的 `port`
2. `deploy/deployment.yaml` 中 `containers[0].ports[].containerPort`
3. `deploy/service.yaml` 中 `spec.ports[].port` 和 `targetPort`
4. AIOps/HolmesGPT 配置里的 `mcp_servers.<name>.config.url`

只改 `env`、`enabled` 或第三方服务地址时，改 ConfigMap 后执行 `make deploy` 或 `make reload` 即可。

---

## AIOps / HolmesGPT 接入

AIOps 侧配置的是 **MCP SSE URL**，格式为：

```text
http://mcp-server-manager.<mcp-namespace>:<port>/sse
```

如果 AIOps 与本项目在同一个 namespace，也可以使用短地址：

```text
http://mcp-server-manager:<port>/sse
```

示例，本项目部署在 `mcp` namespace：

```yaml
mcp_servers:
  prometheus:
    description: "Prometheus MCP - 查询指标、标签、规则和 PromQL"
    config:
      url: "http://mcp-server-manager.mcp:8095/sse"
      mode: "sse"
    enabled: true

  k8s-core:
    description: "Kubernetes MCP - 查询资源、事件、日志和实时指标"
    config:
      url: "http://mcp-server-manager.mcp:8093/sse"
      mode: "sse"
    enabled: true

  aiops-observability-query:
    description: "Pod-scoped observability queries with canonical Fact Ledger"
    config:
      url: "http://mcp-server-manager.mcp:8100/sse"
      mode: "sse"
    enabled: true
```

如果本项目部署 namespace 改成 `monitor`，AIOps 中的 URL 也要改：

```yaml
config:
  url: "http://mcp-server-manager.monitor:8095/sse"
  mode: "sse"
```

修改 AIOps 的 ConfigMap 后，需要重载或重启 AIOps Agent，让它重新加载 MCP 工具。

---

## 多集群模式

挂载集群配置（ConfigMap `aiops-clusters`，环境变量 `AIOPS_CLUSTERS_CONFIG`）后开启；不挂载时就是单集群，工具定义和行为与原来相同。

每个工具多一个 `cluster` 参数（`servers/cluster_scope.py`），缺少时返回 `cluster_required`；新增 `list_clusters` 返回集群清单和在线状态。不同工具在不同地方执行：

| 工具 | 执行位置 | 说明 |
|---|---|---|
| 指标、日志、链路（Prometheus、`aiops-observability-query`） | 中心 | 查询中心的 VictoriaMetrics / VictoriaLogs，按 `cluster` 标签过滤；没有链路数据时返回 `capability_missing` |
| kubectl 类（`k8s-core`、Pod 生命周期等） | 边缘 | 子进程环境带上目标集群，`bin/kubectl` 替身经 xnet 隧道把命令交给边缘的 `aiops-edge-runner` 执行，按 runner 权限等级放行 |
| `bash_command` | 边缘集群只接受 `kubectl … \| jq/grep` 这类管道 | kubectl 部分在边缘执行，过滤在中心执行；其他命令会被拒绝 |
| `helm-tools` | 只在本集群 | 远端集群返回 `capability_missing` |

- 部署：先在 Agent 仓库执行 `make mc-init`（集群配置、xnet 证书、签名密钥放进 `mcp` 命名空间，不需要手动处理证书），再执行本仓库的 `make deploy-mc`；缺少这些对象时会提示。
- `servers/aiops_edge/` 是 Agent 仓库 `aiops_edge/` 的同步副本，不要单独修改：在这里执行 `make sync-aiops-edge` 同步（默认从 `../robusta/aiops_edge` 复制；按 README 克隆的目录名是 `holmegpt-agent`，这时执行 `./scripts/sync_aiops_edge.sh ../holmegpt-agent/aiops_edge`），`tests/test_aiops_edge_sync.py` 会检查两边是否一致。
- 完整的设计与实现见 Agent 仓库的 `docs/multi-cluster-design.md`（单一来源）。

## 外部依赖服务 URL 与 namespace

某些 MCP 工具还会访问外部服务，这些地址在该 MCP 的 `env` 中配置。最容易混淆的是 Prometheus：

```yaml
- name: prometheus
  path: "servers/prometheus_server.py"
  port: 8095
  enabled: true
  env:
    PROMETHEUS_URL: "http://observability-prometheus.xnet.svc:9090"
```

这里的 `PROMETHEUS_URL` 是 Prometheus 服务地址，不是 AIOps 连接 MCP 的 SSE 地址。

Kubernetes Service DNS 常用格式：

```text
http://<service-name>.<service-namespace>.svc:<service-port>
```

因此，如果 Prometheus 服务名仍是 `observability-prometheus`，但部署 namespace 从 `xnet` 改为 `monitor`，必须改成：

```yaml
env:
  PROMETHEUS_URL: "http://observability-prometheus.monitor.svc:9090"
```

如果服务名也变了，则同时修改 service name，例如：

```yaml
env:
  PROMETHEUS_URL: "http://prometheus-k8s.monitor.svc:9090"
```

### namespace 变化时改哪里

| 变化场景 | 需要修改 |
|---|---|
| 只有 Prometheus 从 `xnet` 移到 `monitor` | 只改 `deploy/configmap.yaml` 中 `PROMETHEUS_URL` 为 `http://observability-prometheus.monitor.svc:9090`，然后 `make deploy` |
| 只有 MCP Server Manager 从 `mcp` 移到 `monitor` | 改 `deploy/*.yaml` 和 `Makefile` 中本项目 namespace；AIOps `mcp_servers.*.config.url` 改为 `http://mcp-server-manager.monitor:<port>/sse` |
| Prometheus 和 MCP Server Manager 都移到 `monitor` | 两边都改：`PROMETHEUS_URL` 使用 `observability-prometheus.monitor.svc`，AIOps SSE URL 使用 `mcp-server-manager.monitor` |

同理，Elasticsearch、Runbook 目录、Bash 安全策略等依赖也都通过各 MCP 的 `env` 独立配置：

| MCP | 常见环境变量 |
|---|---|
| `prometheus` | `PROMETHEUS_URL`, `PROMETHEUS_TIMEOUT` |
| `elasticsearch` | `ES_URL`, `ES_USERNAME`, `ES_PASSWORD`, `ES_SSL_SKIP_VERIFY` |
| `runbook` | `RUNBOOK_SEARCH_PATH` |
| `bash_command` | `BASH_BLOCKED_COMMANDS`, `BASH_BLOCKED_PATTERNS`, `BASH_TOOL_UNSAFE_ALLOW_ALL` |

---

## 验证方式

列出当前配置：

```bash
python3 start.py --list
```

本地启动后，用测试客户端连接某个 SSE 端点：

```bash
python3 mcp_client.py http://localhost:8095/sse
```

K8s 部署后，在集群内测试 Service：

```bash
kubectl run curl-test --rm -it --image=curlimages/curl -n mcp -- \
  curl -s -o /dev/null -w "%{http_code}" http://mcp-server-manager:8095/sse
```

如果本项目部署在 `monitor` namespace，把命令里的 `-n mcp` 改成 `-n monitor`。

Prometheus MCP 还需要确认 `PROMETHEUS_URL` 在 Pod 内可访问：

```bash
kubectl exec -n mcp deployment/mcp-server-manager -- \
  curl -s http://observability-prometheus.xnet.svc:9090/-/ready
```

如果 Prometheus namespace 已变为 `monitor`：

```bash
kubectl exec -n mcp deployment/mcp-server-manager -- \
  curl -s http://observability-prometheus.monitor.svc:9090/-/ready
```

---

## Makefile 命令

| 命令 | 说明 |
|---|---|
| `make build` / `make push` / `make build-push` | 构建、推送镜像 |
| `make deploy` | 部署到 K8s：apply、滚动重启、等待就绪，失败自动回滚 |
| `make deploy-mc` | 部署到多集群中心（需先在 Agent 仓库 `make mc-init`） |
| `make reload` / `make restart` | 应用 ConfigMap 并重启 / 仅重启 |
| `make delete` | 删除部署资源，保留 namespace |
| `make status` / `make logs` | 查看资源 / 日志 |
| `make run` / `make run-local` / `make list` | 本地运行默认配置 / 优先读 `config/mcp_config.local.yaml` / 列出配置的 MCP |
| `make check` / `make test-unit` / `make lint` / `make format` | 语法 + 离线测试 / 仅离线测试 / ruff 检查 / 格式化 |
| `make test` | 用 `mcp_client.py` 测试运行中的 SSE 服务 |
| `make sync-aiops-edge` | 从 Agent 仓库同步 `servers/aiops_edge/` |

---

## 常见问题

| 现象 | 常见原因 | 处理 |
|---|---|---|
| AIOps 显示 0 个工具 | `mcp_servers.*.config.url` 的 namespace/port 错误，或 AIOps 未重载 | 核对 `mcp-server-manager.<namespace>:<port>/sse`，重启或重载 AIOps |
| K8s 内连不上 `/sse` | ConfigMap 端口、Deployment containerPort、Service port 不一致 | 按“K8s 端口同步规则”检查四处配置 |
| Prometheus 查询失败 | `PROMETHEUS_URL` 指向了错误 namespace/service/port，或 NetworkPolicy/RBAC 限制 | 在 Pod 内 curl Prometheus ready 接口，确认 `http://<service>.<namespace>.svc:<port>` 可达 |
| 本地启动失败 | Python/Node 依赖缺失，端口占用，server `path` 写错 | 安装 `requirements.txt`，检查 `node`/`mcp-proxy`，运行 `python3 start.py --list` |
| 只改配置后未生效 | ConfigMap 已改但 Pod 没重启 | 执行 `make deploy`（会滚动重启）或 `make reload` |

更多添加新 MCP 的步骤见 [docs/EXTENDING.md](docs/EXTENDING.md)。

## License

项目自有代码采用 [Apache License 2.0](LICENSE)，允许商业使用。
第三方代码与依赖保留各自的许可证及署名，不由本项目重新授权。
注明移植自 Holmes 的模块仍需核实上游版本和许可材料，
不能仅凭本仓库 LICENSE 认定其第三方分发合规已完成。
