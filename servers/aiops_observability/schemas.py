"""Shared schema helpers for AIOps observability MCP tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time_window(
    *,
    start: str | None = None,
    end: str | None = None,
    window_minutes: int | None = None,
) -> "TimeWindow":
    end_dt = parse_datetime(end) if end else utc_now()
    minutes = max(1, int(window_minutes or 30))
    start_dt = parse_datetime(start) if start else end_dt - timedelta(minutes=minutes)
    return TimeWindow(start=isoformat(start_dt) or "", end=isoformat(end_dt) or "", timezone="UTC")


def parse_datetime(value: str) -> datetime:
    """Parse ISO-8601 (``Z`` accepted) to aware UTC; naive values are UTC."""
    text = str(value or "").strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class TimeWindow:
    start: str
    end: str
    timezone: str = "UTC"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceItem:
    evidence_id: str
    source_system: str
    dimension: str
    summary: str
    timestamp: str | None = None
    severity: str = "info"
    confidence: str = "medium"
    directness: str | None = None
    trace_correlation: dict[str, Any] | None = None
    raw_ref: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        return {key: value for key, value in record.items() if value not in (None, {}, [])}


@dataclass
class SourceResult:
    source_system: str
    dimension: str
    coverage: str
    records: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    query_meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    truncated: bool = False

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "ok": self.coverage != "error",
            "source_system": self.source_system,
            "dimension": self.dimension,
            "coverage": self.coverage,
            "records": self.records,
            "query_meta": self.query_meta,
            "truncated": self.truncated,
        }
        if self.error:
            payload["error"] = self.error
        if self.evidence:
            payload["evidence"] = [item.to_record() for item in self.evidence]
        return payload


def evidence_id(prefix: str, *parts: str | None) -> str:
    clean_parts = [str(part).strip().replace("/", "-").replace(" ", "-") for part in parts if part]
    return "-".join([prefix] + clean_parts).lower()
