# Holmes 内置工具与 mcpstander 实现对照

本文档对照 **Holmes MCP 导出层**（`holmes/mcp/tools/`）与 **mcpstander** 中实现的 4 类工具，从**功能与能力逻辑**上说明是否一致。

---

## 一、结论摘要

| 工具集 | 功能/能力一致性 | 差异说明 |
|--------|------------------|----------|
| **internet** (fetch_webpage) | ✅ 一致 | 逻辑与参数相同 |
| **connectivity** (tcp_check) | ✅ 一致 | 逻辑与参数相同 |
| **prometheus** (8 个 API 工具) | ✅ 一致 | 工具列表、参数、环境变量、API 路径均对齐 |
| **helm** (只读 8 个) | ✅ 一致 | helm_list / values / status / history / manifest / hooks / chart / notes 与 Holmes helm/core 一致 |
| **bash** (run_bash_command, kubectl_run_image) | ⚠️ 能力对应、策略不同 | 见下文「五、Bash」 |

---

## 二、Internet（fetch_webpage）

| 项目 | Holmes (`holmes/mcp/tools/internet_tools.py`) | mcpstander (`servers/holmes_tools/internet.py`) |
|------|-----------------------------------------------|-------------------------------------------------|
| 工具名 | `fetch_webpage` | `fetch_webpage` |
| 参数 | `url`（必填） | `url`（必填） |
| 环境变量 | `INTERNET_TIMEOUT_SECONDS`（默认 30） | 同左 |
| 行为 | GET url → 若 Content-Type 为 text/html 或内容像 HTML，则 BeautifulSoup 解析，去掉 script/style/nav/header/footer/iframe，再 markdownify 转 Markdown | 与 Holmes 完全一致 |
| 依赖 | requests, beautifulsoup4, markdownify（可选） | 同左 |

**结论：功能和能力逻辑一致。**

---

## 三、Connectivity（tcp_check）

| 项目 | Holmes (`holmes/mcp/tools/connectivity_tools.py`) | mcpstander (`servers/holmes_tools/connectivity.py`) |
|------|----------------------------------------------------|-----------------------------------------------------|
| 工具名 | `tcp_check` | `tcp_check` |
| 参数 | host, port（必填）, timeout（默认 3.0） | 同左 |
| 行为 | `socket.create_connection((host, port), timeout=timeout)`，成功返回 `{"ok": true}`，失败返回 `{"ok": false, "error": "..."}` | 与 Holmes 完全一致 |

**结论：功能和能力逻辑一致。**

---

## 四、Prometheus（8 个工具）

| 项目 | Holmes (`holmes/mcp/tools/prometheus_tools.py`) | mcpstander (`servers/holmes_tools/prometheus.py`) |
|------|-------------------------------------------------|---------------------------------------------------|
| 环境变量 | `PROMETHEUS_URL`（默认 http://localhost:9090/）, `PROMETHEUS_TIMEOUT`（默认 30） | 同左 |
| 工具列表 | list_prometheus_rules, get_metric_names, get_label_values, get_all_labels, get_series, get_metric_metadata, execute_prometheus_instant_query, execute_prometheus_range_query | 与 Holmes 完全一致（8 个） |
| API 调用 | GET/POST 对应 Prometheus HTTP API（rules, label/__name__/values, label/{label}/values, labels, series, metadata, query, query_range） | 路径与参数一致 |

**结论：功能和能力逻辑一致。**

---

## 五、Helm（只读 8 个，对应 Holmes helm/core）

| 工具名 | Holmes 命令/行为 | mcpstander 实现 |
|--------|------------------|-----------------|
| helm_list | `helm list -A` | `run_helm_command(["list", "-A"])` ✅ |
| helm_values | `helm get values -a {{ release_name }} -n {{ namespace }} -o json` | `["get", "values", "-a", release_name, "-n", namespace, "-o", "json"]` ✅ |
| helm_status | `helm status {{ release_name }} -n {{ namespace }}` | `["status", release_name, "-n", namespace]` ✅ |
| helm_history | `helm history {{ release_name }} -n {{ namespace }}` | 同左 ✅ |
| helm_manifest | `helm get manifest {{ release_name }} -n {{ namespace }}` | 同左 ✅ |
| helm_hooks | `helm get hooks {{ release_name }} -n {{ namespace }}` | 同左 ✅ |
| helm_chart | `helm get chart {{ release_name }} -n {{ namespace }}` | 同左 ✅ |
| helm_notes | `helm get notes {{ release_name }} -n {{ namespace }}` | 同左 ✅ |

