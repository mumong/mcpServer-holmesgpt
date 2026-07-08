"""Prometheus collector for Pod-scoped AIOps cases."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

from ..schemas import EvidenceItem, SourceResult, evidence_id

try:
    import requests
except ImportError:  # pragma: no cover - exercised in minimal images
    requests = None


def _query(url: str, query: str, timeout: int) -> dict[str, Any]:
    if requests is None:
        return {"status": "error", "error": "requests is not installed"}
    endpoint = urljoin(url if url.endswith("/") else url + "/", "api/v1/query")
    try:
        response = requests.get(endpoint, params={"query": query}, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"status": "error", "error": "invalid payload"}
    except Exception as exc:
        return {"status": "error", "error": str(exc), "query": query}


def collect_prometheus(
    *,
    prometheus_url: str,
    namespace: str,
    pod: str,
    pod_uid: str | None,
    timeout: int,
) -> SourceResult:
    if not prometheus_url:
        return SourceResult("prometheus", "metrics", "absent", error="PROMETHEUS_URL is not configured")

    selectors = [f'namespace="{namespace}"', f'pod="{pod}"']
    selector = "{" + ",".join(selectors) + "}"
    queries = {
        "container_memory_working_set_bytes": f"container_memory_working_set_bytes{selector}",
        "kube_pod_container_status_restarts_total": f"kube_pod_container_status_restarts_total{selector}",
        "kube_pod_container_status_last_terminated_reason": f"kube_pod_container_status_last_terminated_reason{selector}",
        "kube_pod_status_phase": f"kube_pod_status_phase{selector}",
    }
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for metric, query in queries.items():
        payload = _query(prometheus_url, query, timeout)
        if payload.get("status") == "success":
            values = (((payload.get("data") or {}).get("result")) or [])
            records.append({"metric": metric, "query": query, "result_count": len(values), "result": values[:10]})
        else:
            errors.append(str(payload.get("error") or payload))

    if records:
        total = sum(int(item.get("result_count") or 0) for item in records)
        coverage = "present" if total > 0 else "empty"
        evidence = [
            EvidenceItem(
                evidence_id=evidence_id("metric", namespace, pod, "prometheus"),
                source_system="prometheus",
                dimension="metrics",
                summary=f"Prometheus pod metrics queried: series={total} metrics={len(records)}",
                confidence="high" if total else "medium",
                directness="direct",
                payload={"records": records, "pod_uid": pod_uid},
            )
        ]
        return SourceResult("prometheus", "metrics", coverage, records=records, evidence=evidence)
    return SourceResult("prometheus", "metrics", "error", error="; ".join(errors) or "no metrics records")

