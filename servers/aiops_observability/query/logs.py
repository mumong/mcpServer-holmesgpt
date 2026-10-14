"""Query Pod-scoped Elasticsearch/Filebeat logs with model-selected filters."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urljoin

from ..collectors import _kube
from ..collectors.elasticsearch import es_credentials
from ..config import get_config
from .common import (
    MAX_QUERY_METADATA_BYTES,
    bounded_window,
    error_result,
    evidence_ref,
    query_descriptor,
    query_metadata_exceeds_budget,
    resolve_pod_scope,
)

from .. import _http

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


MAX_KEYWORDS = 8
MAX_LEVELS = 6
MAX_RECORDS = 50
MAX_BACKEND_RECORDS = 100
DEFAULT_SOURCE_FIELDS = [
    "@timestamp",
    "message",
    "log",
    "log.level",
    "level",
    "severity",
    "severity_text",
    "trace.id",
    "trace_id",
    "kubernetes.namespace",
    "kubernetes.namespace_name",
    "kubernetes.pod.name",
    "kubernetes.pod_name",
    "kubernetes.pod.uid",
    "kubernetes.container.name",
    "kubernetes.container_name",
]
SAFE_SOURCE_FIELD_RE = re.compile(
    r"[A-Za-z0-9_@-]+(?:\.[A-Za-z0-9_@-]+)*\Z"
)


def build_query_body(
    *,
    namespace: str,
    pod: str,
    pod_uid: str | None,
    start: str,
    end: str,
    container: str | None,
    keywords: list[str],
    keyword_mode: str,
    levels: list[str],
    trace_id: str | None,
    max_records: int,
    identity_basis: str,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    filters: list[dict[str, Any]] = [
        {"range": {"@timestamp": {"gte": start, "lte": end}}},
        {
            "bool": {
                "should": [
                    {"term": {"kubernetes.namespace": namespace}},
                    {"term": {"kubernetes.namespace_name": namespace}},
                ],
                "minimum_should_match": 1,
            }
        },
    ]
    if identity_basis == "pod_uid" and pod_uid:
        filters.append(
            {
                "bool": {
                    "should": [
                        {"term": {"kubernetes.pod.uid": pod_uid}},
                        {"term": {"kubernetes.pod.uid.keyword": pod_uid}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    else:
        filters.append(
            {
                "bool": {
                    "should": [
                        {"term": {"kubernetes.pod.name": pod}},
                        {"term": {"kubernetes.pod_name": pod}},
                        {"term": {"kubernetes.pod.name.keyword": pod}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    if container:
        filters.append(
            {
                "bool": {
                    "should": [
                        {"term": {"kubernetes.container.name": container}},
                        {"term": {"kubernetes.container_name": container}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    if levels:
        filters.append(
            {
                "bool": {
                    "should": [
                        {"terms": {field: levels}}
                        for field in ("log.level", "level", "severity", "severity_text")
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    if trace_id:
        filters.append(
            {
                "bool": {
                    "should": [
                        {"term": {field: trace_id}}
                        for field in ("trace.id", "trace_id", "trace.id.keyword")
                    ],
                    "minimum_should_match": 1,
                }
            }
        )

    must: list[dict[str, Any]] = []
    keyword_clauses = [
        {
            "bool": {
                "should": [
                    {"match_phrase": {"message": keyword}},
                    {"match_phrase": {"log": keyword}},
                ],
                "minimum_should_match": 1,
            }
        }
        for keyword in keywords
    ]
    if keyword_clauses:
        if keyword_mode == "any":
            must.append(
                {
                    "bool": {
                        "should": keyword_clauses,
                        "minimum_should_match": 1,
                    }
                }
            )
        else:
            must.extend(keyword_clauses)

    return {
        "size": MAX_BACKEND_RECORDS,
        "_source": _source_fields(fields),
        "sort": [{"@timestamp": "desc"}],
        "query": {"bool": {"filter": filters, "must": must}},
    }


def execute(arguments: dict[str, Any]) -> dict[str, Any]:
    namespace = str(arguments.get("namespace") or "").strip()
    pod = str(arguments.get("pod") or "").strip()
    purpose = str(arguments.get("purpose") or "").strip()
    keywords = [
        str(value).strip()
        for value in arguments.get("keywords") or []
        if str(value).strip()
    ]
    levels = [
        str(value).strip()
        for value in arguments.get("levels") or []
        if str(value).strip()
    ]
    if len(keywords) > MAX_KEYWORDS:
        return error_result(
            source_system="elasticsearch",
            dimension="logging",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="too_many_keywords",
            message=f"keywords must contain at most {MAX_KEYWORDS} items",
        )
    if len(levels) > MAX_LEVELS:
        return error_result(
            source_system="elasticsearch",
            dimension="logging",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="too_many_levels",
            message=f"levels must contain at most {MAX_LEVELS} items",
        )
    if any(len(value) > 120 for value in keywords):
        return error_result(
            source_system="elasticsearch",
            dimension="logging",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="keyword_too_long",
            message="each keyword must be at most 120 characters",
        )
    keyword_mode = str(arguments.get("keyword_mode") or "any").strip().lower()
    if keyword_mode not in {"any", "all", "phrase"}:
        return error_result(
            source_system="elasticsearch",
            dimension="logging",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="invalid_keyword_mode",
            message="keyword_mode must be any, all or phrase",
        )
    if keyword_mode == "phrase" and keywords:
        keywords = [" ".join(keywords)]
        keyword_mode = "all"

    requested_fields = [
        str(value).strip()
        for value in arguments.get("fields") or []
        if str(value).strip()
    ]
    invalid_fields = [
        field
        for field in requested_fields
        if len(field) > 128 or not SAFE_SOURCE_FIELD_RE.fullmatch(field)
    ]
    if invalid_fields:
        return error_result(
            source_system="elasticsearch",
            dimension="logging",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="invalid_fields",
            message=(
                "fields contains unsupported values: "
                + ", ".join(invalid_fields[:4])
            ),
        )

    max_records = min(MAX_RECORDS, max(1, int(arguments.get("max_records") or 20)))
    entity, entity_error = resolve_pod_scope(
        namespace,
        pod,
        arguments.get("pod_uid"),
    )
    if entity_error:
        return error_result(
            source_system="elasticsearch",
            dimension="logging",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code=entity_error["code"],
            message=entity_error["message"],
        )
    assert entity is not None
    container = str(arguments.get("container") or "").strip() or None
    if container and container not in entity.get("containers", []):
        return error_result(
            source_system="elasticsearch",
            dimension="logging",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="container_not_found",
            message=f"container {container} is not present in the target Pod",
            entity=entity,
        )
    window, window_error = bounded_window(
        arguments,
        not_before=entity.get("creation_timestamp"),
    )
    if window_error:
        return error_result(
            source_system="elasticsearch",
            dimension="logging",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code=window_error["code"],
            message=window_error["message"],
            entity=entity,
        )
    assert window is not None

    common = {
        "namespace": namespace,
        "pod": pod,
        "pod_uid": entity.get("pod_uid"),
        "start": window["start"],
        "end": window["end"],
        "container": container,
        "keywords": keywords,
        "keyword_mode": keyword_mode,
        "levels": levels,
        "trace_id": str(arguments.get("trace_id") or "").strip() or None,
        "max_records": max_records,
        "fields": requested_fields or None,
    }
    identity_basis = "pod_uid" if entity.get("pod_uid") else "name_time_fallback"
    body = build_query_body(
        **common,
        identity_basis=identity_basis,
    )
    if query_metadata_exceeds_budget(body):
        return error_result(
            source_system="elasticsearch",
            dimension="logging",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="query_metadata_too_large",
            message=(
                "generated Elasticsearch DSL exceeds the "
                f"{MAX_QUERY_METADATA_BYTES}-byte metadata budget"
            ),
            entity=entity,
            query=query_descriptor(body, executed=False),
        )
    payload = _query_elasticsearch(body)
    if payload.get("error"):
        backend_error = str(payload.get("error"))
        return error_result(
            source_system="elasticsearch",
            dimension="logging",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="elasticsearch_error",
            message=backend_error,
            entity=entity,
            query={
                "index": get_config().elasticsearch_index,
                "identity_basis": identity_basis,
                "time_range": window,
                "dsl": body,
                "executions": [
                    {
                        "identity_basis": identity_basis,
                        "dsl_ref": "query.dsl",
                        "status": "error",
                        "error_ref": "error.message",
                    }
                ],
            },
        )

    uid_hits = _hits(payload)
    query_executions = [
        {
            "identity_basis": identity_basis,
            "dsl_ref": "query.dsl",
            "hit_count": len(uid_hits),
        }
    ]
    allow_fallback = bool(arguments.get("allow_name_fallback", True))
    if (
        not uid_hits
        and identity_basis == "pod_uid"
        and allow_fallback
    ):
        uid_body = body
        query_executions = [
            {
                "identity_basis": "pod_uid",
                "query": query_descriptor(uid_body, executed=True),
                "hit_count": len(uid_hits),
            }
        ]
        identity_basis = "name_time_fallback"
        body = build_query_body(
            **common,
            identity_basis=identity_basis,
        )
        planned_query_meta = {
            "index": get_config().elasticsearch_index,
            "identity_basis": identity_basis,
            "uid_query_hits": len(uid_hits),
            "time_range": window,
            "dsl": body,
            "executions": [
                *query_executions,
                {
                    "identity_basis": identity_basis,
                    "dsl_ref": "query.dsl",
                },
            ],
        }
        if query_metadata_exceeds_budget(planned_query_meta):
            return error_result(
                source_system="elasticsearch",
                dimension="logging",
                namespace=namespace,
                pod=pod,
                purpose=purpose,
                code="query_metadata_too_large",
                message=(
                    "generated Elasticsearch fallback query chain exceeds the "
                    f"{MAX_QUERY_METADATA_BYTES}-byte metadata budget"
                ),
                entity=entity,
                query={
                    "index": get_config().elasticsearch_index,
                    "identity_basis": "pod_uid",
                    "uid_query_hits": len(uid_hits),
                    "time_range": window,
                    "dsl": uid_body,
                    "executions": [
                        {
                            "identity_basis": "pod_uid",
                            "dsl_ref": "query.dsl",
                            "hit_count": len(uid_hits),
                        }
                    ],
                    "rejected_next_query": query_descriptor(
                        body,
                        executed=False,
                    ),
                },
            )
        payload = _query_elasticsearch(body)
        if payload.get("error"):
            backend_error = str(payload.get("error"))
            return error_result(
                source_system="elasticsearch",
                dimension="logging",
                namespace=namespace,
                pod=pod,
                purpose=purpose,
                code="elasticsearch_error",
                message=backend_error,
                entity=entity,
                query={
                    **planned_query_meta,
                    "executions": [
                        *query_executions,
                        {
                            "identity_basis": identity_basis,
                            "dsl_ref": "query.dsl",
                            "status": "error",
                            "error_ref": "error.message",
                        },
                    ],
                },
            )
        query_executions.append(
            {
                "identity_basis": identity_basis,
                "dsl_ref": "query.dsl",
                "hit_count": len(_hits(payload)),
            }
        )

    current_entity, lifecycle_error = resolve_pod_scope(
        namespace,
        pod,
        entity.get("pod_uid"),
    )
    if lifecycle_error or _entity_lifecycle_changed(entity, current_entity):
        changed_entity = {**entity, "lifecycle_changed": True}
        return error_result(
            source_system="elasticsearch",
            dimension="logging",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="pod_lifecycle_changed",
            message=str(
                (lifecycle_error or {}).get("message")
                or "Pod identity changed while Elasticsearch query was running"
            ),
            entity=changed_entity,
            query={
                "index": get_config().elasticsearch_index,
                "identity_basis": identity_basis,
                "time_range": window,
                "executions": query_executions,
            },
        )

    normalized_records = _dedupe_records(
        _hits(payload),
        requested_fields=requested_fields,
        current_pod_uid=str(entity.get("pod_uid") or ""),
        identity_basis=identity_basis,
    )
    records = normalized_records[:max_records]
    samples = records
    facts: list[dict[str, Any]] = []
    for sample in samples:
        ref = evidence_ref("log", namespace, pod, sample)
        sample["ref"] = ref
        facts.append(
            {
                "ref": ref,
                "source_system": "elasticsearch",
                "dimension": "logging",
                "entity": "/".join(
                    part
                    for part in (namespace, pod, sample.get("container"))
                    if part
                ),
                "pod_uid": sample.get("pod_uid"),
                "observed_at": sample.get("timestamp"),
                "name": "log.message",
                "value": sample.get("message"),
                "value_original_length": sample.get("message_original_length"),
                "value_sha256": sample.get("message_sha256"),
                "value_truncated": False,
                "level": sample.get("level"),
                "trace_id": sample.get("trace_id"),
                "raw_ref": sample.get("raw_ref"),
                "fields": sample.get("fields"),
                "directness": sample.get("directness") or "related_context",
            }
        )
    query_meta = {
        "index": get_config().elasticsearch_index,
        "identity_basis": identity_basis,
        "uid_query_hits": len(uid_hits),
        "backend_total": _backend_total(payload),
        "time_range": window,
        "container": container,
        "keywords": keywords,
        "keyword_mode": keyword_mode,
        "levels": levels,
        "trace_id": common["trace_id"],
        "dsl": body,
        "executions": query_executions,
        "dsl_sha256": hashlib.sha256(
            json.dumps(body, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16],
    }
    backend_total = _backend_total(payload)
    backend_matched = int(backend_total.get("value") or 0)
    backend_retrieved = len(_hits(payload))
    truncated = _backend_truncated(payload, len(records))
    truncation_stages = []
    if (
        str(backend_total.get("relation") or "eq") != "eq"
        or backend_matched > backend_retrieved
    ):
        truncation_stages.append(
            {
                "stage": "backend",
                "reason": "max_hits",
                "before": backend_matched,
                "after": backend_retrieved,
            }
        )
    if len(normalized_records) > len(records):
        truncation_stages.append(
            {
                "stage": "tool_query",
                "reason": "max_records",
                "before": len(normalized_records),
                "after": len(records),
            }
        )
    return {
        "ok": True,
        "status": "query_succeeded",
        "source_system": "elasticsearch",
        "dimension": "logging",
        "entity": entity,
        "purpose": purpose,
        "coverage": "present" if records else "empty",
        "directness": (
            "direct"
            if records
            and all(item.get("directness") == "direct" for item in records)
            else "related_context"
            if records
            else "direct"
        ),
        "query": query_meta,
        "facts": facts,
        "samples": samples,
        "evidence_refs": [fact["ref"] for fact in facts],
        "counts": {
            "matched": backend_matched,
            "retrieved": backend_retrieved,
            "normalized": len(normalized_records),
            "returned": len(records),
            "dropped": max(0, backend_matched - len(records)),
        },
        "truncation": {
            "truncated": truncated,
            "stages": truncation_stages,
        },
        "truncated": truncated,
        "limits": {
            "max_records": MAX_RECORDS,
            "requested_max_records": max_records,
            "max_backend_records": MAX_BACKEND_RECORDS,
            "max_keywords": MAX_KEYWORDS,
        },
    }


def _query_elasticsearch(body: dict[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    if cfg.elasticsearch_url:
        if requests is None:
            return {"error": "requests is not installed"}
        endpoint = urljoin(
            cfg.elasticsearch_url
            if cfg.elasticsearch_url.endswith("/")
            else cfg.elasticsearch_url + "/",
            f"{cfg.elasticsearch_index}/_search",
        )
        try:
            response = _http.SESSION.post(
                endpoint,
                json=body,
                timeout=cfg.timeout_seconds,
                verify=False,
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {"error": "invalid Elasticsearch payload"}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
    if not cfg.es_exec_enabled:
        return {"error": "Elasticsearch is not configured"}
    username, password = es_credentials(cfg)
    es_pod = cfg.es_exec_pod or _kube.find_pod(
        cfg.es_exec_namespace,
        cfg.es_exec_selector,
    )
    if not username or not password or not es_pod:
        return {"error": "Elasticsearch credentials or Pod are unavailable"}
    endpoint = f"{cfg.es_local_url.rstrip('/')}/{cfg.elasticsearch_index}/_search"
    ok, out, error = _kube.exec_in_pod(
        cfg.es_exec_namespace,
        es_pod,
        [
            "curl",
            "-sk",
            "-u",
            f"{username}:{password}",
            "-H",
            "Content-Type: application/json",
            "-d",
            "@-",
            endpoint,
        ],
        input_text=json.dumps(body),
        timeout=cfg.timeout_seconds,
        stdin=True,
    )
    if not ok:
        return {"error": error or out}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        return {"error": f"invalid Elasticsearch JSON: {exc}"}
    return payload if isinstance(payload, dict) else {"error": "invalid Elasticsearch payload"}


def _hits(payload: dict[str, Any]) -> list[dict[str, Any]]:
    hits = ((payload.get("hits") or {}).get("hits")) if isinstance(payload, dict) else []
    return hits if isinstance(hits, list) else []


def _dedupe_records(
    hits: list[dict[str, Any]],
    *,
    requested_fields: list[str],
    current_pod_uid: str = "",
    identity_basis: str = "pod_uid",
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for hit in hits:
        source = hit.get("_source") if isinstance(hit, dict) else {}
        source = source if isinstance(source, dict) else {}
        timestamp = str(source.get("@timestamp") or "")
        message = source.get("message")
        if not isinstance(message, str):
            log_value = source.get("log")
            message = log_value if isinstance(log_value, str) else ""
        kubernetes = source.get("kubernetes") if isinstance(source.get("kubernetes"), dict) else {}
        container = _nested_value(kubernetes, "container", "name") or kubernetes.get("container_name")
        observed_uid = str(_nested_value(kubernetes, "pod", "uid") or "").strip()
        directness = (
            "direct"
            if identity_basis == "pod_uid"
            or (current_pod_uid and observed_uid == current_pod_uid)
            else "related_context"
        )
        key = (timestamp, str(container or ""), message)
        if key in seen:
            continue
        seen.add(key)
        message_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
        raw_ref = {
            "source_system": "elasticsearch",
            "index": str(hit.get("_index") or ""),
            "document_id": str(hit.get("_id") or ""),
        }
        selected_fields = {
            field: value
            for field in requested_fields
            if (value := _field_value(source, field)) is not None
        }
        records.append(
            {
                "timestamp": timestamp or None,
                "message": message,
                "message_original_length": len(message),
                "message_sha256": message_sha256,
                "message_truncated": False,
                "container": container,
                "pod_uid": observed_uid or None,
                "directness": directness,
                "level": (
                    _nested_value(source, "log", "level")
                    or source.get("level")
                    or source.get("severity")
                ),
                "trace_id": (
                    _nested_value(source, "trace", "id")
                    or source.get("trace_id")
                ),
                "raw_ref": raw_ref,
                "fields": selected_fields,
            }
        )
    return records


def _nested_value(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _field_value(source: dict[str, Any], field: str) -> Any:
    if field in source:
        return source[field]
    return _nested_value(source, *field.split("."))


def _source_fields(fields: list[str] | None) -> list[str]:
    merged = list(DEFAULT_SOURCE_FIELDS)
    seen = set(merged)
    for value in fields or []:
        field = str(value or "").strip()
        if not field or field in seen:
            continue
        seen.add(field)
        merged.append(field)
    return merged


def _backend_truncated(payload: dict[str, Any], retained_count: int) -> bool:
    hits_wrapper = payload.get("hits") if isinstance(payload, dict) else {}
    hits_wrapper = hits_wrapper if isinstance(hits_wrapper, dict) else {}
    total = hits_wrapper.get("total")
    if isinstance(total, dict):
        try:
            value = int(total.get("value") or 0)
        except (TypeError, ValueError):
            value = 0
        relation = str(total.get("relation") or "eq")
        return relation != "eq" or value > retained_count
    hits = hits_wrapper.get("hits")
    return isinstance(hits, list) and len(hits) >= MAX_BACKEND_RECORDS


def _backend_total(payload: dict[str, Any]) -> dict[str, Any]:
    hits_wrapper = payload.get("hits") if isinstance(payload, dict) else {}
    hits_wrapper = hits_wrapper if isinstance(hits_wrapper, dict) else {}
    total = hits_wrapper.get("total")
    if isinstance(total, dict):
        return {
            "value": total.get("value"),
            "relation": total.get("relation") or "eq",
        }
    hits = hits_wrapper.get("hits")
    return {
        "value": len(hits) if isinstance(hits, list) else 0,
        "relation": "eq",
    }


def _entity_lifecycle_changed(
    before: dict[str, Any],
    after: dict[str, Any] | None,
) -> bool:
    if not isinstance(after, dict):
        return True
    return any(
        str(before.get(key) or "") != str(after.get(key) or "")
        for key in ("pod_uid", "creation_timestamp")
    )
