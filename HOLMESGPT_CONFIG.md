# HolmesGPT 集成配置

## 配置方式

在 HolmesGPT 的 `config.yaml` 中添加 MCP Server：

```yaml
mcp_servers:
  <服务名>:
    description: "<描述>"
    config:
      url: "http://localhost:<端口>/sse"
      mode: "sse"
    enabled: true
```

## 配置示例

根据 `mcp_config.yaml` 中启用的服务，添加对应的 SSE 端点：

```yaml
mcp_servers:
  # Elasticsearch (端口 8082)
  elasticsearch:
    description: "Elasticsearch - 查询索引、搜索日志"
    config:
      url: "http://localhost:8082/sse"
      mode: "sse"
    enabled: true

  # 本地测试工具 (端口 8090)
  test-tools:
    description: "本地工具 - 时间、计算器等"
    config:
      url: "http://localhost:8090/sse"
      mode: "sse"
    enabled: true
```

## 使用流程

1. 编辑 `mcp_config.yaml` 配置工具
2. 运行 `python start.py` 启动服务
3. 在 HolmesGPT 配置中添加对应的 SSE URL
4. HolmesGPT 即可调用这些工具
