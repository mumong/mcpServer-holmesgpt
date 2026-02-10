# MCP Server Manager 使用指南

## 架构说明

**简单直接**：每个 MCP 服务运行独立的 supergateway 实例

```
start.py
  ├─ supergateway (8082) -> npm: elasticsearch
  ├─ supergateway (8090) -> python: test_server.py
  ├─ supergateway (8092) -> python: helm_server.py
  └─ supergateway (8093) -> npm: kubernetes
```

**优势**：
- ✅ 使用标准 supergateway（HolmesGPT 完全兼容）
- ✅ 每个服务独立，互不干扰
- ✅ 配置简单，易于维护

---

## 本地开发

### 安装

```bash
pip install -r requirements.txt
```

### 启动

```bash
python3 start.py
```

### 测试

```bash
python3 tools/mcp_client.py http://localhost:8082/sse
```

### 添加新服务

编辑 `mcp_config.yaml`:

```yaml
customermcp:
  - name: new-service
    package: "npm-package-name"
    port: 8094
    enabled: true
```

---

## Kubernetes 部署

### 构建和部署

```bash
# 构建镜像
make build-push

# 部署
make deploy

# 查看状态
make status

# 查看日志
make logs
```

### aiops 配置

```yaml
# config.yaml 或 ConfigMap
mcp_servers:
  - name: elasticsearch
    url: http://mcp-server-manager.mcp.svc.cluster.local:8082/sse
    enabled: true
    
  - name: test-tools
    url: http://mcp-server-manager.mcp.svc.cluster.local:8090/sse
    enabled: true
    
  - name: helm-tools
    url: http://mcp-server-manager.mcp.svc.cluster.local:8092/sse
    enabled: true
    
  - name: kubernetes
    url: http://mcp-server-manager.mcp.svc.cluster.local:8093/sse
    enabled: true
```

---

## 跨集群访问

### 方式 1: LoadBalancer（推荐）

```bash
# 修改 Service 类型
kubectl patch svc mcp-server-manager -n mcp -p '{"spec":{"type":"LoadBalancer"}}'

# 获取外部 IP
EXTERNAL_IP=$(kubectl get svc mcp-server-manager -n mcp -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
echo $EXTERNAL_IP
```

**aiops 配置**：

```yaml
mcp_servers:
  - name: elasticsearch-cluster-a
    url: http://<EXTERNAL_IP>:8082/sse
```

### 方式 2: Ingress

```bash
# 编辑域名
vim deploy/ingress.yaml

# 部署
kubectl apply -f deploy/ingress.yaml
```

**aiops 配置**：

```yaml
mcp_servers:
  - name: elasticsearch-cluster-a
    url: http://mcp-cluster-a.example.com/elasticsearch/sse
```

---

## 故障排查

### Pod 启动失败

```bash
kubectl describe pod -n mcp -l app=mcp-server-manager
kubectl logs -n mcp -l app=mcp-server-manager
```

### aiops 连接失败

```bash
# 1. 测试网络
kubectl exec -n aiops -it <aiops-pod> -- \
  curl -v http://mcp-server-manager.mcp.svc.cluster.local:8082/sse

# 2. 查看 mcp 日志
kubectl logs -n mcp -l app=mcp-server-manager | grep "Supergateway\|Error"

# 3. 检查 Service
kubectl get svc,endpoints -n mcp
```

### 工具显示 0 个

**原因**：supergateway 不支持多连接

**解决**：确保 aiops 不要同时打开多个连接到同一个服务

---

## 配置更新

```bash
# 1. 编辑配置
vim deploy/configmap.yaml

# 2. 应用并重启
make reload
```

---

## 端口说明

| 服务 | 端口 | 类型 |
|------|------|------|
| Elasticsearch MCP | 8082 | NPM 包 |
| Test Tools MCP | 8090 | Python |
| Helm Tools MCP | 8092 | Python |
| Kubernetes MCP | 8093 | NPM 包 |

---

## 常见命令

```bash
# 本地
python3 start.py                                    # 启动
python3 tools/mcp_client.py http://localhost:8082/sse  # 测试

# Kubernetes
make build-push deploy   # 构建+推送+部署
make status              # 查看状态
make logs                # 查看日志
make restart             # 重启
make delete              # 删除
```

---

## 版本

当前：**3.1.0** - 简化架构，使用标准 supergateway
