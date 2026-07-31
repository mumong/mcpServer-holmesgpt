# AIOps Pod 自主查询与 Canonical Fact Ledger

`aiops-observability-query` 是面向 Agent 的通用 Pod 可观测性 MCP，通过
`8100/sse` 暴露 Metrics、Logging、Tracing 和 Topology 查询。它不按故障类型
选择查询：模型提供诊断目的和受约束的查询参数，MCP 只负责作用域、
执行、有界投影和事实发布。

## 工具

| 工具 | 数据源 | 主要边界 |
|---|---|---|
| `execute_pod_promql` | Prometheus | 每个 vector selector 必须绑定精确 namespace/Pod |
| `query_pod_logs` | Elasticsearch/Filebeat | 服务端构造安全 DSL，优先使用 Pod UID |
| `query_pod_tracing` | DeepFlow/Tempo | Pod IP 作用域，flow 与 application span 分开表达 |
| `query_pod_topology` | Kubernetes API | 只发布 owner、selector、node 和 container 的来源支持关系 |

所有工具都接受 `namespace`、`pod`、`purpose` 和可选 `pod_uid`。时间窗最长
120 分钟，输入和输出均有硬预算。

## 结果合同

单工具 Agent-facing JSON 最大 6144 bytes，包含：

- 精确实体身份和查询描述；
- `present/empty/absent/weak/error` coverage；
- 有界 facts、samples 或 source-backed relationships；
- 可展开的 evidence refs；
- 从最终有界结果重建的 `fact_ledger`。

`fact_ledger` 使用 `contract_version=aiops.fact-ledger.v1`、
`source=mcp_canonical`、`legacy_contract=false`。每条 FactRecord 的 ID 由标准 JSON
确定性计算，排序和预算裁剪也是稳定的。

## 信任和安全边界

- Ledger 不包含 diagnosis、root cause、remediation、ground truth 或 evaluator label。
- FactRecord 必须保留 source system、dimension、directness、confidence 和 evidence refs。
- Pod UID 不匹配、查询作用域不完整或关系无来源时 fail closed。
- 输出裁剪后重建 Ledger，避免 Fact 引用指向已被删除的内容。
- Metrics 采样、Trace 代表性、数据源空结果和拓扑边均保留限制语义。

## 本地验证

```bash
python -m unittest \
  tests.test_aiops_observability_query_fact_ledger \
  tests.test_aiops_observability_query_contract \
  tests.test_aiops_observability_query_metrics \
  tests.test_aiops_observability_query_logs \
  tests.test_aiops_observability_query_tracing \
  tests.test_aiops_observability_query_topology -v
```

列出服务并连接 SSE：

```bash
python3 start.py --list
python3 mcp_client.py http://localhost:8100/sse
```
