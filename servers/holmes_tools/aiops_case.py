"""AIOps case package MCP tools.

The tools intentionally return compact summaries and references. Large raw
evidence stays in the case package and can be fetched by ref only when needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List

from mcp.types import Tool

from servers.aiops_observability.case_builder import (
    case_summary,
    collect_case,
    get_case,
    get_evidence,
    search_cases,
)
from servers.aiops_observability.config import get_config, runtime_status
from servers.aiops_observability.evidence_store import (
    AGENT_VISIBLE_REF_FILES,
    EVIDENCE_FILES,
    line_count,
    read_jsonl,
    read_yaml,
    safe_case_dir,
)


AGENT_VISIBLE_FILES = [
    "case.yaml",
    "signals.jsonl",
    "timeline.jsonl",
    "entities.jsonl",
    "topology.jsonl",
]


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _cases_dir() -> Path:
    return get_config().cases_dir


def _safe_case_dir(case_id: str) -> Path:
    return safe_case_dir(_cases_dir(), case_id)


def _topology_summary(case_dir: Path) -> dict[str, Any]:
    return case_summary(case_dir).get("topology_summary", {})


def _evidence_inventory(case_dir: Path) -> list[dict[str, Any]]:
    inventory = []
    for relative in EVIDENCE_FILES:
        path = case_dir / relative
        inventory.append(
            {
                "ref": relative,
                "exists": path.exists(),
                "bytes": path.stat().st_size if path.exists() else 0,
                "records": line_count(path),
            }
        )
    return inventory


def _evidence_refs(case_dir: Path, limit: int = 40) -> list[str]:
    refs: list[str] = []
    for relative in EVIDENCE_FILES:
        path = case_dir / relative
        if path.suffix != ".jsonl":
            continue
        for item in read_jsonl(path):
            ref = item.get("evidence_id")
            if ref and ref not in refs:
                refs.append(str(ref))
            if len(refs) >= limit:
                return refs
    return refs


def _case_summary(case_dir: Path) -> dict[str, Any]:
    return case_summary(case_dir)


def _run_collect_aiops_case(arguments: dict[str, Any]) -> str:
    pod = str(arguments.get("pod") or "").strip()
    if not pod:
        return _json({"ok": False, "error": "pod is required"})
    namespace = str(arguments.get("namespace") or "").strip()
    scenario = str(arguments.get("scenario") or "auto").strip() or "auto"
    case_id = str(arguments.get("case_id") or f"{scenario}-{namespace or 'cluster'}-{pod}").strip()
    payload = collect_case({**arguments, "namespace": namespace, "pod": pod, "scenario": scenario, "case_id": case_id})
    return _json(payload)


def _run_get_aiops_case(arguments: dict[str, Any]) -> str:
    try:
        return _json(get_case(_cases_dir(), str(arguments.get("case_id") or "")))
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})


def _find_evidence_by_id(case_dir: Path, evidence_ref: str) -> dict[str, Any] | None:
    for relative in EVIDENCE_FILES:
        path = case_dir / relative
        if path.suffix != ".jsonl":
            continue
        for item in read_jsonl(path):
            if item.get("evidence_id") == evidence_ref:
                return {"ref": relative, "record": item}
    return None


def _run_get_aiops_case_evidence(arguments: dict[str, Any]) -> str:
    try:
        evidence_ref = str(arguments.get("evidence_ref") or "")
        max_bytes = int(arguments.get("max_bytes") or 12000)
        return _json(get_evidence(_cases_dir(), str(arguments.get("case_id") or ""), evidence_ref, max_bytes=max_bytes))
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})


def _run_search_aiops_cases(arguments: dict[str, Any]) -> str:
    return _json(search_cases(_cases_dir(), arguments))


_HANDLERS: Dict[str, Callable[[dict[str, Any]], str]] = {
    "collect_aiops_case": _run_collect_aiops_case,
    "get_aiops_case": _run_get_aiops_case,
    "get_aiops_case_evidence": _run_get_aiops_case_evidence,
    "search_aiops_cases": _run_search_aiops_cases,
}


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


TOOLS: List[Tool] = [
    Tool(
        name="collect_aiops_case",
        description="Collect a live Kubernetes abnormal Pod case into an AIOps case package and return a compact agent-safe summary with evidence refs.",
        inputSchema=_schema(
            {
                "namespace": {"type": "string", "description": "Kubernetes namespace. Optional only when pod name is cluster-unique."},
                "pod": {"type": "string", "description": "Abnormal Pod name."},
                "scenario": {"type": "string", "description": "auto, oomkilled, pod_terminating, or another supported scenario."},
                "case_id": {"type": "string", "description": "Optional stable case id."},
                "start": {"type": "string", "description": "Optional ISO collection start."},
                "end": {"type": "string", "description": "Optional ISO collection end."},
                "window_minutes": {"type": "integer", "description": "Optional collection window in minutes."},
            },
            ["pod"],
        ),
    ),
    Tool(
        name="get_aiops_case",
        description="Return an agent-safe compact summary for a collected AIOps case package.",
        inputSchema=_schema({"case_id": {"type": "string"}}, ["case_id"]),
    ),
    Tool(
        name="get_aiops_case_evidence",
        description="Read one evidence item or package-relative evidence file from a collected case package.",
        inputSchema=_schema(
            {
                "case_id": {"type": "string"},
                "evidence_ref": {"type": "string", "description": "Evidence id or package-relative evidence path."},
                "max_bytes": {"type": "integer", "description": "Maximum bytes for file content."},
            },
            ["case_id", "evidence_ref"],
        ),
    ),
    Tool(
        name="search_aiops_cases",
        description="Search collected AIOps case packages by namespace, pod, or abnormal type.",
        inputSchema=_schema(
            {
                "namespace": {"type": "string"},
                "pod": {"type": "string"},
                "abnormal_type": {"type": "string"},
            },
            [],
        ),
    ),
]


def call_tool(name: str, arguments: dict[str, Any]) -> str | None:
    handler = _HANDLERS.get(name)
    if not handler:
        return None
    return handler(arguments or {})
