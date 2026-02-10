# 项目结构说明

## 目录结构

```
mcpstander/
├── start.py              # 主启动程序（核心）
├── mcp_sse_bridge.py     # SSE 桥接器（核心）
├── mcp_config.yaml       # 服务配置文件
├── requirements.txt      # Python 依赖
├── Dockerfile            # Docker 镜像构建
├── Makefile              # 便捷命令
├── VERSION               # 版本号
├── README.md             # 项目简介
├── .gitignore            # Git 忽略规则
│
├── servers/              # 本地 MCP 服务器
│   ├── test_server.py   # 测试工具服务
│   └── helm_server.py   # Helm 管理服务
│
├── deploy/               # Kubernetes 部署配置
│   ├── namespace.yaml   # 命名空间
│   ├── rbac.yaml        # 权限配置
│   ├── configmap.yaml   # MCP 配置
│   ├── nginx-configmap.yaml  # Nginx 配置
│   ├── deployment.yaml  # Pod 部署
│   ├── service.yaml     # Service 配置
│   └── ingress.yaml     # Ingress 配置
│
├── tools/                # 开发和测试工具
│   ├── mcp_client.py    # MCP 测试客户端
│   └── test-deployment.sh  # 部署验证脚本
│
└── docs/                 # 文档
    └── GUIDE.md         # 完整使用指南
```

## 核心文件说明

### start.py
- **作用**: 统一管理和启动所有 MCP 服务
- **特点**: 低耦合，支持 npm 包和本地 Python 服务
- **扩展**: 通过 `mcp_config.yaml` 添加新服务

### mcp_sse_bridge.py
- **作用**: 将 stdio MCP 服务转换为 SSE 传输
- **特点**: 完全模块化，支持 0.0.0.0 监听
- **扩展**: 可独立使用，支持自定义配置

### mcp_config.yaml
- **作用**: 定义所有 MCP 服务的配置
- **结构**:
  - `customermcp`: NPM 包服务
  - `basicmcp`: 本地 Python 服务

## 设计原则

1. **低耦合**: 每个模块职责单一，互不依赖
2. **高内聚**: 相关功能集中在一起
3. **易扩展**: 添加新服务只需修改配置文件
4. **保持简洁**: 避免过度设计，保持代码清晰

## 部署方式

### 本地开发
```bash
python3 start.py
```

### Kubernetes
```bash
make build-push deploy
```

## 添加新服务

只需编辑 `mcp_config.yaml`:

```yaml
customermcp:
  - name: new-service
    package: "npm-package-name"
    port: 8094
    enabled: true
```

然后重启或重新部署即可。

## 修改记录

### v3.0.0
- ✅ 简化项目结构
- ✅ 修改 imagePullPolicy 为 IfNotPresent
- ✅ 合并冗余文档为单一 GUIDE.md
- ✅ 删除不必要的配置示例
- ✅ 保持核心功能完整
- ✅ 低耦合、高内聚、易扩展
