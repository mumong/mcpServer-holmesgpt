"""File-first case package storage for AIOps MCP tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


AGENT_VISIBLE_FILES = [
    "case.yaml",
    "signals.jsonl",
    "timeline.jsonl",
    "entities.jsonl",
    "topology.jsonl",
]

EVIDENCE_FILES = [
    "evidence/metrics.jsonl",
    "evidence/logs.jsonl",
    "evidence/deepflow_l4.jsonl",
    "evidence/deepflow_l7.jsonl",
    "evidence/k8s_events.jsonl",
    "evidence/k8s_describe.txt",
    "evidence/k8s_pod.yaml",
]

AGENT_VISIBLE_REF_FILES = set(AGENT_VISIBLE_FILES + EVIDENCE_FILES)


def safe_case_dir(cases_dir: Path, case_id: str) -> Path:
    if not case_id or "/" in case_id or "\\" in case_id or ".." in case_id.split("."):
        raise ValueError("invalid case_id")
    root = cases_dir.resolve()
    path = (root / case_id).resolve()
    if root not in path.parents and path != root:
        raise ValueError("invalid case_id")
    return path


def safe_relative_path(root: Path, relative: str) -> Path:
    if not relative or relative.startswith("/") or "\\" in relative:
        raise ValueError("invalid evidence_ref")
    path = (root / relative).resolve()
    if root.resolve() not in path.parents and path != root.resolve():
        raise ValueError("invalid evidence_ref")
    return path


def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows),
        encoding="utf-8",
    )


def read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
        if limit and len(rows) >= limit:
            break
    return rows


def line_count(path: Path) -> int:
    if not path.exists():
        return 0
    if path.suffix in {".jsonl", ".txt"}:
        return sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
    return 1 if path.stat().st_size > 0 else 0

