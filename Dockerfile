# MCP Server 统一管理工具
FROM python:3.11-slim

LABEL maintainer="huhu"
LABEL description="MCP Server Manager - 将 MCP 工具转换为 SSE 端点"

# 构建参数
ARG VERSION=1.0.0

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VERSION=${VERSION}

# 安装 Node.js (使用 apt 源，不需要外网)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    npm \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY start.py .
COPY mcp_client.py .
COPY servers/ ./servers/

# 配置文件通过 ConfigMap 挂载到 /app/config/mcp_config.yaml
ENV MCP_CONFIG_PATH=/app/config/mcp_config.yaml

# 暴露端口范围
EXPOSE 8080-8099

# 启动命令
CMD ["python", "start.py", "--config", "/app/config/mcp_config.yaml"]
