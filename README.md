# MCP Server Manager
![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)

[English](README.en.md) | 简体中文

## 目录
- [项目概述](#项目概述)
- [核心功能](#核心功能)
- [快速开始](#快速开始)
- [架构设计](#架构设计)
- [文档](#文档)
- [许可证](#许可证)

## 项目概述
K8s AIOps Copilot 的工具服务：把多个 stdio MCP Server 统一拉起，每个暴露成一个 HTTP SSE 端点，供 AIOps Agent 通过 `mcp_servers` 接入。
- 一个 Pod、一份配置管理全部 MCP
- 单集群直接访问本集群；多集群时 kubectl 经 xnet 隧道交给边缘 runner 执行
- bash / kubectl 等执行类工具受高危命令规则约束，修复动作由 Agent 审批后执行

## 核心功能
### 内置工具
✔️ Kubernetes：get / describe / events / logs / top / 对象关系（`k8s-core`，8093）  
✔️ 指标、日志、链路：PromQL 与 Pod 级观测查询（`prometheus` 8095、`aiops-observability-query` 8100）  
✔️ Helm、受控 bash、连通性检查、Runbook 检索等（端口 8088–8100，见 `deploy/configmap.yaml`）  

### 多集群
⚡ 每个工具多一个 `cluster` 参数，`list_clusters` 返回集群和在线状态  
⚡ 指标和日志查中心的 VictoriaMetrics / VictoriaLogs，按 `cluster` 标签过滤  
⚡ kubectl 由 `bin/kubectl` 替身经 xnet 转发到边缘 runner；helm 只在本集群  

## 快速开始
### 环境要求
- Kubernetes 集群，部署机有 `make`、`kubectl`
- 镜像已发布在 `xnet.registry.io:8443`，无需构建
- 三个服务（MCP、Agent、前端）一起部署的步骤见 Agent 仓库 README「快速开始」

### 部署
```bash
make deploy        # 单集群：apply + 滚动重启 + 等待就绪，失败自动回滚
make deploy-mc     # 多集群中心（先在 Agent 仓库执行 make mc-init）
make status        # 查看 Pod / Service / ConfigMap
make delete        # 卸载（保留命名空间）
```

### 修改配置或升级
```bash
vi deploy/configmap.yaml && make deploy                # 改工具地址、开关等配置
echo <新版本号> > VERSION && make build push deploy    # 改了代码
```

### 本地运行
```bash
python3 -m pip install -r requirements.txt
make run           # 或 make run-local（读取 config/mcp_config.local.yaml）
make check         # 语法检查 + 离线单元测试
```

## 架构设计
```
AIOps Agent ──SSE──> mcp-server-manager:<端口>/sse ──> mcp-proxy ──> stdio MCP Server ──> K8s API / Prometheus / VictoriaMetrics …
                                                                    └─(多集群) bin/kubectl ──xnet 隧道──> 边缘 runner
```

## 文档
| 文档 | 内容 |
|---|---|
| [使用与运维](docs/GUIDE.md) | 配置方式、端口同步、namespace 变更、AIOps 接入、验证、Makefile、常见问题 |
| [扩展新 MCP](docs/EXTENDING.md) | 添加第三方或本地工具 |
| [Pod 观测查询](docs/AIOPS_OBSERVABILITY_QUERY.md) | 查询工具与 Fact Ledger 合同 |

## 许可证
[Apache License 2.0](LICENSE)。第三方代码保留各自的许可证。
