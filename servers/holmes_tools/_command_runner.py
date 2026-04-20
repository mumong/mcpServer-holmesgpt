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

from .mcp_logger import get_logger, log_command

logger = get_logger("command")


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
        logger.error(f"[run_command] 模板渲染失败: tpl={command_tpl!r}, args={arguments}, error={e}")
        return f"Template error: {e}"
    cmd = os.path.expandvars(cmd)
    logger.info(f"[run_command] 执行命令: {cmd}")
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
                f"[run_command] ❌ 命令失败 (exit {result.returncode}, {elapsed:.2f}s): {cmd}\n"
                f"  stdout({len(stdout)}): {stdout[:300]}\n"
                f"  stderr({len(stderr)}): {stderr[:300]}"
            )
            return f"Command failed (exit {result.returncode}):\n{cmd}\n{out}"
        logger.info(
            f"[run_command] ✅ 命令成功 ({elapsed:.2f}s, output={len(out)} chars): {cmd[:120]}"
        )
        return out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        logger.error(f"[run_command] ⏰ 命令超时 ({elapsed:.2f}s, limit={timeout}s): {cmd}")
        return f"Command timed out after {timeout}s."
    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.error(f"[run_command] 💥 异常 ({elapsed:.2f}s): {cmd}, error={e}")
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
        logger.error(f"[run_script] 模板渲染失败: args={arguments}, error={e}")
        return f"Template error: {e}"
    script = os.path.expandvars(script)
    if not script.strip().startswith("#!"):
        script = "#!/bin/bash\n" + script
    logger.info(f"[run_script] 执行脚本 ({len(script)} chars): {script[:200]}")
    fd, path = tempfile.mkstemp(suffix=".sh")
    t0 = time.monotonic()
    try:
        os.write(fd, script.encode("utf-8"))
        os.close(fd)
        os.chmod(path, 0o700)
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
                f"[run_script] ❌ 脚本失败 (exit {result.returncode}, {elapsed:.2f}s)\n"
                f"  stdout({len(stdout)}): {stdout[:300]}\n"
                f"  stderr({len(stderr)}): {stderr[:300]}"
            )
            return f"Script failed (exit {result.returncode}):\n{out}"
        logger.info(f"[run_script] ✅ 脚本成功 ({elapsed:.2f}s, output={len(out)} chars)")
        return out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        logger.error(f"[run_script] ⏰ 脚本超时 ({elapsed:.2f}s, limit={timeout}s)")
        return f"Script timed out after {timeout}s."
    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.error(f"[run_script] 💥 异常 ({elapsed:.2f}s): error={e}")
        return str(e)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
