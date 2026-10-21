"""servers/aiops_edge must equal robusta/aiops_edge (single source, specs/008-multi-cluster)."""

import os
import runpy
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "robusta" / "aiops_edge"
FILES = ("__init__.py", "signing.py", "kubectl_args.py", "clusters.py", "tunnel.py", "shim.py", "streams.py")


class VendoredCopyTest(unittest.TestCase):
    @unittest.skipUnless(SOURCE.is_dir(), "robusta checkout not next to mcpstander")
    def test_vendored_copy_matches_source(self):
        for name in FILES:
            with self.subTest(name=name):
                self.assertEqual(
                    (ROOT / "servers" / "aiops_edge" / name).read_text(),
                    (SOURCE / name).read_text(),
                    "run scripts/sync_aiops_edge.sh",
                )
        self.assertEqual((ROOT / "bin" / "kubectl").read_text(), (SOURCE / "kubectl").read_text())


class ShimWrapperTest(unittest.TestCase):
    def test_without_cluster_execs_real_kubectl(self):
        calls = []
        env = {k: v for k, v in os.environ.items() if k != "AIOPS_CLUSTER"}
        env["AIOPS_REAL_KUBECTL"] = "/opt/kubectl.real"

        def fake_execv(path, args):
            calls.append((path, args))
            raise SystemExit(0)

        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("os.execv", fake_execv),
            mock.patch("sys.argv", ["kubectl", "get", "ns"]),
        ):
            with self.assertRaises(SystemExit):
                runpy.run_path(str(ROOT / "bin" / "kubectl"))
        self.assertEqual(calls, [("/opt/kubectl.real", ["/opt/kubectl.real", "get", "ns"])])


if __name__ == "__main__":
    unittest.main()
