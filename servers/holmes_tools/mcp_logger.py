"""
MCP 统一日志模块 —— 为所有 MCP 工具提供分级、多格式的结构化日志。

设计原则：
  - 所有日志输出到 stderr，避免污染 MCP stdio JSON-RPC 的 stdout 通道
  - 通过环境变量控制级别和格式，无需改代码即可调整
  - 提供面向 MCP 场景的 helper 函数，覆盖工具调用、HTTP 请求、Shell 命令三大类

环境变量：
  MCP_LOG_LEVEL   — DEBUG / INFO / WARNING / ERROR（默认 INFO）
  MCP_LOG_FORMAT  — text / json（默认 text）

用法：
  from .mcp_logger import get_logger, log_tool_call, log_tool_result

  logger = get_logger("prometheus")
  log_tool_call("prometheus", "execute_prometheus_instant_query", {"query": "up"})
"""

import json
import logging
import os
import sys
import time
import traceback
from typing import Any, Dict, Optional, Union


# ---------------------------------------------------------------------------
# 内部常量
# ---------------------------------------------------------------------------

# 环境变量键
_ENV_LEVEL = "MCP_LOG_LEVEL"
_ENV_FORMAT = "MCP_LOG_FORMAT"

# 默认值
_DEFAULT_LEVEL = "INFO"
_DEFAULT_FORMAT = "text"

# 参数摘要截断长度（INFO 级别用）
_ARGS_SUMMARY_MAX = 200

# 完整内容截断长度（DEBUG 级别用）
_BODY_MAX = 4096

# 已配置的 handler 缓存，避免重复挂载
_configured_loggers: set = set()


# ---------------------------------------------------------------------------
# 格式化器
# ---------------------------------------------------------------------------

class _TextFormatter(logging.Formatter):
    """人类可读的文本格式，带时间戳、级别、logger 名称。"""

    _FMT = "%(asctime)s [%(levelname)-7s] %(name)s — %(message)s"
    _DATEFMT = "%Y-%m-%d %H:%M:%S"

    def __init__(self):
        super().__init__(fmt=self._FMT, datefmt=self._DATEFMT)


class _JsonFormatter(logging.Formatter):
    """结构化 JSON 格式，每条日志一行，便于日志采集系统解析。"""

    def format(self, record: logging.LogRecord) -> str:
        entry: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # 附加结构化字段（由 helper 函数通过 extra 传入）
        if hasattr(record, "structured"):
            entry["data"] = record.structured
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = traceback.format_exception(*record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 核心配置
# ---------------------------------------------------------------------------

def _resolve_level() -> int:
    """从环境变量解析日志级别。"""
    raw = os.environ.get(_ENV_LEVEL, _DEFAULT_LEVEL).strip().upper()
    mapping = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    return mapping.get(raw, logging.INFO)


def _resolve_formatter() -> logging.Formatter:
    """从环境变量解析日志格式化器。"""
    raw = os.environ.get(_ENV_FORMAT, _DEFAULT_FORMAT).strip().lower()
    if raw == "json":
        return _JsonFormatter()
    return _TextFormatter()


def _ensure_stderr_handler(logger: logging.Logger) -> None:
    """确保 logger 有且仅有一个 stderr handler，避免重复挂载。"""
    if logger.name in _configured_loggers:
        return
    # 清除可能继承的 handler
    logger.handlers.clear()
    logger.propagate = False

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_resolve_formatter())
    logger.addHandler(handler)
    logger.setLevel(_resolve_level())

    _configured_loggers.add(logger.name)


# ---------------------------------------------------------------------------
# 公开 API：get_logger
# ---------------------------------------------------------------------------

def get_logger(name: str) -> logging.Logger:
    """
    获取一个已配置好的 logger 实例。

    所有日志输出到 stderr，级别和格式由环境变量控制。
    名称会自动加上 'mcp.' 前缀以便统一过滤。

    Args:
        name: 模块/工具集名称，如 "prometheus"、"kubernetes"

    Returns:
        配置好的 logging.Logger 实例
    """
    full_name = f"mcp.{name}" if not name.startswith("mcp.") else name
    logger = logging.getLogger(full_name)
    _ensure_stderr_handler(logger)
    return logger


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------

