## 1. Core Investigation (TodoWrite) 模块

- [x] 1.1 创建 `servers/holmes_tools/core_investigation.py`，移植 `holmes/mcp/tools/core_investigation_tools.py` 的核心逻辑（`_format_tasks`、`_run_todo_write`、`TOOLS`、`call_tool`）
- [x] 1.2 验证 TodoWrite 工具的 inputSchema 定义正确（todos 数组，含 id/content/status 字段）

## 2. Runbook (fetch_runbook) 模块

- [x] 2.1 创建 `servers/holmes_tools/runbook.py`，移植 `holmes/mcp/tools/runbook_tools.py` 的核心逻辑（`_get_search_paths`、`_resolve_runbook_path`、`_run_fetch_runbook`、`TOOLS`、`call_tool`）
- [x] 2.2 验证路径遍历防护逻辑（`realpath` 检查）正确工作
- [x] 2.3 验证 `RUNBOOK_SEARCH_PATH` 环境变量解析（逗号/冒号分隔、目录存在性检查）

## 3. Server 注册

- [x] 3.1 修改 `servers/holmes_tools_server.py`，import `core_investigation` 和 `runbook` 模块并添加到 `_MODULES` 列表
- [x] 3.2 验证 `list_tools` 返回包含 `TodoWrite` 和 `fetch_runbook` 两个新工具

## 4. 独立 Server + 部署配置

- [x] 4.1 创建 `servers/core_investigation_server.py`（独立 server，端口 8098）
- [x] 4.2 创建 `servers/runbook_server.py`（独立 server，端口 8099）
- [x] 4.3 更新 `deploy/configmap.yaml` 添加 core-investigation 和 runbook 配置
- [x] 4.4 更新 `deploy/deployment.yaml` 添加 containerPort 8098、8099
- [x] 4.5 更新 `deploy/service.yaml` 添加 port 8098、8099
- [x] 4.6 验证三文件端口一致性

## 5. 集成验证

- [x] 5.1 独立 server import 无错误
- [x] 5.2 TodoWrite 工具调用返回格式正确（含状态图标、计数）
- [x] 5.3 fetch_runbook 工具调用返回 runbook 内容（含 `<runbook>` 标签）
- [x] 5.4 路径遍历防护验证通过
- [x] 5.5 现有工具回归测试通过（connectivity、internet、prometheus）
