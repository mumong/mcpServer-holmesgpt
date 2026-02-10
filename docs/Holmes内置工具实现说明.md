# Holmes 内置工具（Toolsets）实现说明

本文档说明 `holmes/` 目录下**内置 MCP 相关工具**的实现方式，便于对照你提到的 toolsets 配置（core_investigation、runbook、bash、kubernetes/core、helm/core 等）理解代码结构。**不修改任何 holmes 源码。**

---

## 一、整体架构

Holmes 有两套与「工具」相关的实现：

| 层级 | 位置 | 作用 |
|------|------|------|
| **内置 Toolset（主流程）** | `holmes/core/tools.py` + `holmes/plugins/toolsets/` | 配置里的 `toolsets: core_investigation: ...` 等，由 ToolsetManager 加载，供 Holmes 主流程（LLM 调用、审批、执行）使用。 |
| **MCP 导出层** | `holmes/mcp/tools/` | 当 Holmes **自身**作为 MCP Server 对外暴露时，把上述能力转成 MCP 的 `Tool` 列表和 `call_tool`，供 list_tools / tools/call 使用。 |

你配置的 `core_investigation`、`runbook`、`bash`、`kubernetes/core`、`helm/core` 等，对应的是**内置 Toolset**；若通过 MCP 暴露，则会经 **MCP 导出层** 转成 MCP 工具。

---

## 二、内置 Toolset 的加载与执行

### 2.1 配置与加载

- 配置格式（你给出的示例）：
  ```yaml
  toolsets:
    core_investigation:
      enabled: true
    runbook:
      enabled: true
    bash:
      enabled: false
    kubernetes/core:
      enabled: true
    helm/core:
      enabled: true
    # ...
  ```
- **加载入口**：`holmes/core/toolset_manager.py` 的 `ToolsetManager`。
  - `_list_all_toolsets()` 会：
    1. 调用 `load_builtin_toolsets()` 加载**内置** toolsets；
    2. 用配置里的 `toolsets` 覆盖/合并（如 enabled、config）；
    3. 再加载 custom toolsets。
  - 内置列表来自：`holmes/plugins/toolsets/__init__.py` 的 `load_builtin_toolsets()` → `load_python_toolsets()` + 所有 `plugins/toolsets/*.yaml`。

### 2.2 内置 Toolset 的两种定义方式

1. **Python 类（Toolset + Tool）**  
   在 `plugins/toolsets/` 下某目录或模块里：
   - 定义一个 **Toolset** 子类（继承 `holmes.core.tools.Toolset`），包含：`name`、`description`、`tools`（Tool 列表）、`tags`、`prerequisites`、`enabled` 等。
   - 每个 **Tool** 继承 `holmes.core.tools.Tool`，实现：
     - `_invoke(params, context) -> StructuredToolResult`
     - 可选：`requires_approval(params, context) -> Optional[ApprovalRequirement]`
     - 可选：`get_parameterized_one_liner(params)`  
   例如：`core_investigation`、`runbook`、`bash` 都是这种。

2. **YAML 定义（仅声明式，无 Python 子类）**  
   在 `plugins/toolsets/*.yaml` 里写 `toolsets: <name>: ...`，每个 tool 用 `command` 或 `script` + Jinja2 模板。  
   例如：`kubernetes/core`（`kubernetes.yaml`）、`helm/core`（`helm.yaml`）都是 YAML。

### 2.3 执行流程（主流程）

- 某次 LLM 决定调用某个 tool 时：
  1. 根据 tool 名找到对应的 **Tool** 实例；
  2. 调用 `Tool.invoke(params, context)`（见 `core/tools.py`）：
     - 先 `requires_approval()`：若返回 `ApprovalRequirement(needs_approval=True)`，则返回 `APPROVAL_REQUIRED`，不执行；
     - 否则执行 `_invoke(params, context)`；
     - 再对结果做 `_apply_transformers()`（如 llm_summarize 压缩输出）；
  3. 返回 `StructuredToolResult`（status、data、error、return_code 等）。

---

## 三、各内置 Toolset 与实现位置

### 3.1 core_investigation（任务分解与规划）

- **配置名**：`core_investigation`。
- **工具**：`TodoWrite`。
- **实现**：
  - **Toolset**：`holmes/plugins/toolsets/investigator/core_investigation.py` → `CoreInvestigationToolset`。
  - **Tool**：`TodoWriteTool`，接收 `todos`（数组，每项含 id、content、status），格式化任务列表并返回；内部用 `format_tasks`、可打日志/表格。
