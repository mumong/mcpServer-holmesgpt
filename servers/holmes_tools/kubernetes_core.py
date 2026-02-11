"""
Kubernetes 只读工具集（对应 Holmes kubernetes/core + live-metrics + kube-prometheus-stack + krew-extras）。
工具：kubectl_describe, kubectl_get_by_name, kubectl_get_by_kind_in_namespace, kubectl_get_by_kind_in_cluster,
kubectl_find_resource, kubectl_get_yaml, kubectl_events, kubernetes_jq_query, kubernetes_tabular_query,
kubernetes_count, kubectl_top_pods, kubectl_top_nodes, get_prometheus_target, kubectl_lineage_children,
kubectl_lineage_parents。
依赖：kubectl、jq（部分工具）、jinja2。
"""
from typing import Any, Dict, List, Optional

from mcp.types import Tool

from ._command_runner import run_command, run_script

# 工具名 -> ( "command" | "script", 模板字符串 )
_KUBERNETES_SPECS: Dict[str, tuple] = {
    "kubectl_describe": (
        "command",
        "kubectl describe {{ kind }} {{ name }}{% if namespace %} -n {{ namespace }}{% endif %}",
    ),
    "kubectl_get_by_name": (
        "command",
        "kubectl get --show-labels -o wide {{ kind }} {{ name }}{% if namespace %} -n {{ namespace }}{% endif %}",
    ),
    "kubectl_get_by_kind_in_namespace": (
        "command",
        "kubectl get --show-labels -o wide {{ kind }} -n {{ namespace }}",
    ),
    "kubectl_get_by_kind_in_cluster": (
        "command",
        "kubectl get -A --show-labels -o wide {{ kind }}",
    ),
    "kubectl_find_resource": (
        "command",
        "kubectl get -A --show-labels -o wide {{ kind }} | grep {{ keyword }}",
    ),
    "kubectl_get_yaml": (
        "command",
        "kubectl get -o yaml {{ kind }} {{ name }}{% if namespace %} -n {{ namespace }}{% endif %}",
    ),
    "kubectl_events": (
        "command",
        "kubectl events --for {{ resource_type }}/{{ resource_name }}{% if namespace %} -n {{ namespace }}{% endif %}",
    ),
    "kubernetes_tabular_query": (
        "command",
        "kubectl get {{ kind }} --all-namespaces -o custom-columns='{{ columns }}'{% if filter_pattern %} | (head -n 1; tail -n +2 | grep {{ filter_pattern }}){% endif %}",
    ),
    "kubectl_top_pods": ("command", "kubectl top pods -A"),
    "kubectl_top_nodes": ("command", "kubectl top nodes"),
    "get_prometheus_target": (
        "command",
        "kubectl get --raw '/api/v1/namespaces/{{ prometheus_namespace }}/services/{{ prometheus_service_name }}:9090/proxy/api/v1/targets' | jq '.data.activeTargets[] | select(.labels.job == \"{{ target_name }}\")'",
    ),
    "kubectl_lineage_children": (
        "command",
        "kubectl lineage {{ kind }} {{ name }}{% if namespace %} -n {{ namespace }}{% endif %}",
    ),
    "kubectl_lineage_parents": (
        "command",
        "kubectl lineage {{ kind }} {{ name }}{% if namespace %} -n {{ namespace }}{% endif %} -D",
    ),
}

# 简化版 jq 查询（不分页，适合中小集群）
_KUBERNETES_JQ_SCRIPT = """\
set -e
echo "Executing jq query for {{ kind }}..."
kubectl get {{ kind }} --all-namespaces -o json | jq -r '{{ jq_expr }}'
"""

_KUBERNETES_COUNT_SCRIPT = """\
set -e
echo "Count for {{ kind }} with jq..."
OUT=$(kubectl get {{ kind }} --all-namespaces -o json | jq -c -r '{{ jq_expr }}')
COUNT=$(echo "$OUT" | grep -v '^$' | grep -v '^null$' | wc -l)
echo "$COUNT results"
echo "---"
echo "$OUT" | head -n 20
"""


def _normalize_kubectl_args(args: dict) -> dict:
    """kubectl 要求 kind、resource_type 等为小写，避免 AI 传入 Node/Pod 导致命令失败。"""
    out = dict(args)
    if out.get("kind"):
        out["kind"] = str(out["kind"]).strip().lower()
    if out.get("resource_type"):
        out["resource_type"] = str(out["resource_type"]).strip().lower()
    return out


