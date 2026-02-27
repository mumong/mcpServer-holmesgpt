"""
通用「命令/脚本」执行层：根据工具名与参数渲染 Jinja2 模板后执行 shell，返回标准输出。
用于 kubernetes_core、helm 等声明式工具。来源：Holmes mcp/tools/_command_runner。
"""
import os
import subprocess
import tempfile
from typing import Any, Dict

try:
    from jinja2 import Template
except ImportError:
    Template = None


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
        return f"Template error: {e}"
    cmd = os.path.expandvars(cmd)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            return f"Command failed (exit {result.returncode}):\n{cmd}\n{out}"
        return out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s."
    except Exception as e:
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
        return f"Template error: {e}"
    script = os.path.expandvars(script)
    if not script.strip().startswith("#!"):
        script = "#!/bin/bash\n" + script
    fd, path = tempfile.mkstemp(suffix=".sh")
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
        out = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            return f"Script failed (exit {result.returncode}):\n{out}"
        return out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Script timed out after {timeout}s."
    except Exception as e:
        return str(e)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
