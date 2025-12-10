# MCP Server Manager

将 MCP 工具转换为 SSE 端点，供 HolmesGPT 等服务集成使用。
一句话说明如何扩展使用新的mcp工具并且配置运行
1. 写自己的mcp service 并且把它放在/servers下 或者使用第三方的mcp工具如npm的或者uv的或者golang的
2. 将写好的文件写入到配置configmap.yaml里，里面定义好需要暴露的端口，
3. 同步修改svc和deployment中关于port的暴露端口
4. 编译镜像保存运行。
5. 修改aiops的项目中的config.yaml文件,将新添加的服务填入aiops的配置configmap中。
```
      elasticsearch:
        description: "Elasticsearch MCP 工具集 - 用于查询索引、搜索日志数据"
        config:
          url: "http://mcp-server-manager.mcp:8088/sse"
          mode: "sse"
        enabled: true
```
6. 重新加载aiops的configmap。让agent服务重新运行加载新的工具集合。

## 目录结构

```
mcpstander/
├── start.py             # 启动器
├── mcp_config.yaml      # 本地配置文件 (开发用)
├── mcp_client.py        # 测试客户端
├── servers/             # 本地自定义 MCP 工具
│   └── test_server.py
├── deploy/              # K8s 部署文件
│   ├── namespace.yaml
│   ├── configmap.yaml   # 配置文件 (生产用)
│   ├── deployment.yaml
│   └── service.yaml
├── Dockerfile
├── Makefile
└── VERSION
```

## 快速开始

### 本地运行

```bash
pip install -r requirements.txt
python start.py
```

### K8s 部署

```bash
# 构建并推送镜像
make build-push

# 部署
make deploy

# 查看状态
make status
```

## 配置说明

编辑 `deploy/configmap.yaml` 配置 MCP 工具：

```yaml
data:
  mcp_config.yaml: |
    # 第三方 npm 包
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

    # 本地自定义工具
    basicmcp:
      - name: test-tools
        path: "servers/test_server.py"
        port: 8091
        enabled: true
```

## HolmesGPT 集成

### SSE 端点 URL 格式

```
http://mcp-server-manager.<namespace>:<port>/sse
```

### 配置示例

在 HolmesGPT 或其他服务的配置中添加：

```yaml
mcp_servers:
  elasticsearch:
    description: "Elasticsearch MCP - 查询索引、搜索日志"
    config:
      url: "http://mcp-server-manager.mcp:8088/sse"
      mode: "sse"
    enabled: true
  
  test-tools:
    description: "测试工具集 - MCP 集成测试"
    config:
      url: "http://mcp-server-manager.mcp:8091/sse"
      mode: "sse"
    llm_instructions: "只有当用户需要测试的时候才运行"
    enabled: true
```

### 端口对应关系

| MCP 服务 | ConfigMap 中的 port | SSE URL |
|----------|---------------------|---------|
| elasticsearch | 8088 | `http://mcp-server-manager.mcp:8088/sse` |
| test-tools | 8091 | `http://mcp-server-manager.mcp:8091/sse` |

> **注意**: `.mcp` 是 namespace 名称，如果部署在其他 namespace，请相应修改。

## Makefile 命令

| 命令 | 说明 |
|------|------|
| `make build` | 构建镜像 |
| `make push` | 推送镜像 |
| `make build-push` | 构建并推送 |
| `make deploy` | 部署到 K8s |
| `make undeploy` | 卸载部署 |
| `make delete` | 删除所有资源（包括 namespace） |
| `make reload` | 更新配置并重启 |
| `make status` | 查看状态 |
| `make logs` | 查看日志 |
| `make restart` | 重启服务 |

## 扩展新工具

### 添加第三方 npm 包

在 `deploy/configmap.yaml` 的 `customermcp` 部分添加：

```yaml
- name: github
  type: npm
  package: "@modelcontextprotocol/server-github"
  port: 8089
  enabled: true
  env:
    GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_xxx"
```

### 添加本地自定义工具

1. 在 `servers/` 下创建 Python 文件
2. 在 `deploy/configmap.yaml` 的 `basicmcp` 部分添加配置
3. 重新构建镜像：`make build-push`
4. 重启服务：`make restart`

## 注意事项

1. **端口同步**: 修改 ConfigMap 中的端口后，需要同步修改 `deployment.yaml` 和 `service.yaml`
2. **镜像更新**: 修改 `start.py` 或 `servers/` 下的代码后，需要重新构建镜像
3. **配置更新**: 只修改 ConfigMap 时，执行 `make reload` 即可
