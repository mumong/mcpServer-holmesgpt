# 部署指南

## 本地部署

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动服务
python3 start.py
```

## Kubernetes 部署

### 快速部署

```bash
make build-push deploy
```

### 分步部署

```bash
# 1. 构建镜像
make build

# 2. 推送镜像
make push

# 3. 部署到 K8s
make deploy
```

### 验证部署

```bash
# 查看状态
kubectl get pods,svc -n mcp

# 查看日志
kubectl logs -n mcp -l app=mcp-server-manager -f

# 测试连接
kubectl run test --rm -it --image=curlimages/curl -n mcp -- \
  curl http://mcp-server-manager:8082/sse
```

## aiops 配置

### 集群内访问

```yaml
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

### 跨集群访问

#### LoadBalancer 方式

```bash
# 修改 Service 类型
kubectl patch svc mcp-server-manager -n mcp -p '{"spec":{"type":"LoadBalancer"}}'

# 获取外部 IP
kubectl get svc mcp-server-manager -n mcp
```

**aiops 配置**：

```yaml
mcp_servers:
  - name: elasticsearch
    url: http://<EXTERNAL_IP>:8082/sse
```

---

## 故障排查

### Pod 启动失败

```bash
kubectl describe pod -n mcp -l app=mcp-server-manager
kubectl logs -n mcp -l app=mcp-server-manager
```

### aiops 显示 0 个工具

**检查网络**：

```bash
kubectl exec -n aiops -it <aiops-pod> -- \
  curl -v http://mcp-server-manager.mcp.svc.cluster.local:8082/sse
```

**检查日志**：

```bash
kubectl logs -n mcp -l app=mcp-server-manager | grep "supergateway\|Error"
```

---

## 常用命令

```bash
make build-push    # 构建并推送
make deploy        # 部署
make status        # 状态
make logs          # 日志
make restart       # 重启
make reload        # 更新配置
make delete        # 删除
```