def _truncate(text: Any, max_len: int) -> str:
    """安全截断，处理非字符串类型。"""
    s = str(text) if not isinstance(text, str) else text
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"...(truncated, total {len(s)} chars)"


def _args_summary(arguments: Optional[Dict]) -> str:
    """生成参数摘要：键名 + 值的前 60 字符。"""
    if not arguments:
        return "{}"
    parts = []
    for k, v in arguments.items():
        v_str = str(v)
        if len(v_str) > 60:
            v_str = v_str[:60] + "..."
        parts.append(f"{k}={v_str}")
    summary = ", ".join(parts)
    return _truncate(summary, _ARGS_SUMMARY_MAX)


def _structured_log(
    logger: logging.Logger,
    level: int,
    msg: str,
    data: Optional[Dict] = None,
) -> None:
    """带结构化数据的日志输出（JSON 格式下会写入 data 字段）。"""
    if data:
        logger.log(level, msg, extra={"structured": data})
    else:
        logger.log(level, msg)


# ---------------------------------------------------------------------------
# 公开 API：log_tool_call
# ---------------------------------------------------------------------------

def log_tool_call(
    server_name: str,
    tool_name: str,
    arguments: Optional[Dict] = None,
) -> None:
    """
    记录 MCP 工具调用入口。

    - DEBUG: 完整参数
    - INFO:  工具名 + 参数摘要

    Args:
        server_name: MCP 服务名称，如 "kubernetes"、"prometheus"
        tool_name:   工具名称，如 "kubectl_describe"
        arguments:   工具参数字典
    """
    logger = get_logger(server_name)

    if logger.isEnabledFor(logging.DEBUG):
        # DEBUG：完整参数，便于排查
        _structured_log(logger, logging.DEBUG, (
            f"[TOOL_CALL] {tool_name} | "
            f"args={_truncate(json.dumps(arguments or {}, ensure_ascii=False, default=str), _BODY_MAX)}"
        ), data={
            "event": "tool_call",
            "server": server_name,
            "tool": tool_name,
            "arguments": arguments,
        })
    elif logger.isEnabledFor(logging.INFO):
        # INFO：参数摘要
        logger.info(
            f"[TOOL_CALL] {tool_name} | args=({_args_summary(arguments)})"
        )


# ---------------------------------------------------------------------------
# 公开 API：log_tool_result
# ---------------------------------------------------------------------------

