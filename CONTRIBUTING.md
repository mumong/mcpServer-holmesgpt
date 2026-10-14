# 开发与兼容性约定

## 环境与离线验证

使用 Python 3.10+；生产镜像使用 Python 3.11。已有环境满足依赖时无需重装。
新开发环境可在虚拟环境中安装 `requirements-test.txt`（其中包含运行依赖）。
运行依赖在 `requirements.txt`，本仓库直接用于测试的 JSON Schema 校验器约束在
`requirements-test.txt`；MCP SDK 也会在运行镜像间接引入该包。测试使用标准库
unittest，不依赖 pytest。

Fact Ledger 合同测试始终使用 `tests/fixtures/contracts/fact-ledger.schema.json`
中的固定测试快照，单仓 checkout 不需要同级 data 项目。快照逐字来自 data
权威合同；来源提交、路径、SHA256 和更新规则见同目录 README。测试检查固定
hash、schema 合法性及既有合同断言，运行时不读取此快照。
若 `../data/specs/004-canonical-fact-ledger/contracts/fact-ledger.schema.json`
存在，独立一致性测试要求它与快照字节一致；不存在只跳过这项可选检查，
不跳过合同验证，也不按环境切换验证 schema。带上游 checkout 的集成验证
仍需保留，以发现单仓无法检测的上游漂移。合同变更须审查后同步原字节、
来源提交和固定 hash，不自动刷新快照来消除失败。

```bash
make check
make test-unit
make check-syntax
```

可用 `make PYTHON=/path/to/python check` 指定解释器。检查不安装依赖、不生成
字节码、不运行镜像构建或部署。`check-syntax` 检查根目录、`servers/`、
`scripts/`、`tests/` 的 Python 语法，不是类型检查或 lint。

`test-unit` 对意外网络访问及子进程启动设置失败防护；只放行现有三项
从 `servers/` 工作目录导入 AIOps 入口的子进程。此防护不是安全沙箱；
测试仍必须 mock HTTP、kubectl、shell 和进程/线程边界，文件写入仅使用
测试临时目录。不要在普通单测中调用实际 MCP 工具。

旧的 `make test` 保留为 SSE 客户端集成检查，需要运行中的服务；它不是
离线单测。单测通过不代表 SSE、镜像或后端集成验证完成。

## 模块职责

- `start.py`：读取配置、校验入口路径、管理子进程/日志线程和停止信号。
- `launch_config.py`：仅构造环境字典与 proxy 命令；不读文件、不启动进程。
- `servers/*_server.py`：MCP 协议入口，公开工具列表和入口策略。
- `servers/holmes_tools/server_adapter.py`：八个文本入口的公共计时、日志和
  TextContent 包装；入口显式选择参数清洗及异常返回策略。
- `servers/holmes_tools/`：工具描述、输入 schema 与工具分派。
- `servers/aiops_observability/`：采集器、case 持久化、观测查询及 Fact Ledger。

query 入口的结构化错误及聚合入口的逐模块清洗策略不适用公共文本适配器。
历史 vendored `holmes/` 目录已删除（未被引用、未打包）；注明“移植自 Holmes”的
模块是独立实现，不要重新引入上游包到运行路径。

## Python 代码约定

采用四空格缩进、snake_case 函数/模块名，公共边界写类型注解与简短职责说明。
优先纯函数和显式依赖，避免导入时启动进程、网络访问或写入文件。维持
Python 3.10 兼容；不为格式统一引入新运行时依赖或全仓无关格式化。
协议 stdout 只用于 MCP，日志走已有 stderr logger。

修复或重构先补 characterization 测试，再迁移实现。至少固定：

- tools/list 的工具顺序、名称、完整 description 与 inputSchema；这些文字
  是模型可见提示合同，不只是注释。
- 原始/标量参数策略、None 与空字符串区别、未知工具和异常返回/传播策略。
- 命令字符串、继承环境/覆盖顺序、超时和结果文本；mock 命令执行。
- Pod scope/UID、coverage、证据引用、6144-byte 有界投影与 Fact Ledger 一致性。

`tests/test_server_adapters.py` 中的有序工具摘要在重构前生成，涵盖完整 SDK
序列化内容。测试摘要不能因为“重构后不同”直接更新；先逐项解释合同变化。
Runbook schema 的示例文件列表在导入时根据目录生成，快照测试固定该目录输入，
另测动态排序/去重；新增文档不应导致依赖当前 checkout 内容的测试漂移。
启动器行为及镜像复制边界由 `tests/test_launcher.py` 固定。

## 变更与交付边界

保持工具行为的重构不包含修正 shell 拼接、TLS 默认值、RBAC、凭据或存储
策略；发现风险应单列审查，不悄悄修改其语义。新增运行模块必须同步 Dockerfile
复制清单。部署验证、版本更新与代码检查分开执行并如实记录未覆盖项。

独立授权的安全修复需明确说明行为差异并补专用回归：`kubectl_run_image`
现在将 `command` 列表逐项作为 argv 传给 kubectl，不在宿主 shell 中拼接执行。
空格、引号、分号、空字符串均保留为原参数；需要容器 shell 时显式传入
`["sh", "-c", "容器内命令"]`。`run_bash_command` 仍保留受原规则约束的 shell
执行能力。两类工具的返回字段、日志事件和超时合同由 `tests/test_bash_server.py`
覆盖，这不代表 RBAC 或其他工具的 shell 风险已解决。

不提交实际凭据、测试产生的 case 数据或敏感日志。保留现有第三方署名；
自有代码已由维护者选择Apache-2.0，见根目录LICENSE；第三方来源和许可仍须单独
核实，不假定所有历史代码可重新授权，不删除既有第三方署名。
