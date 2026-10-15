"""
通用「命令/脚本」执行层：根据工具名与参数渲染 Jinja2 模板后执行 shell，返回标准输出。
用于 kubernetes_core、helm 等声明式工具。来源：Holmes mcp/tools/_command_runner。
"""

import os
import subprocess
import tempfile
import time
from typing import Any, Dict

try:
    from jinja2 import Template
except ImportError:
    Template = None

from .mcp_logger import get_logger

logger = get_logger("command")

DEFAULT_MAX_OUTPUT_BYTES = 1_000_000


def _max_output_bytes() -> int:
    try:
        return int(os.environ.get("MCP_TOOL_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES))
    except ValueError:
        return DEFAULT_MAX_OUTPUT_BYTES


def _cap_output(text: str) -> str:
    """Bound tool output to MCP_TOOL_MAX_OUTPUT_BYTES with an explicit marker."""
    limit = _max_output_bytes()
    data = text.encode("utf-8")
    if limit <= 0 or len(data) <= limit:
        return text
    kept = data[:limit].decode("utf-8", errors="ignore")
    return f"{kept}\n...[output truncated: {len(data)} bytes exceeds limit of {limit} bytes]"


def _render(template_str: str, params: Dict[str, Any]) -> str:
    if Template is None:
        raise RuntimeError("jinja2 is required for kubernetes/helm tools. pip install jinja2")
    params = {k: (v if v is not None else "") for k, v in params.items()}
    # 避免 Jinja2 全局 namespace 覆盖：当未显式传入 namespace 时，强制提供空字符串，
    # 这样模板中的 {% if namespace %} 在未设置时为假，不会渲染出 -n <class 'jinja2.utils.Namespace'>。
    if "namespace" not in params:
        params["namespace"] = ""
    return Template(template_str).render(**params)


def run_command(
    command_tpl: str,
    arguments: dict,
    timeout: int = 120,
) -> str:
    """渲染单条 command 并执行，返回 stdout+stderr。"""
    try:
        cmd = _render(command_tpl, arguments)
    except Exception as e:
        logger.error("[run_command] 模板渲染失败: tpl=%r, args=%s, error=%s", command_tpl, arguments, e)
        return f"Template error: {e}"
    cmd = os.path.expandvars(cmd)
    logger.info("[run_command] 执行命令: %s", cmd)
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - t0
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        out = stdout + stderr
        if result.returncode != 0:
            logger.warning(
                "[run_command] 命令失败 (exit %s, %.2fs): %s\n  stdout(%d): %s\n  stderr(%d): %s",
                result.returncode,
                elapsed,
                cmd,
                len(stdout),
                stdout[:300],
                len(stderr),
                stderr[:300],
            )
            return _cap_output(f"Command failed (exit {result.returncode}):\n{cmd}\n{out}")
        logger.info("[run_command] 命令成功 (%.2fs, output=%d chars): %s", elapsed, len(out), cmd[:120])
        return _cap_output(out.strip()) or "(no output)"
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        logger.error("[run_command] 命令超时 (%.2fs, limit=%ss): %s", elapsed, timeout, cmd)
        return f"Command timed out after {timeout}s."
    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.error("[run_command] 异常 (%.2fs): %s, error=%s", elapsed, cmd, e)
        return str(e)


def run_script(
    script_tpl: str,
    arguments: dict,
    timeout: int = 300,
) -> str:
    """渲染多行 script 并写入临时 .sh 执行，返回 stdout+stderr。"""
    try:
        script = _render(script_tpl, arguments)
    except Exception as e:
        logger.error("[run_script] 模板渲染失败: args=%s, error=%s", arguments, e)
        return f"Template error: {e}"
    script = os.path.expandvars(script)
    if not script.strip().startswith("#!"):
        script = "#!/bin/bash\n" + script
    logger.info("[run_script] 执行脚本 (%d chars): %s", len(script), script[:200])
    fd, path = tempfile.mkstemp(suffix=".sh")
    t0 = time.monotonic()
    try:
        os.write(fd, script.encode("utf-8"))
        os.close(fd)
        # mkstemp creates a private 0600 file; bash reads it without execute permission.
        result = subprocess.run(
            ["/bin/bash", path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - t0
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        out = stdout + stderr
        if result.returncode != 0:
            logger.warning(
                "[run_script] 脚本失败 (exit %s, %.2fs)\n  stdout(%d): %s\n  stderr(%d): %s",
                result.returncode,
                elapsed,
                len(stdout),
                stdout[:300],
                len(stderr),
                stderr[:300],
            )
            return _cap_output(f"Script failed (exit {result.returncode}):\n{out}")
        logger.info("[run_script] 脚本成功 (%.2fs, output=%d chars)", elapsed, len(out))
        return _cap_output(out.strip()) or "(no output)"
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        logger.error("[run_script] 脚本超时 (%.2fs, limit=%ss)", elapsed, timeout)
        return f"Script timed out after {timeout}s."
    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.error("[run_script] 异常 (%.2fs): error=%s", elapsed, e)
        return str(e)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
