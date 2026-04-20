## Context

`holmes_tools_server.py` 采用模块化聚合架构：每个工具模块暴露 `TOOLS` 列表和 `call_tool()` 函数，Server 通过 `_MODULES` 列表统一注册。当前已有 internet、connectivity、prometheus 三个模块运行稳定。

Holmes 原生的 `core_investigation`（TodoWrite）和 `runbook`（fetch_runbook）已在 `holmes/mcp/tools/` 下有独立实现，但未集成到本项目的 MCP Server 中。

## Goals / Non-Goals

**Goals:**
- 将 TodoWrite 和 fetch_runbook 以独立模块形式集成到 `holmes_tools_server.py`
- 复用 `holmes/mcp/tools/` 中已验证的核心逻辑，适配本项目的模块接口规范
- 保持零侵入：不修改现有三个模块的任何代码

**Non-Goals:**
- 不实现 Holmes 原生的 Toolset/Tool 类继承体系（本项目用轻量模块模式）
- 不支持 Supabase DAL 方式的 Runbook 获取（仅文件系统）
- 不修改 K8s 部署清单（复用现有端口）

## Decisions

### D1: 从 holmes/mcp/tools/ 移植而非直接 import

**选择**: 在 `servers/holmes_tools/` 下创建独立模块，移植核心逻辑
**理由**: holmes 子模块依赖 `mcp.types.Tool`，而本项目的模块也用同一类型，但 import 路径和 `sys.path` 不同。独立模块避免子模块路径耦合，且可针对本项目需求微调（如日志格式）。
**替代方案**: 直接 `from holmes.mcp.tools import core_investigation_tools` — 被否决，因为子模块路径不在 `sys.path` 中，且会引入不必要的耦合。

### D2: 模块命名

**选择**: `core_investigation.py` 和 `runbook.py`
**理由**: 与 Holmes 原生 toolset 名称一致，便于溯源。

### D3: Runbook 搜索路径配置

**选择**: 复用 `RUNBOOK_SEARCH_PATH` 环境变量
**理由**: 与 Holmes 原生行为一致，K8s 部署时可通过 ConfigMap 注入。

## Risks / Trade-offs

- **[风险] 原始实现与移植版本分叉** → 两个文件逻辑简单（各 <100 行），分叉风险低。在模块头部注释标注来源版本。
- **[风险] RUNBOOK_SEARCH_PATH 未配置时 fetch_runbook 无法找到文件** → 默认回退到当前工作目录，与 Holmes 行为一致。
- **[权衡] 不支持 Supabase Runbook** → 当前部署场景仅用文件系统，Supabase 支持可后续按需添加。
