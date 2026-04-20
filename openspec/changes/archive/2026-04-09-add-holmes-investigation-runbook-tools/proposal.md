## Why

当前 `holmes_tools_server.py` 聚合了 internet、connectivity、prometheus 三类工具，但 HolmesGPT 原生的 `core_investigation`（TodoWrite 任务分解）和 `runbook`（fetch_runbook 知识库获取）两个关键 toolset 尚未纳入 MCP Server Manager。这两个工具对 AI Agent 的调查规划和知识库驱动排障至关重要，缺失它们会导致 AIOps Copilot 无法进行结构化任务分解和 Runbook 引导式诊断。

## What Changes

- 在 `servers/holmes_tools/` 下新增 `core_investigation.py` 模块，实现 `TodoWrite` 工具（任务分解与状态追踪）
- 在 `servers/holmes_tools/` 下新增 `runbook.py` 模块，实现 `fetch_runbook` 工具（Runbook 内容获取）
- 修改 `servers/holmes_tools_server.py`，将两个新模块注册到 `_MODULES` 列表
- 两个模块遵循现有 `TOOLS` + `call_tool()` 模式，零侵入式扩展

## Capabilities

### New Capabilities
- `investigation-todo`: TodoWrite 工具 — AI Agent 将复杂问题分解为可追踪的子任务列表，支持 pending/in_progress/completed/failed 状态管理
- `runbook-fetch`: fetch_runbook 工具 — 根据 runbook_id 从配置的搜索路径读取 Runbook 内容，支持路径遍历防护和多目录搜索

### Modified Capabilities
（无，现有工具不受影响）

## Impact

- **代码变更**: 新增 2 个 Python 模块 + 修改 1 行 import/注册代码
- **端口**: 无变化，复用现有 holmes-tools 端口
- **依赖**: 无新依赖，纯 Python 标准库实现
- **环境变量**: `RUNBOOK_SEARCH_PATH`（可选，runbook 搜索路径，逗号/冒号分隔）
- **配置**: 无需修改 ConfigMap 或 K8s 清单
