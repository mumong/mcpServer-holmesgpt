"""kubernetes_tabular_query accepts bare JSONPath columns.

kubectl demands ``HEADER:.json.path`` per column; a model that writes
``metadata.name,status.addresses[0].address`` got ``unexpected custom-columns spec``
twice in live run 66662a8287ce4dac. The tool derives the header and the leading dot.
"""

import unittest
from unittest.mock import patch

from holmes_tools import kubernetes_core


class TabularColumnsTests(unittest.TestCase):
    def test_bare_paths_get_header_and_leading_dot(self):
        self.assertEqual(
            kubernetes_core.normalize_custom_columns("metadata.name,status.addresses[0].address, spec.nodeName"),
            "NAME:.metadata.name,ADDRESS:.status.addresses[0].address,NODENAME:.spec.nodeName",
        )

    def test_well_formed_columns_are_unchanged(self):
        spec = "NAME:.metadata.name,IP:.status.podIP"
        self.assertEqual(kubernetes_core.normalize_custom_columns(spec), spec)

    def test_duplicate_derived_headers_are_disambiguated(self):
        self.assertEqual(
            kubernetes_core.normalize_custom_columns("status.addresses[0].address,status.addresses[1].address"),
            "ADDRESS:.status.addresses[0].address,ADDRESS2:.status.addresses[1].address",
        )

    def test_dispatch_normalizes_columns_before_rendering(self):
        with patch.object(kubernetes_core, "run_command", return_value="ok") as run:
            kubernetes_core.call_tool(
                "kubernetes_tabular_query", {"kind": "Nodes", "columns": "metadata.name,status.phase"}
            )
        args = run.call_args.args[1]
        self.assertEqual(args["kind"], "nodes")
        self.assertEqual(args["columns"], "NAME:.metadata.name,PHASE:.status.phase")

    def test_description_states_the_column_format(self):
        tool = next(t for t in kubernetes_core.TOOLS if t.name == "kubernetes_tabular_query")
        self.assertIn("HEADER:.json.path", tool.description)


if __name__ == "__main__":
    unittest.main()
