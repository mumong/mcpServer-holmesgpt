"""Prometheus collector for Pod-scoped AIOps cases."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

from ..schemas import EvidenceItem, SourceResult, evidence_id

from .. import _http

try:
    import requests
except ImportError:  # pragma: no cover - exercised in minimal images
    requests = None


def _query(url: str, query: str, timeout: int) -> dict[str, Any]:
    if requests is None:
        return {"status": "error", "error": "requests is not installed"}
    endpoint = urljoin(url if url.endswith("/") else url + "/", "api/v1/query")
    try:
        response = _http.SESSION.get(endpoint, params={"query": query}, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"status": "error", "error": "invalid payload"}
    except Exception as exc:
        return {"status": "error", "error": str(exc), "query": query}


def _query_range(
    url: str,
    query: str,
    start: str,
    end: str,
    step: int,
    timeout: int,
) -> dict[str, Any]:
    if requests is None:
        return {"status": "error", "error": "requests is not installed"}
    endpoint = urljoin(url if url.endswith("/") else url + "/", "api/v1/query_range")
    try:
        response = _http.SESSION.get(
            endpoint,
            params={"query": query, "start": start, "end": end, "step": max(1, int(step))},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"status": "error", "error": "invalid payload"}
    except Exception as exc:
        return {"status": "error", "error": str(exc), "query": query}


def _bytes_mib(value: float) -> str:
    return f"{value / (1024 * 1024):.1f}Mi"


def _sample_time(value: object) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(value)


def _is_application_container(labels: dict[str, Any]) -> bool:
    container = str(labels.get("container") or "").strip()
    image = str(labels.get("image") or "").lower()
    return bool(container and container != "POD" and "pause:" not in image and "/pause@" not in image)


def _series_points(series: dict[str, Any]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for item in series.get("values") or []:
        if not isinstance(item, list) or len(item) < 2:
            continue
        try:
            points.append((float(item[0]), float(item[1])))
        except (TypeError, ValueError):
            continue
    return points


def _limit_map(limit_results: list[dict[str, Any]]) -> dict[tuple[str, str, str], float]:
    limits: dict[tuple[str, str, str], float] = {}
    for item in limit_results:
        labels = item.get("metric") if isinstance(item.get("metric"), dict) else {}
        container = str(labels.get("container") or "").strip()
        if not container:
            continue
        value = item.get("value") or []
        try:
            numeric = float(value[1])
        except (IndexError, TypeError, ValueError):
            continue
        key = (
            str(labels.get("namespace") or ""),
            str(labels.get("pod") or ""),
            container,
        )
        limits[key] = max(limits.get(key, 0.0), numeric)
    return limits


def _bounded_samples(points: list[tuple[float, float]], limit: int = 5) -> list[str]:
    if len(points) <= limit:
        selected = points
    else:
        indexes = sorted({round(index * (len(points) - 1) / (limit - 1)) for index in range(limit)})
        selected = [points[index] for index in indexes]
    return [f"{_sample_time(timestamp)}={_bytes_mib(value)}" for timestamp, value in selected]


def _memory_highlights(
    range_results: list[dict[str, Any]],
    limit_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    limits = _limit_map(limit_results)
    grouped: dict[tuple[str, str, str], list[tuple[float, float]]] = {}
    for series in range_results:
        labels = series.get("metric") if isinstance(series.get("metric"), dict) else {}
        if not _is_application_container(labels):
            continue
        key = (
            str(labels.get("namespace") or ""),
            str(labels.get("pod") or ""),
            str(labels.get("container") or ""),
        )
        points = _series_points(series)
        if not points:
            continue
        # Duplicate kubelet scrapers usually expose identical logical series.
        existing = grouped.get(key)
        if existing is None or max(value for _, value in points) > max(value for _, value in existing):
            grouped[key] = points

    highlights: list[dict[str, Any]] = []
    for key, points in sorted(grouped.items()):
        _, pod, container = key
        maximum = max(value for _, value in points)
        limit = limits.get(key)
        item: dict[str, Any] = {
            "metric": "container_memory_working_set_bytes",
            "pod": pod,
            "container": container,
            "start": _bytes_mib(points[0][1]),
            "max": _bytes_mib(maximum),
            "last": _bytes_mib(points[-1][1]),
            "samples": _bounded_samples(points),
            "_max_bytes": maximum,
        }
        if limit:
            item["limit"] = _bytes_mib(limit)
            item["max_limit_ratio"] = round(maximum / limit, 4)
        highlights.append(item)
    highlights.sort(
        key=lambda item: (
            item.get("max_limit_ratio") is None,
            -float(item.get("max_limit_ratio") or 0.0),
            -float(item.get("_max_bytes") or 0.0),
        )
    )
    for item in highlights:
        item.pop("_max_bytes", None)
    return highlights


def collect_prometheus(
    *,
    prometheus_url: str,
    namespace: str,
    pod: str,
    pod_uid: str | None,
    timeout: int,
    start: str | None = None,
    end: str | None = None,
    step_seconds: int = 15,
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
        "kube_pod_container_resource_limits": (
            f'kube_pod_container_resource_limits{{namespace="{namespace}",pod="{pod}",resource="memory",unit="byte"}}'
        ),
    }
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    instant_results: dict[str, list[dict[str, Any]]] = {}
    for metric, query in queries.items():
        payload = _query(prometheus_url, query, timeout)
        if payload.get("status") == "success":
            values = (((payload.get("data") or {}).get("result")) or [])
            instant_results[metric] = values
            records.append({"metric": metric, "query": query, "result_count": len(values), "result": values[:10]})
        else:
            errors.append(str(payload.get("error") or payload))

    range_results: list[dict[str, Any]] = []
    if start and end:
        range_query = f"container_memory_working_set_bytes{selector}"
        range_payload = _query_range(prometheus_url, range_query, start, end, step_seconds, timeout)
        if range_payload.get("status") == "success":
            range_results = (((range_payload.get("data") or {}).get("result")) or [])
            records.append(
                {
                    "metric": "container_memory_working_set_bytes_range",
                    "query": range_query,
                    "result_count": len(range_results),
                    "result": range_results[:10],
                    "start": start,
                    "end": end,
                    "step_seconds": step_seconds,
                }
            )
        else:
            errors.append(str(range_payload.get("error") or range_payload))

    if records:
        total = sum(int(item.get("result_count") or 0) for item in records)
        coverage = "present" if total > 0 else "empty"
        highlights = _memory_highlights(
            range_results,
            instant_results.get("kube_pod_container_resource_limits", []),
        )
        evidence = [
            EvidenceItem(
                evidence_id=evidence_id("metric", namespace, pod, "prometheus"),
                source_system="prometheus",
                dimension="metrics",
                summary=(
                    f"Prometheus pod metrics queried: series={total} metrics={len(records)}"
                    + (
                        f"; memory max={highlights[0].get('max')} limit={highlights[0].get('limit', 'unknown')}"
                        if highlights
                        else ""
                    )
                ),
                confidence="high" if total else "medium",
                directness="direct",
                payload={"records": records, "pod_uid": pod_uid, "highlights": highlights},
            )
        ]
        return SourceResult("prometheus", "metrics", coverage, records=records, evidence=evidence)
    return SourceResult("prometheus", "metrics", "error", error="; ".join(errors) or "no metrics records")