- **注册**：在 `plugins/toolsets/__init__.py` 的 `load_python_toolsets()` 里：`CoreInvestigationToolset()`。

### 3.2 runbook（Runbook 知识库）

- **配置名**：`runbook`。
- **工具**：`fetch_runbook`、`runbook`（若有两项则对应两个 Tool）。
- **实现**：
  - **Toolset**：`holmes/plugins/toolsets/runbook/runbook_fetcher.py` → `RunbookToolset`。
  - **Tool**：`RunbookFetcher`（name=`fetch_runbook`）：根据 `runbook_id`（UUID 或 .md 文件名）从 catalog 或本地路径取 runbook 内容；支持 Supabase DAL 和本地 `additional_search_paths`。
- **注册**：`load_python_toolsets()` 里 `RunbookToolset(dal=dal, additional_search_paths=...)`。

### 3.3 bash（Bash 执行，有安全风险）

- **配置名**：`bash`。
- **工具**：`run_bash_command`、`kubectl_run_image`。
- **实现**：
  - **Toolset**：`holmes/plugins/toolsets/bash/bash_toolset.py` → `BashExecutorToolset`。
  - **Tool**：
    - **RunBashCommand**（`run_bash_command`）：
      - **审批/安全**：`requires_approval()` 里用 `make_command_safe(command_str, self.toolset.config)` 做安全校验；不通过则返回 `ApprovalRequirement(needs_approval=True)`；若环境变量 `BASH_TOOL_UNSAFE_ALLOW_ALL` 为真则放行。
      - **执行**：未审批则再次 `make_command_safe`；通过后调用 `execute_bash_command(cmd, timeout, params)`（`plugins/toolsets/bash/common/bash.py`）：`subprocess.run(..., shell=True, executable="/bin/bash", timeout=timeout)`，stdout+stderr 拼进 `StructuredToolResult.data`。
    - **KubectlRunImageCommand**（`kubectl_run_image`）：
      - 校验 namespace 正则（`SAFE_NAMESPACE_PATTERN`）、`validate_image_and_commands()`；通过后拼出 `kubectl run <pod> --image=... --rm --attach ... -- <command>`，同样用 `execute_bash_command()` 执行。
  - **安全解析**：`make_command_safe` 在 `plugins/toolsets/bash/parse_command.py`，基于 `BashCommand` 子命令解析（kubectl、helm、jq、grep 等）与白名单/规则。
- **注册**：`load_python_toolsets()` 里 `BashExecutorToolset()`。

### 3.4 kubernetes/core（K8s 只读）

- **配置名**：`kubernetes/core`。
- **工具**：kubectl_describe、kubectl_get_by_name、kubectl_get_by_kind_in_namespace、kubectl_get_by_kind_in_cluster、kubectl_find_resource、kubectl_get_yaml、kubectl_events、kubernetes_jq_query、kubernetes_tabular_query、kubernetes_count、kubectl_top_pods、kubectl_top_nodes、get_prometheus_target、kubectl_lineage_* 等（以 YAML 为准）。
- **实现**：**纯 YAML**，无单独 Python Tool 类。  
  - 文件：`holmes/plugins/toolsets/kubernetes.yaml`，顶层 `toolsets: kubernetes/core: ...`。  
  - 每个 tool 是 `command: "kubectl ... {{ kind }} {{ name }} ..."` 形式的 Jinja2 模板，由框架解析为 `YAMLTool`（`core/tools.py`），执行时渲染后 `subprocess.run`；部分 tool 配置了 `transformers: llm_summarize` 做输出压缩。
  - **prerequisites**：如 `command: "kubectl version --client"`，用于检查是否可用。

### 3.5 helm/core（Helm 只读）

- **配置名**：`helm/core`。
- **工具**：helm_list、helm_values、helm_status、helm_history、helm_manifest、helm_hooks、helm_chart、helm_notes。
- **实现**：**纯 YAML**。  
  - 文件：`holmes/plugins/toolsets/helm.yaml`，`toolsets: helm/core: ...`。  
  - 每个 tool 为 `command: "helm ... {{ release_name }} -n {{ namespace }}"` 等模板，同样由 `YAMLTool` 执行。  
  - **prerequisites**：如 `command: "helm version"`。

---

