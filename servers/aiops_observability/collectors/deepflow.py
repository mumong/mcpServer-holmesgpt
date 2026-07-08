"""DeepFlow ClickHouse collector for Pod-scoped flow/trace context."""

from __future__ import annotations

from typing import Any
import re

from ..schemas import EvidenceItem, SourceResult, evidence_id

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


def collect_deepflow(
    *,
    clickhouse_url: str,
    clickhouse_database: str,
    namespace: str,
    pod: str,
    pod_ip: str | None,
    node: str | None,
    start: str,
    end: str,
    timeout: int,
    max_records: int,
) -> SourceResult:
    if not clickhouse_url:
        return SourceResult("deepflow", "tracing", "absent", error="DEEPFLOW_CLICKHOUSE_URL/CLICKHOUSE_URL is not configured")
    if requests is None:
        return SourceResult("deepflow", "tracing", "error", error="requests is not installed")
    if not pod_ip:
        return SourceResult("deepflow", "tracing", "empty", error="pod_ip is empty")

    database = _safe_identifier(clickhouse_database or "flow_log")
    sql = f"""
SELECT time, ip4_0, ip4_1, l7_protocol, request_type, response_status, trace_id
FROM {database}.l7_flow_log
WHERE time >= toDateTime('{_clickhouse_datetime(start)}')
  AND time <= toDateTime('{_clickhouse_datetime(end)}')
  AND (ip4_0 = '{pod_ip}' OR ip4_1 = '{pod_ip}')
LIMIT {int(max_records)}
FORMAT JSON
""".strip()
    try:
        response = requests.post(clickhouse_url, data=sql.encode("utf-8"), timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return SourceResult(
            "deepflow",
            "tracing",
            "error",
            error=str(exc),
            query_meta={"sql": sql, "pod_ip": pod_ip, "node": node, "database": database},
        )

    rows = payload.get("data") if isinstance(payload, dict) else []
    rows = rows if isinstance(rows, list) else []
    coverage = "present" if rows else "empty"
    evidence = []
    if rows:
        evidence.append(
            EvidenceItem(
                evidence_id=evidence_id("deepflow", namespace, pod, "pod-ip-flow"),
                source_system="deepflow",
                dimension="tracing",
                summary=f"DeepFlow returned {len(rows)} direct Pod IP L7 flow records",
                confidence="medium",
                directness="direct",
                trace_correlation={"pod_ip": pod_ip, "trace_ids": [r.get("trace_id") for r in rows if r.get("trace_id")][:5]},
                payload={"records": rows[:10]},
            )
        )
    return SourceResult(
        "deepflow",
        "tracing",
        coverage,
        records=rows[:max_records],
        evidence=evidence,
        query_meta={"sql": sql, "pod_ip": pod_ip, "node": node, "database": database},
        truncated=len(rows) > max_records,
    )


def _safe_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        return "flow_log"
    return value


def _clickhouse_datetime(value: str) -> str:
    normalized = value.strip().replace("Z", "").replace("T", " ")
    if "." in normalized:
        normalized = normalized.split(".", 1)[0]
    if "+" in normalized:
        normalized = normalized.split("+", 1)[0]
    return normalized
