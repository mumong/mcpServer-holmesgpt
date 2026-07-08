"""Runtime configuration for AIOps observability MCP tools."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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


def get_config() -> RuntimeConfig:
    cases_dir = Path(os.environ.get("AIOPS_CASES_DIR", "/app/aiops-cases")).resolve()
    return RuntimeConfig(
        cases_dir=cases_dir,
        prometheus_url=os.environ.get("PROMETHEUS_URL", "").strip(),
        elasticsearch_url=(os.environ.get("ELASTICSEARCH_URL") or os.environ.get("ES_URL") or "").strip(),
        elasticsearch_index=os.environ.get("AIOPS_ES_INDEX", os.environ.get("ES_INDEX", "filebeat-*")).strip(),
        deepflow_clickhouse_url=(
            os.environ.get("DEEPFLOW_CLICKHOUSE_URL")
            or os.environ.get("CLICKHOUSE_URL")
            or ""
        ).strip(),
        deepflow_clickhouse_database=os.environ.get("DEEPFLOW_CLICKHOUSE_DATABASE", "flow_log").strip() or "flow_log",
        timeout_seconds=int(os.environ.get("AIOPS_SOURCE_TIMEOUT", "30")),
        max_records=int(os.environ.get("AIOPS_MAX_RECORDS", "50")),
    )


def runtime_status() -> dict[str, object]:
    cfg = get_config()
    return {
        "cases_dir": str(cfg.cases_dir),
        "cases_dir_exists": cfg.cases_dir.exists(),
        "prometheus_configured": bool(cfg.prometheus_url),
        "elasticsearch_configured": bool(cfg.elasticsearch_url),
        "deepflow_clickhouse_configured": bool(cfg.deepflow_clickhouse_url),
        "deepflow_clickhouse_database": cfg.deepflow_clickhouse_database,
        "timeout_seconds": cfg.timeout_seconds,
        "max_records": cfg.max_records,
        "data_dependency": False,
    }
