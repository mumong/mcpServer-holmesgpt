import json
import unittest
from unittest.mock import patch

from servers.holmes_tools import prometheus


class _FakeResponse:
    status_code = 400
    text = json.dumps({
        "status": "error",
        "errorType": "bad_data",
        "error": "invalid parameter \"query\": 1:49: parse error: unexpected character inside braces: '/'",
    })

    def raise_for_status(self):
        from requests import HTTPError

        raise HTTPError("400 Client Error: Bad Request", response=self)


class PrometheusErrorTests(unittest.TestCase):
    def test_all_prometheus_tools_are_marked_query_only(self):
        for tool in prometheus.TOOLS:
            with self.subTest(tool=tool.name):
                description = tool.description.lower()
                self.assertIn("/query", description)
                self.assertIn("/ask", description)
                self.assertIn("execute_pod_promql", description)

    def test_do_get_preserves_prometheus_error_body(self):
        with patch.object(prometheus.requests, "get", return_value=_FakeResponse()):
            result = json.loads(prometheus._do_get("api/v1/query", params={"query": "bad"}))

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["errorType"], "bad_data")
        self.assertIn("unexpected character inside braces", result["error"])
        self.assertIn("400 Client Error", result["http_error"])
        self.assertEqual(result["query"], "bad")


if __name__ == "__main__":
    unittest.main()