def log_tool_result(
    server_name: str,
    tool_name: str,
    result: Any,
    elapsed_seconds: float,
    error: Optional[Exception] = None,
) -> None:
    """
    记录 MCP 工具调用结果。

    - DEBUG: 完整响应体
    - INFO:  结果长度 + 耗时 + 成功/失败状态
    - ERROR: 异常信息 + 堆栈

    Args:
        server_name:     MCP 服务名称
        tool_name:       工具名称
        result:          工具返回结果（字符串或其他）
        elapsed_seconds: 耗时（秒）
        error:           如果调用失败，传入异常对象
    """
    logger = get_logger(server_name)
    result_str = str(result) if result is not None else ""
    result_len = len(result_str)
    elapsed_ms = elapsed_seconds * 1000

    if error:
        # ERROR：异常详情
        _structured_log(logger, logging.ERROR, (
            f"[TOOL_RESULT] {tool_name} | FAILED | "
            f"{elapsed_ms:.0f}ms | {type(error).__name__}: {error}"
        ), data={
            "event": "tool_result",
            "server": server_name,
            "tool": tool_name,
            "status": "error",
            "elapsed_ms": round(elapsed_ms, 1),
            "error_type": type(error).__name__,
            "error_msg": str(error),
        })
        # 附带堆栈（仅 DEBUG 可见完整 traceback，ERROR 级别记录摘要即可）
        logger.debug(
            f"[TOOL_RESULT] {tool_name} | traceback:\n"
            f"{traceback.format_exception(type(error), error, error.__traceback__)}"
        )
        return

    status = "ok"
    # 简单启发式判断：结果中包含错误标记时标记为 warning
    lower_result = result_str[:500].lower()
    if any(kw in lower_result for kw in ("error", "failed", "command failed", "timed out")):
        status = "warn"

    if logger.isEnabledFor(logging.DEBUG):
        # DEBUG：完整响应体
        _structured_log(logger, logging.DEBUG, (
            f"[TOOL_RESULT] {tool_name} | {status.upper()} | "
            f"{elapsed_ms:.0f}ms | {result_len} chars | "
            f"body={_truncate(result_str, _BODY_MAX)}"
        ), data={
            "event": "tool_result",
            "server": server_name,
            "tool": tool_name,
            "status": status,
            "elapsed_ms": round(elapsed_ms, 1),
            "result_length": result_len,
            "result": result_str[:_BODY_MAX],
        })
    elif logger.isEnabledFor(logging.INFO):
        # INFO：摘要
        logger.info(
            f"[TOOL_RESULT] {tool_name} | {status.upper()} | "
            f"{elapsed_ms:.0f}ms | {result_len} chars"
        )

    # WARNING：结果中检测到错误标记时额外告警
    if status == "warn" and logger.isEnabledFor(logging.WARNING):
        logger.warning(
            f"[TOOL_RESULT] {tool_name} | 结果包含错误标记 | "
            f"preview={_truncate(result_str, 300)}"
        )


# ---------------------------------------------------------------------------
# 公开 API：log_http_request
# ---------------------------------------------------------------------------

def log_http_request(
    method: str,
    url: str,
    params_or_data: Optional[Union[Dict, str]] = None,
    status_code: Optional[int] = None,
    response_text: Optional[str] = None,
    elapsed: float = 0.0,
) -> None:
    """
    记录 HTTP 请求与响应。

    - DEBUG: 完整请求参数 + 完整响应体
    - INFO:  方法 + URL + 状态码 + 响应长度 + 耗时
    - WARNING: 4xx/5xx 状态码

    Args:
        method:         HTTP 方法（GET/POST/...）
        url:            请求 URL
        params_or_data: 请求参数或 body
        status_code:    HTTP 响应状态码
        response_text:  响应文本
        elapsed:        耗时（秒）
    """
    logger = get_logger("http")
    elapsed_ms = elapsed * 1000
    resp_len = len(response_text) if response_text else 0
    status_str = str(status_code) if status_code else "N/A"

    if logger.isEnabledFor(logging.DEBUG):
        # DEBUG：完整请求和响应
        params_str = (
            json.dumps(params_or_data, ensure_ascii=False, default=str)
            if isinstance(params_or_data, dict)
            else str(params_or_data or "")
        )
        _structured_log(logger, logging.DEBUG, (
            f"[HTTP] {method} {url} | {status_str} | "
            f"{elapsed_ms:.0f}ms | req={_truncate(params_str, _BODY_MAX)} | "
            f"resp={_truncate(response_text or '', _BODY_MAX)}"
        ), data={
            "event": "http_request",
            "method": method,
            "url": url,
            "status_code": status_code,
            "elapsed_ms": round(elapsed_ms, 1),
            "request_params": params_or_data,
            "response_length": resp_len,
            "response_body": (response_text or "")[:_BODY_MAX],
        })
    elif logger.isEnabledFor(logging.INFO):
        # INFO：摘要
        logger.info(
            f"[HTTP] {method} {url} | {status_str} | "
            f"{elapsed_ms:.0f}ms | resp={resp_len} chars"
        )

    # WARNING：HTTP 错误状态码
    if status_code and status_code >= 400:
        logger.warning(
            f"[HTTP] {method} {url} | HTTP {status_code} | "
            f"{elapsed_ms:.0f}ms | resp_preview={_truncate(response_text or '', 500)}"
        )


