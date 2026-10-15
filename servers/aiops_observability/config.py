"""Runtime configuration for AIOps observability MCP tools."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RuntimeConfig:
    cases_dir: Path
    prometheus_url: str
    elasticsearch_url: str
    elasticsearch_index: str
    deepflow_clickhouse_url: str
    deepflow_clickhouse_database: str
    timeout_seconds: int
    max_records: int
    # kubectl-exec collection mode (mirrors the data-project scripts): query the
    # backend by exec-ing into its Pod, auto-fetching credentials from a secret.
    # Enabled by default so logging/tracing work without brittle direct URLs.
    es_exec_enabled: bool
    es_exec_namespace: str
    es_exec_pod: str
    es_exec_selector: str
    es_secret_namespace: str
    es_secret_name: str
    es_local_url: str
    clickhouse_exec_enabled: bool
    clickhouse_exec_namespace: str
    clickhouse_exec_pod: str
    clickhouse_exec_selector: str
    tempo_exec_enabled: bool
    tempo_exec_namespace: str
    tempo_exec_pod: str
    tempo_exec_selector: str
    tempo_local_url: str


def get_config() -> RuntimeConfig:
    cases_dir = Path(os.environ.get("AIOPS_CASES_DIR", "/app/aiops-cases")).resolve()
    return RuntimeConfig(
        cases_dir=cases_dir,
        prometheus_url=os.environ.get("PROMETHEUS_URL", "").strip(),
        elasticsearch_url=(os.environ.get("ELASTICSEARCH_URL") or os.environ.get("ES_URL") or "").strip(),
        elasticsearch_index=os.environ.get(
            "AIOPS_ES_INDEX", os.environ.get("ES_INDEX", "filebeat-*")
        ).strip(),
        deepflow_clickhouse_url=(
            os.environ.get("DEEPFLOW_CLICKHOUSE_URL") or os.environ.get("CLICKHOUSE_URL") or ""
        ).strip(),
        deepflow_clickhouse_database=os.environ.get("DEEPFLOW_CLICKHOUSE_DATABASE", "flow_log").strip()
        or "flow_log",
        timeout_seconds=int(os.environ.get("AIOPS_SOURCE_TIMEOUT", "30")),
        max_records=int(os.environ.get("AIOPS_MAX_RECORDS", "50")),
        es_exec_enabled=_bool_env("AIOPS_ES_EXEC_ENABLED", True),
        es_exec_namespace=os.environ.get("AIOPS_ES_EXEC_NAMESPACE", "xnet").strip() or "xnet",
        es_exec_pod=os.environ.get("AIOPS_ES_EXEC_POD", "elasticsearch-master-0").strip(),
        es_exec_selector=os.environ.get("AIOPS_ES_EXEC_SELECTOR", "app=elasticsearch-master").strip(),
        es_secret_namespace=os.environ.get("AIOPS_ES_SECRET_NAMESPACE", "xnet").strip() or "xnet",
        es_secret_name=os.environ.get("AIOPS_ES_SECRET_NAME", "elasticsearch-master-credentials").strip(),
        es_local_url=os.environ.get("AIOPS_ES_LOCAL_URL", "https://127.0.0.1:9200").strip(),
        clickhouse_exec_enabled=_bool_env("AIOPS_CLICKHOUSE_EXEC_ENABLED", True),
        clickhouse_exec_namespace=os.environ.get("AIOPS_CLICKHOUSE_EXEC_NAMESPACE", "xnet").strip() or "xnet",
        clickhouse_exec_pod=os.environ.get("AIOPS_CLICKHOUSE_EXEC_POD", "observability-clickhouse-0").strip(),
        clickhouse_exec_selector=os.environ.get(
            "AIOPS_CLICKHOUSE_EXEC_SELECTOR", "app.kubernetes.io/name=clickhouse"
        ).strip(),
        tempo_exec_enabled=_bool_env("AIOPS_TEMPO_EXEC_ENABLED", True),
        tempo_exec_namespace=os.environ.get("AIOPS_TEMPO_EXEC_NAMESPACE", "xnet").strip() or "xnet",
        tempo_exec_pod=os.environ.get("AIOPS_TEMPO_EXEC_POD", "").strip(),
        tempo_exec_selector=os.environ.get("AIOPS_TEMPO_EXEC_SELECTOR", "app=lgtm").strip(),
        tempo_local_url=os.environ.get("AIOPS_TEMPO_LOCAL_URL", "http://127.0.0.1:3200").strip(),
    )


def runtime_status() -> dict[str, object]:
    cfg = get_config()
    return {
        "cases_dir": str(cfg.cases_dir),
        "cases_dir_exists": cfg.cases_dir.exists(),
        "prometheus_configured": bool(cfg.prometheus_url),
        "elasticsearch_configured": bool(cfg.elasticsearch_url) or cfg.es_exec_enabled,
        "elasticsearch_mode": "direct_url"
        if cfg.elasticsearch_url
        else ("kubectl_exec" if cfg.es_exec_enabled else "disabled"),
        "deepflow_clickhouse_configured": bool(cfg.deepflow_clickhouse_url) or cfg.clickhouse_exec_enabled,
        "deepflow_mode": "direct_url"
        if cfg.deepflow_clickhouse_url
        else ("kubectl_exec" if cfg.clickhouse_exec_enabled else "disabled"),
        "deepflow_clickhouse_database": cfg.deepflow_clickhouse_database,
        "timeout_seconds": cfg.timeout_seconds,
        "max_records": cfg.max_records,
        "data_dependency": False,
    }