def _run_kubernetes(name: str, arguments: dict) -> Optional[str]:
    args = _normalize_kubectl_args(arguments)
    if name == "kubernetes_jq_query":
        return run_script(_KUBERNETES_JQ_SCRIPT, args, timeout=180)
    if name == "kubernetes_count":
        return run_script(_KUBERNETES_COUNT_SCRIPT, args, timeout=180)
    spec = _KUBERNETES_SPECS.get(name)
    if not spec:
        return None
    typ, tpl = spec
    if typ == "command":
        return run_command(tpl, args, timeout=120)
    return run_script(tpl, args, timeout=180)


def _input_schema(required: List[str], props: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


TOOLS: List[Tool] = [
    Tool(
        name="kubectl_describe",
        description="Run kubectl describe <kind> <name> -n <namespace>. Use for resource description.",
        inputSchema=_input_schema(
            ["kind", "name"],
            {"kind": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}},
        ),
    ),
    Tool(
        name="kubectl_get_by_name",
        description="Run kubectl get <kind> <name> --show-labels -o wide.",
        inputSchema=_input_schema(
            ["kind", "name"],
            {"kind": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}},
        ),
    ),
    Tool(
        name="kubectl_get_by_kind_in_namespace",
        description="List all resources of a kind in a namespace: kubectl get <kind> -n <namespace>.",
        inputSchema=_input_schema(
            ["kind", "namespace"],
            {"kind": {"type": "string"}, "namespace": {"type": "string"}},
        ),
    ),
    Tool(
        name="kubectl_get_by_kind_in_cluster",
        description="List all resources of a kind in the cluster: kubectl get -A <kind>.",
        inputSchema=_input_schema(["kind"], {"kind": {"type": "string"}}),
    ),
    Tool(
        name="kubectl_find_resource",
        description="Find resource by keyword: kubectl get -A <kind> | grep <keyword>.",
        inputSchema=_input_schema(
            ["kind", "keyword"],
            {"kind": {"type": "string"}, "keyword": {"type": "string"}},
        ),
    ),
    Tool(
        name="kubectl_get_yaml",
        description="Get single resource as YAML: kubectl get -o yaml <kind> <name>.",
        inputSchema=_input_schema(
            ["kind", "name"],
            {"kind": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}},
        ),
    ),
    Tool(
        name="kubectl_events",
        description="Retrieve events for a resource. resource_type: pod, service, deployment, etc.",
        inputSchema=_input_schema(
            ["resource_type", "resource_name"],
            {"resource_type": {"type": "string"}, "resource_name": {"type": "string"}, "namespace": {"type": "string"}},
        ),
    ),
    Tool(
        name="kubernetes_jq_query",
        description="kubectl get <kind> -A -o json | jq -r <jq_expr>. Use plural kind (e.g. pods, services).",
        inputSchema=_input_schema(
            ["kind", "jq_expr"],
            {"kind": {"type": "string"}, "jq_expr": {"type": "string"}},
        ),
    ),
    Tool(
        name="kubernetes_tabular_query",
        description="Tabular output: kubectl get <kind> -A -o custom-columns=<columns>. Optional filter_pattern for grep.",
        inputSchema=_input_schema(
            ["kind", "columns"],
            {"kind": {"type": "string"}, "columns": {"type": "string"}, "filter_pattern": {"type": "string"}},
        ),
    ),
    Tool(
        name="kubernetes_count",
        description="Count resources with jq. E.g. .items[] | .metadata.name. Use plural kind.",
        inputSchema=_input_schema(
            ["kind", "jq_expr"],
            {"kind": {"type": "string"}, "jq_expr": {"type": "string"}},
        ),
    ),
    Tool(
        name="kubectl_top_pods",
        description="Real-time CPU/memory per pod: kubectl top pods -A.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="kubectl_top_nodes",
        description="Real-time CPU/memory per node: kubectl top nodes.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_prometheus_target",
        description="Fetch Prometheus target definition via cluster proxy (kube-prometheus-stack).",
        inputSchema=_input_schema(
            ["prometheus_namespace", "prometheus_service_name", "target_name"],
            {
                "prometheus_namespace": {"type": "string"},
                "prometheus_service_name": {"type": "string"},
                "target_name": {"type": "string"},
            },
        ),
    ),
    Tool(
        name="kubectl_lineage_children",
        description="Get children/dependents of a resource (requires kube-lineage/krew).",
        inputSchema=_input_schema(
            ["kind", "name"],
            {"kind": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}},
        ),
    ),
    Tool(
        name="kubectl_lineage_parents",
        description="Get parents/dependencies of a resource (requires kube-lineage/krew).",
        inputSchema=_input_schema(
            ["kind", "name"],
            {"kind": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}},
        ),
    ),
]


def call_tool(name: str, arguments: dict) -> Optional[str]:
    return _run_kubernetes(name, arguments)
