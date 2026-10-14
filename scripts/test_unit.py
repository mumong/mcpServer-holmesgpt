"""Run offline unit tests with an accidental network/process-call tripwire.

Tests must still mock external boundaries. The only real child processes are
the three existing regression checks that import MCP entrypoints from servers/.
This is a regression guard, not an operating-system security sandbox.
"""

import os
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
IMPORT_CHECKS = {
    "import aiops_case_server",
    "import aiops_observability_fine_server",
    "import aiops_observability_query_server",
}


def guard_external_calls(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "os.system", "os.exec", "os.posix_spawn"}:
        raise RuntimeError(f"Offline tests blocked external operation: {event}")
    if event == "subprocess.Popen":
        executable, command, cwd, _ = args
        if (
            executable == sys.executable
            and isinstance(command, (list, tuple))
            and len(command) == 3
            and command[0] == sys.executable
            and command[1] == "-c"
            and command[2] in IMPORT_CHECKS
            and Path(cwd or "").resolve() == ROOT / "servers"
        ):
            return
        raise RuntimeError("Offline tests blocked external child process")


def check_python_version():
    """Fail fast when the interpreter is older than pyproject's requires-python."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^requires-python\s*=\s*">=(\d+)\.(\d+)"', text, re.MULTILINE)
    if match and sys.version_info[:2] < (int(match[1]), int(match[2])):
        sys.exit(f"Python >={match[1]}.{match[2]} required (pyproject.toml), got {sys.version.split()[0]}")


def main():
    check_python_version()
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    sys.addaudithook(guard_external_calls)
    suite = unittest.defaultTestLoader.discover("tests", top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
