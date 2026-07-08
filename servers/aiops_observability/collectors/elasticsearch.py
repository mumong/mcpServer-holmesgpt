"""Elasticsearch/Filebeat collector for Pod-scoped AIOps cases."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from ..schemas import EvidenceItem, SourceResult, evidence_id

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


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
    if not elasticsearch_url:
        return SourceResult("elasticsearch", "logs", "absent", error="ELASTICSEARCH_URL/ES_URL is not configured")
    if requests is None:
        return SourceResult("elasticsearch", "logs", "error", error="requests is not installed")

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
    body = {
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
    endpoint = urljoin(elasticsearch_url if elasticsearch_url.endswith("/") else elasticsearch_url + "/", f"{index}/_search")
    try:
        response = requests.post(endpoint, json=body, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return SourceResult("elasticsearch", "logs", "error", error=str(exc), query_meta={"index": index, "body": body})

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
                summary=f"Elasticsearch returned {len(records)} Pod log records in the case window",
                confidence="high",
                directness="direct",
                payload={"records": records[:10]},
            )
        )
    return SourceResult(
        "elasticsearch",
        "logs",
        coverage,
        records=records,
        evidence=evidence,
        query_meta={"index": index, "body": body, "endpoint": endpoint},
        truncated=len(hits) > len(records),
    )

