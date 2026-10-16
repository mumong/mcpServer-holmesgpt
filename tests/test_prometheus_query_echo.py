"""Successful Prometheus query payloads carry the expression they answer.

A bare ``{"status":"success","data":{...}}`` vector says nothing about what was
asked; two of them (CPU and memory) were swapped downstream in live run
66662a8287ce4dac. Error payloads already echo ``query``; success does too.
"""

import json
import unittest
from unittest.mock import patch

from holmes_tools import prometheus

_VECTOR = '{"status":"success","data":{"resultType":"vector","result":[]}}'


class PrometheusQueryEchoTests(unittest.TestCase):
    def test_instant_query_success_echoes_query(self):
        with patch.object(prometheus, "_do_get", return_value=_VECTOR):
            out = json.loads(prometheus.call_tool("execute_prometheus_instant_query", {"query": "up"}))
        self.assertEqual(out["query"], "up")
        self.assertEqual(out["data"]["resultType"], "vector")

    def test_range_query_success_echoes_query(self):
        with patch.object(prometheus, "_do_post", return_value=_VECTOR):
            out = json.loads(
                prometheus.call_tool(
                    "execute_prometheus_range_query", {"query": "up", "start": "1", "end": "2"}
                )
            )
        self.assertEqual(out["query"], "up")

    def test_non_json_or_error_payloads_pass_through(self):
        with patch.object(prometheus, "_do_get", return_value="not json"):
            self.assertEqual(prometheus.call_tool("execute_prometheus_instant_query", {"query": "up"}), "not json")
        err = '{"status":"error","query":"bad","http_error":"x"}'
        with patch.object(prometheus, "_do_get", return_value=err):
            self.assertEqual(prometheus.call_tool("execute_prometheus_instant_query", {"query": "bad"}), err)


if __name__ == "__main__":
    unittest.main()