## 四、MCP 导出层（holmes/mcp/tools/）

当 Holmes **作为 MCP Server** 对外提供工具时，不会直接暴露上述 Toolset 对象，而是通过另一套「扁平」的 MCP 工具列表：

- **入口**：`holmes/mcp/tools/registry.py`。
  - `get_all_tools()`：遍历各模块的 `TOOLS`（`mcp.types.Tool` 列表），合并后供 MCP 的 `list_tools` 使用。
  - `call_tool(name, arguments)`：按顺序在各模块上调用 `call_tool(name, arguments)`，返回第一个非 None 的字符串结果。
- **模块**：  
  `core_investigation_tools`、`runbook_tools`、`bash_tools`、`kubernetes_tools`、`helm_tools`、`internet_tools`、`connectivity_tools`、`prometheus_tools`。
- **与内置的对应关系**：
  - **core_investigation** → `core_investigation_tools.py`：实现 `TodoWrite`，接收 `todos` 列表，格式化后返回（与内置 TodoWriteTool 行为一致）。
  - **runbook** → `runbook_tools.py`：`fetch_runbook`，根据 `runbook_id` 从 `RUNBOOK_SEARCH_PATH` 等解析路径并读文件返回内容。
  - **bash** → `bash_tools.py`：`run_bash_command`、`kubectl_run_image`；这里采用**允许列表**（`BASH_ALLOWED_COMMANDS`）和 `BASH_KUBECTL_ALLOWED_IMAGES`，与内置的 `make_command_safe` 黑名单 + 审批是两套策略，但能力对应。
  - **kubernetes** → `kubernetes_tools.py`：对应 kubernetes/core 及部分其他 K8s 相关 toolsets 的只读能力。
  - **helm** → `helm_tools.py`：对应 helm/core 的只读能力。

也就是说：**配置里启用的内置 toolsets** 决定 Holmes 主流程能用哪些工具；**MCP 导出层** 则决定「当 Holmes 以 MCP 形式对外暴露时，对方能看到和调用哪些工具」——两者可对齐，但实现上是两套代码（内置用 Tool/Toolset + StructuredToolResult，MCP 用 mcp.types.Tool + 返回字符串）。

---

## 五、关键类型与文件速查

| 概念 | 位置 |
|------|------|
| Tool / Toolset 基类、StructuredToolResult、ApprovalRequirement、YAMLTool | `holmes/core/tools.py` |
| ToolsetManager、load_builtin_toolsets、load_toolsets_from_config | `holmes/core/toolset_manager.py`、`holmes/plugins/toolsets/__init__.py` |
| core_investigation（TodoWrite） | `holmes/plugins/toolsets/investigator/core_investigation.py` |
| runbook（fetch_runbook） | `holmes/plugins/toolsets/runbook/runbook_fetcher.py` |
| bash（run_bash_command, kubectl_run_image） | `holmes/plugins/toolsets/bash/bash_toolset.py`、`common/bash.py`、`parse_command.py`、`kubectl/kubectl_run.py` |
| kubernetes/core（只读 kubectl/helm 等） | `holmes/plugins/toolsets/kubernetes.yaml` |
| helm/core（只读 helm） | `holmes/plugins/toolsets/helm.yaml` |
| MCP 侧 bash/runbook/core_investigation/kubernetes/helm | `holmes/mcp/tools/*.py`、`holmes/mcp/tools/registry.py` |

---

## 六、小结

- 你配置的 **toolsets**（core_investigation、runbook、bash、kubernetes/core、helm/core 等）对应的是 **Holmes 内置 Toolset**：  
  - 一部分是 **Python Toolset + Tool**（core_investigation、runbook、bash），实现 `_invoke`、可选 `requires_approval`，bash 还带 `make_command_safe` 与审批。  
  - 一部分是 **YAML 声明的 command/script**（kubernetes/core、helm/core），由通用 `YAMLTool` 执行。
- **执行路径**：ToolsetManager 加载 → 按配置启用 → 执行时走 `Tool.invoke` → `requires_approval` → `_invoke` → transformers → `StructuredToolResult`。
- **MCP 对外暴露**：通过 `holmes/mcp/tools/` 的 TOOLS + `call_tool` 聚合，把上述能力以 MCP 协议暴露；与内置实现分离但功能对应。

按上述结构即可在 `holmes/` 下快速定位各内置 tool 的实现方式与调用链，且无需改动任何 holmes 代码。