mcpstander 的 `helm_server.py` 还额外提供 helm_install、helm_upgrade、helm_rollback 等**写操作**，与 Holmes 内置「只读 helm/core」无关；仅就上述 8 个只读工具而言，**功能与能力与 Holmes 一致**。

---

## 六、Bash（run_bash_command、kubectl_run_image）

### 6.1 run_bash_command

| 项目 | Holmes (`holmes/mcp/tools/bash_tools.py`) | mcpstander (`servers/bash_server.py`) |
|------|-------------------------------------------|---------------------------------------|
| 参数 | command（必填）, timeout（默认 60） | command（必填）, timeout_seconds（默认 60） |
| 执行 | subprocess.run(..., shell=True, executable="/bin/bash") | 同左 |
| 安全策略 | **白名单**：首命令必须在 `BASH_ALLOWED_COMMANDS` 中（默认 kubectl, helm, jq, grep, cat, head, tail, wc, curl, awk, sed, ...）；**黑名单**：禁止危险模式（rm -rf /、mkfs、dd of=/dev/、fork bomb、curl\|bash 等） | **仅黑名单**：禁止 UNSAFE_PATTERNS（rm -rf /、mkfs、dd、/dev/sd、fork bomb、chmod -R 777、curl\|bash 等）；可选 `BASH_TOOL_UNSAFE_ALLOW_ALL=1` 放行 |
| 能力 | 执行一条 bash 命令，返回 stdout+stderr 或错误信息 | 同：执行一条 bash，返回 JSON（success, stdout, stderr, returncode）或错误 |

**功能能力**：都是「在受控环境下执行单条 bash 命令」；**逻辑差异**：Holmes 采用「白名单首命令 + 黑名单危险模式」，mcpstander 仅采用「黑名单危险模式」，未实现首命令白名单，因此**能力对应、安全策略不同**（mcpstander 允许的首命令范围更宽）。

### 6.2 kubectl_run_image

| 项目 | Holmes | mcpstander |
|------|--------|------------|
| 参数 | image, command（必填）, namespace（默认 default）, timeout | namespace, pod_name, image（必填）, command（可选数组）, timeout_seconds |
| Pod 名 | 自动生成：`holmes-mcp-` + 8 位随机小写字母 | 调用方传入 `pod_name` |
| 安全策略 | **配置驱动**：`BASH_KUBECTL_ALLOWED_IMAGES` 为 JSON 数组，每项 `{ "image": "xxx", "allowed_commands": ["regex", ...] }`；仅当 image 在列表中且 command 匹配某条正则时才允许执行 | **校验**：namespace、pod_name 符合 K8s 命名规范，镜像名格式合法，command 为字符串列表；**无**「允许的 image 列表」与「允许的命令正则」配置 |
| 命令形态 | `kubectl run <generated_name> --image=... --namespace=... --rm --attach --restart=Never -i -- <command>` | `kubectl run <pod_name> --image=... --namespace=... --rm --attach --restart=Never`，若有 command 则 `-- <command_list>` |

**功能能力**：都是「在指定 namespace 用指定镜像跑临时 Pod 执行命令，执行完删除」；**逻辑差异**：Holmes 通过环境变量严格限定「允许的镜像 + 允许的命令正则」，mcpstander 只做格式与命名规范校验，没有镜像/命令白名单，**能力对应、安全策略不同**（mcpstander 更依赖部署环境与调用方自律）。

---

## 七、总结

- **internet、connectivity、prometheus、helm（只读 8 个）**：与 Holmes 内置工具在**功能与能力逻辑上一致**，可直接视为同一套能力的独立实现。
- **bash**：  
  - **run_bash_command**：能力一致（执行单条 bash），安全策略不同（Holmes 白名单+黑名单，mcpstander 仅黑名单）。  
  - **kubectl_run_image**：能力一致（临时 Pod 跑命令），安全策略不同（Holmes 依赖 BASH_KUBECTL_ALLOWED_IMAGES 白名单，mcpstander 无镜像/命令白名单）。

若需在安全策略上与 Holmes 完全对齐，可在 mcpstander 的 bash_server 中增加：  
- `BASH_ALLOWED_COMMANDS` 首命令白名单；  
- `BASH_KUBECTL_ALLOWED_IMAGES` 及按 image + 命令正则的校验；  
- 以及（可选）kubectl_run_image 的自动生成 Pod 名以保持与 Holmes 行为一致。
