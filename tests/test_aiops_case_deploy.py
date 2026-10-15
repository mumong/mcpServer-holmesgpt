import subprocess
import sys
import unittest
from pathlib import Path

import yaml


class AiopsServerImportTests(unittest.TestCase):
    """Regression: mcp-proxy launches the server from servers/ (CWD=servers),
    but the tool logic uses `servers.aiops_observability.*` absolute imports.
    Both entrypoints must inject the project root so import works from that CWD.
    """

    def _import_from_servers_cwd(self, module: str) -> subprocess.CompletedProcess:
        servers_dir = Path(__file__).resolve().parents[1] / "servers"
        return subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            cwd=str(servers_dir),
        )

    def test_aiops_case_server_imports_from_servers_cwd(self):
        result = self._import_from_servers_cwd("aiops_case_server")
        self.assertEqual(result.returncode, 0, msg=result.stderr[-600:])

    def test_aiops_fine_server_imports_from_servers_cwd(self):
        result = self._import_from_servers_cwd("aiops_observability_fine_server")
        self.assertEqual(result.returncode, 0, msg=result.stderr[-600:])

    def test_aiops_query_server_imports_from_servers_cwd(self):
        result = self._import_from_servers_cwd("aiops_observability_query_server")
        self.assertEqual(result.returncode, 0, msg=result.stderr[-600:])


class AiopsCaseDeployTests(unittest.TestCase):
    def test_deployment_uses_mcp_owned_aiops_cases_path_on_master(self):
        deployment = yaml.safe_load(Path("deploy/deployment.yaml").read_text(encoding="utf-8"))
        pod_spec = deployment["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]

        self.assertEqual(pod_spec["nodeSelector"]["kubernetes.io/hostname"], "master")

        mounts = {item["name"]: item for item in container["volumeMounts"]}
        self.assertNotIn("aiops-data", mounts)
        self.assertIn("aiops-cases", mounts)
        self.assertEqual(mounts["aiops-cases"]["mountPath"], "/app/aiops-cases")
        self.assertFalse(mounts["aiops-cases"].get("readOnly", False))

        volumes = {item["name"]: item for item in pod_spec["volumes"]}
        self.assertNotIn("aiops-data", volumes)
        self.assertIn("aiops-cases", volumes)
        self.assertEqual(
            volumes["aiops-cases"]["hostPath"]["path"], "/root/huhu/agent/combine-aiops-mcp/aiops-cases"
        )
        self.assertEqual(volumes["aiops-cases"]["hostPath"]["type"], "DirectoryOrCreate")

    def test_aiops_case_deploy_env_has_no_data_dependency_and_fine_server_exists(self):
        configmap = yaml.safe_load(Path("deploy/configmap.yaml").read_text(encoding="utf-8"))
        config = yaml.safe_load(configmap["data"]["mcp_config.yaml"])
        servers = {item["name"]: item for item in config["basicmcp"]}
        aiops_case = servers["aiops-case"]
        fine = servers["aiops-observability-fine"]
        query = servers["aiops-observability-query"]

        self.assertEqual(aiops_case["env"]["AIOPS_CASES_DIR"], "/app/aiops-cases")
        self.assertNotIn("AIOPS_DATASET_ROOT", aiops_case["env"])
        self.assertNotIn("AIOPS_COLLECT_SCRIPT", aiops_case["env"])
        self.assertEqual(fine["path"], "servers/aiops_observability_fine_server.py")
        self.assertEqual(fine["port"], 8090)
        self.assertEqual(query["path"], "servers/aiops_observability_query_server.py")
        self.assertEqual(query["port"], 8100)

        service = yaml.safe_load(Path("deploy/service.yaml").read_text(encoding="utf-8"))
        service_ports = {item["name"]: item["port"] for item in service["spec"]["ports"]}
        self.assertEqual(service_ports["aiops-query"], 8100)

        deployment_ports = {
            item["name"]: item["containerPort"]
            for item in yaml.safe_load(Path("deploy/deployment.yaml").read_text(encoding="utf-8"))["spec"][
                "template"
            ]["spec"]["containers"][0]["ports"]
        }
        self.assertEqual(deployment_ports["aiops-query"], 8100)

    def test_aiops_observability_servers_target_monitor_namespace(self):
        configmap = yaml.safe_load(Path("deploy/configmap.yaml").read_text(encoding="utf-8"))
        config = yaml.safe_load(configmap["data"]["mcp_config.yaml"])
        servers = {item["name"]: item for item in config["basicmcp"]}

        expected = {
            "PROMETHEUS_URL": "http://observability-prometheus.monitor.svc:9090",
            "AIOPS_ES_EXEC_NAMESPACE": "monitor",
            "AIOPS_ES_SECRET_NAMESPACE": "monitor",
            "AIOPS_CLICKHOUSE_EXEC_NAMESPACE": "monitor",
            "AIOPS_TEMPO_EXEC_NAMESPACE": "monitor",
        }
        for server_name in (
            "aiops-case",
            "aiops-observability-fine",
            "aiops-observability-query",
        ):
            with self.subTest(server=server_name):
                env = servers[server_name]["env"]
                for name, value in expected.items():
                    self.assertEqual(env.get(name), value)

    def test_local_aiops_case_config_has_no_collect_script(self):
        config = yaml.safe_load(Path("config/mcp_config.yaml").read_text(encoding="utf-8"))
        servers = {item["name"]: item for item in config["basicmcp"]}
        aiops_case = servers["aiops-case"]
        fine = servers["aiops-observability-fine"]
        query = servers["aiops-observability-query"]

        self.assertEqual(aiops_case["env"]["AIOPS_CASES_DIR"], "./aiops-cases")
        self.assertNotIn("AIOPS_COLLECT_SCRIPT", aiops_case["env"])
        self.assertEqual(fine["port"], 8090)
        self.assertFalse(fine["enabled"])
        self.assertEqual(query["port"], 8100)
        self.assertFalse(query["enabled"])


if __name__ == "__main__":
    unittest.main()