# ---------------------------------------------------------------------------
# 公开 API：log_command
# ---------------------------------------------------------------------------

def log_command(
    cmd: str,
    returncode: Optional[int] = None,
    stdout: Optional[str] = None,
    stderr: Optional[str] = None,
    elapsed: float = 0.0,
    error: Optional[Exception] = None,
) -> None:
    """
    记录 Shell 命令执行。

    - DEBUG: 完整命令 + stdout + stderr
    - INFO:  命令摘要 + 退出码 + 输出长度 + 耗时
    - WARNING: 非零退出码
    - ERROR: 异常（超时等）

    Args:
        cmd:        执行的命令字符串
        returncode: 进程退出码
        stdout:     标准输出
        stderr:     标准错误
        elapsed:    耗时（秒）
        error:      异常对象（超时、执行失败等）
    """
    logger = get_logger("command")
    elapsed_ms = elapsed * 1000
    stdout_len = len(stdout) if stdout else 0
    stderr_len = len(stderr) if stderr else 0

    # ERROR：异常（超时、权限等）
    if error:
        _structured_log(logger, logging.ERROR, (
            f"[CMD] EXCEPTION | {elapsed_ms:.0f}ms | "
            f"{type(error).__name__}: {error} | cmd={_truncate(cmd, 300)}"
        ), data={
            "event": "command",
            "cmd": cmd[:_BODY_MAX],
            "status": "exception",
            "elapsed_ms": round(elapsed_ms, 1),
            "error_type": type(error).__name__,
            "error_msg": str(error),
        })
        return

    if logger.isEnabledFor(logging.DEBUG):
        # DEBUG：完整输出
        _structured_log(logger, logging.DEBUG, (
            f"[CMD] exit={returncode} | {elapsed_ms:.0f}ms | "
            f"cmd={cmd} | "
            f"stdout={_truncate(stdout or '', _BODY_MAX)} | "
            f"stderr={_truncate(stderr or '', _BODY_MAX)}"
        ), data={
            "event": "command",
            "cmd": cmd,
            "returncode": returncode,
            "elapsed_ms": round(elapsed_ms, 1),
            "stdout": (stdout or "")[:_BODY_MAX],
            "stderr": (stderr or "")[:_BODY_MAX],
        })
    elif logger.isEnabledFor(logging.INFO):
        # INFO：摘要
        logger.info(
            f"[CMD] exit={returncode} | {elapsed_ms:.0f}ms | "
            f"stdout={stdout_len} chars, stderr={stderr_len} chars | "
            f"cmd={_truncate(cmd, 120)}"
        )

    # WARNING：非零退出码
    if returncode and returncode != 0:
        logger.warning(
            f"[CMD] 非零退出码 exit={returncode} | {elapsed_ms:.0f}ms | "
            f"cmd={_truncate(cmd, 200)} | "
            f"stderr_preview={_truncate(stderr or '', 500)}"
        )


# ---------------------------------------------------------------------------
# 便捷装饰器：自动记录工具调用 + 结果
# ---------------------------------------------------------------------------

def tool_logged(server_name: str):
    """
    装饰器工厂：自动为工具函数添加调用/结果日志。

    用法：
        @tool_logged("kubernetes")
        def call_tool(name: str, arguments: dict) -> Optional[str]:
            ...

    装饰后，每次调用会自动记录 log_tool_call 和 log_tool_result。
    """
    def decorator(func):
        def wrapper(name: str, arguments: dict, *args, **kwargs):
            log_tool_call(server_name, name, arguments)
            t0 = time.monotonic()
            try:
                result = func(name, arguments, *args, **kwargs)
                elapsed = time.monotonic() - t0
                log_tool_result(server_name, name, result, elapsed)
                return result
            except Exception as e:
                elapsed = time.monotonic() - t0
                log_tool_result(server_name, name, None, elapsed, error=e)
                raise
        # 保留原函数元信息
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        wrapper.__module__ = func.__module__
        return wrapper
    return decorator
