"""Elasticsearch/Filebeat collector for Pod-scoped AIOps cases.

Two collection modes, in priority order:
1. Direct HTTP when ELASTICSEARCH_URL is explicitly configured.
2. kubectl-exec (default): auto-fetch credentials from the Kubernetes secret
   and `kubectl exec` into the Elasticsearch Pod to curl localhost — the proven
   approach from the data-project scripts, works without brittle direct wiring.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

from ..config import get_config
from ..schemas import EvidenceItem, SourceResult, evidence_id
from . import _kube

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


def _query_body(namespace: str, pod: str, pod_uid: str | None, start: str, end: str, max_records: int) -> dict[str, Any]:
    should_terms = [
        {"term": {"kubernetes.namespace": namespace}},
        {"term": {"kubernetes.namespace_name": namespace}},
        {"term": {"kubernetes.pod.name": pod}},
        {"term": {"kubernetes.pod_name": pod}},
    ]
    if pod_uid:
        should_terms.extend(
            [
                {"term": {"kubernetes.pod.uid": pod_uid}},
                {"term": {"kubernetes.pod.uid.keyword": pod_uid}},
            ]
        )
    return {
        "size": max_records,
        "sort": [{"@timestamp": "desc"}],
        "query": {
            "bool": {
                "must": [{"range": {"@timestamp": {"gte": start, "lte": end}}}],
                "should": should_terms,
                "minimum_should_match": 1,
            }
        },
    }


def _parse_hits(payload: dict[str, Any], namespace: str, pod: str, max_records: int, source_desc: str) -> SourceResult:
    hits = (((payload.get("hits") or {}).get("hits")) or []) if isinstance(payload, dict) else []
    records: list[dict[str, Any]] = []
    for hit in hits[:max_records]:
        source = hit.get("_source") or {}
        records.append(
            {
                "timestamp": source.get("@timestamp"),
                "message": source.get("message") or source.get("log") or "",
                "kubernetes": source.get("kubernetes") or {},
            }
        )
    coverage = "present" if records else "empty"
    evidence = []
    if records:
        evidence.append(
            EvidenceItem(
                evidence_id=evidence_id("log", namespace, pod, "elasticsearch"),
                source_system="elasticsearch",
                dimension="logs",
                summary=f"Elasticsearch/Filebeat returned {len(records)} Pod log records（来源：{source_desc}）",
                confidence="high",
                directness="direct",
                payload={"records": records[:10], "source": source_desc},
            )
        )
    return SourceResult(
        "elasticsearch",
        "logs",
        coverage,
        records=records,
        evidence=evidence,
        query_meta={"source": source_desc, "matched": len(hits)},
        truncated=len(hits) > len(records),
    )


def _collect_via_exec(
    cfg: Any, index: str, namespace: str, pod: str, pod_uid: str | None, start: str, end: str, timeout: int, max_records: int
) -> SourceResult:
    username = _kube.secret_value(cfg.es_secret_namespace, cfg.es_secret_name, "username")
    password = _kube.secret_value(cfg.es_secret_namespace, cfg.es_secret_name, "password")
    if not username or not password:
        return SourceResult(
            "elasticsearch", "logs", "absent",
            error=f"无法从 secret {cfg.es_secret_namespace}/{cfg.es_secret_name} 获取 ES 凭证（用户名/密码为空）",
        )
    es_pod = cfg.es_exec_pod or _kube.find_pod(cfg.es_exec_namespace, cfg.es_exec_selector)
    if not es_pod:
        return SourceResult(
            "elasticsearch", "logs", "absent",
            error=f"未找到 Elasticsearch Pod（ns={cfg.es_exec_namespace} selector={cfg.es_exec_selector}）",
        )
    body = _query_body(namespace, pod, pod_uid, start, end, max_records)
    endpoint = f"{cfg.es_local_url.rstrip('/')}/{index}/_search"
    argv = ["curl", "-sk", "-u", f"{username}:{password}", "-H", "Content-Type: application/json", "-d", "@-", endpoint]
    ok, out, err = _kube.exec_in_pod(cfg.es_exec_namespace, es_pod, argv, input_text=json.dumps(body), timeout=timeout, stdin=True)
    if not ok:
        return SourceResult(
            "elasticsearch", "logs", "error",
            error=f"kubectl exec 查询 ES 失败（pod={es_pod}）: {err or out}",
            query_meta={"source": "kubectl_exec", "es_pod": es_pod, "index": index},
        )
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        return SourceResult(
            "elasticsearch", "logs", "error",
            error=f"ES 返回非法 JSON: {exc}",
            query_meta={"source": "kubectl_exec", "es_pod": es_pod, "raw_preview": out[:300]},
        )
    if isinstance(payload, dict) and payload.get("error"):
        return SourceResult(
            "elasticsearch", "logs", "error",
            error=f"ES 查询错误: {json.dumps(payload.get('error'), ensure_ascii=False)[:300]}",
            query_meta={"source": "kubectl_exec", "es_pod": es_pod, "index": index},
        )
    return _parse_hits(payload, namespace, pod, max_records, source_desc=f"kubectl exec {es_pod} -> {index}")


def _collect_via_http(
    elasticsearch_url: str, index: str, namespace: str, pod: str, pod_uid: str | None, start: str, end: str, timeout: int, max_records: int
) -> SourceResult:
    if requests is None:
        return SourceResult("elasticsearch", "logs", "error", error="requests is not installed")
    body = _query_body(namespace, pod, pod_uid, start, end, max_records)
    endpoint = urljoin(elasticsearch_url if elasticsearch_url.endswith("/") else elasticsearch_url + "/", f"{index}/_search")
    try:
        response = requests.post(endpoint, json=body, timeout=timeout, verify=False)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return SourceResult("elasticsearch", "logs", "error", error=str(exc), query_meta={"source": "direct_url", "index": index})
    return _parse_hits(payload, namespace, pod, max_records, source_desc=f"direct {endpoint}")


def collect_elasticsearch(
    *,
    elasticsearch_url: str,
    index: str,
    namespace: str,
    pod: str,
    pod_uid: str | None,
    start: str,
    end: str,
    timeout: int,
    max_records: int,
) -> SourceResult:
    cfg = get_config()
    # 1. Explicit direct URL wins.
    if elasticsearch_url:
        return _collect_via_http(elasticsearch_url, index, namespace, pod, pod_uid, start, end, timeout, max_records)
    # 2. kubectl-exec mode (default, credential auto-fetch).
    if cfg.es_exec_enabled:
        return _collect_via_exec(cfg, index, namespace, pod, pod_uid, start, end, timeout, max_records)
    # 3. Honestly absent.
    return SourceResult(
        "elasticsearch", "logs", "absent",
        error="ELASTICSEARCH_URL 未配置且 kubectl-exec 模式已禁用",
    )
