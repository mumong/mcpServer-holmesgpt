# MCP Server 统一管理工具
FROM python:3.11-slim

LABEL maintainer="huhu"
LABEL description="MCP Server Manager - 将 MCP 工具转换为 SSE 端点"

# 构建参数
ARG KUBECTL_VERSION=v1.29.15
ARG HELM_VERSION=v3.14.4
ARG MCP_PROXY_VERSION=6.4.5

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 安装 Node.js 和必要工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    npm \
    curl \
    jq \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 预装 mcp-proxy，避免运行时多进程并发 npx 安装导致缓存冲突/损坏
RUN npm_config_fetch_timeout=30000 npm_config_fetch_retries=1 npm install -g mcp-proxy@${MCP_PROXY_VERSION}

# 安装 kubectl（K8s core MCP 工具依赖）
RUN curl -LO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/kubectl.real \
    && kubectl.real version --client

# 安装 Helm 并预配置常用仓库
RUN curl -fsSL https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz | tar -xzf - \
    && mv linux-amd64/helm /usr/local/bin/helm \
    && rm -rf linux-amd64 \
    && helm version --short \
    && helm repo add bitnami https://charts.bitnami.com/bitnami \
    && helm repo add stable https://charts.helm.sh/stable \
    && helm repo update

WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir --upgrade pip==26.2.0 setuptools==83.0.0 wheel==0.46.2
RUN pip install --no-cache-dir -r requirements.txt

# 发布版本只影响元数据和代码层，不使依赖安装层失效。
ARG VERSION=1.0.0
ENV VERSION=${VERSION}

# 复制项目文件
COPY start.py launch_config.py ./
COPY mcp_client.py .
COPY servers/ ./servers/
# kubectl shim (specs/008-multi-cluster): runs kubectl.real unless AIOPS_CLUSTER names an edge cluster.
COPY bin/kubectl /usr/local/bin/kubectl

# 配置文件通过 ConfigMap 挂载到 /app/config/mcp_config.yaml
ENV MCP_CONFIG_PATH=/app/config/mcp_config.yaml

# 暴露端口范围
EXPOSE 8080-8100

# 启动命令
CMD ["python", "start.py", "--config", "/app/config/mcp_config.yaml"]
