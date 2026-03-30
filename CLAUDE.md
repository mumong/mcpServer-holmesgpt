# CLAUDE.md

本文档为 Claude Code (claude.ai/code) 在此代码库中工作提供指导。

---

## 项目概述

**MCP Server Manager** - 统一管理 MCP (Model Context Protocol) 服务器的工具，将其转换为 SSE (Server-Sent Events) 端点，供 HolmesGPT 等 AI 服务集成使用。

---

## 代码设计原则

本项目遵循**高内聚、低耦合、易扩展**的设计理念，通过模块化架构简化实现复杂度：

### 核心设计原则

| 原则 | 体现 | 说明 |
|------|------|------|
| **高内聚** | 单一职责 | 每个模块专注于一个特定功能领域 |
| **低耦合** | 接口抽象 | 模块间通过简单接口通信，内部实现可独立变化 |
| **易扩展** | 配置驱动 | 新增服务类型只需修改配置，无需改动核心逻辑 |
| **简化复杂度** | 统一封装 | 使用 `supergateway` 统一处理 stdio → SSE 转换 |

### 模块职责划分

| 模块 | 职责 | 设计要点 |
|------|------|----------|
| `manager.py` | `MCPServerManager` - 配置加载、服务启停、进程监控 | 高内聚：生命周期管理集中在一处 |
| `config.py` | 数据类 + YAML 解析 | 单一职责：配置解析与业务逻辑完全分离 |
| `launchers.py` | 命令构建、进程启动（supergateway 封装） | 低耦合：启动逻辑独立，易于替换实现 |
| `cli.py` | 参数解析、入口点 | 关注点分离：命令行交互与核心逻辑解耦 |
| `paths.py` | 本地脚本路径解析 | 封装细节：路径查找逻辑内部化 |

### 扩展新服务类型的步骤（体现设计优势）

若需支持新的服务类型（如 golang 包）：

1. 在 `config.py` 中添加新的数据类（如 `GolangServerSpec`）
2. 在 `launchers.py` 中添加命令构建函数（如 `build_inner_cmd_for_golang()`）
3. 在 `manager.py` 的 `_start_third_party()` 中添加类型分支

**核心逻辑无需改动**，体现了开闭原则（对扩展开放，对修改封闭）。

---

## 架构说明

### 支持的 MCP 服务类型

1. **npm 包** - 通过 `npx -y <package>` 启动，`supergateway` 包装
2. **uv 包** - Python 包，通过 `uv --directory <dir> run <package>` 启动
3. **本地自定义服务器** - `servers/` 目录下的 Python 脚本

所有类型统一通过 `supergateway` 暴露 SSE 端点：`http://localhost:<port>/sse`

**设计优势**：通过 `supergateway` 统一协议转换，避免了为每种服务类型分别实现网络层的复杂性。

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
   - `mcp_config.yaml`（本地开发用）
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

管理器自动设置 `NODE_TLS_REJECT_UNAUTHORIZED=0`，以避免 Node.js HTTPS 请求时的自签名证书问题。

---

## 当前已配置的工具

| 名称 | 类型 | 端口 | 路径/包 |
|------|------|------|---------|
| elasticsearch | npm | 8088 | `@elastic/mcp-server-elasticsearch@0.3.1` |
| test-tools | 本地 | 8091 | `servers/test_server.py` |
| helm-tools | 本地 | 8092 | `servers/helm_server.py` |

---

## 消费者 SSE URL 格式

```
http://mcp-server-manager.mcp:<port>/sse
```

`mcp` 为 namespace 名称，根据实际部署配置可能有所不同。
