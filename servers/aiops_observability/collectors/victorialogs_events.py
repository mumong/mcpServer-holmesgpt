"""Kubernetes events from VictoriaLogs (victoria profile, research R5).

The new stack only ships event-exporter output. Each event is one text line in
``_msg``; ``level`` is always INFO and the real type is inside the text, the
reason key is spelled ``reasoni``, and ``kubernetes.pod_name`` is the exporter's
own Pod, not the involved object. We query by phrase and then keep only events
whose involved object matches the target exactly.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .. import _http

_FIELD = re.compile(
    r"^event name: (?P<event>[^,]*),count: (?P<count>\d+),involvedObject_namespace: (?P<ns>[^,]*),"
    r"involvedObject_kind: (?P<kind>[^,]*),involvedObject_name: (?P<name>[^,]*),"
    r"reasoni?: (?P<reason>[^,]*),type: (?P<type>[^,]*),message: (?P<message>.*)$",
    re.S,
)


def parse_event(text: str) -> Optional[Dict[str, Any]]:
    match = _FIELD.match(text or "")
    if not match:
        return None
    return {
        "event": match["event"],
        "count": int(match["count"]),
        "involved_namespace": match["ns"],
        "involved_kind": match["kind"],
        "involved_name": match["name"],
        "reason": match["reason"],
        "type": match["type"],
        "message": match["message"],
    }


def _quote(value: str) -> str:
    return json.dumps(value)  # LogsQL accepts JSON-style double-quoted strings


def build_query(cluster_field: str, cluster: str, namespace: str, pod: str) -> str:
    return (
        f"{cluster_field}:={_quote(cluster)} "
        f"{_quote('involvedObject_namespace: ' + namespace)} "
        f"{_quote('involvedObject_name: ' + pod)}"
        " | sort by (_time desc)"
    )


def _fetch(
    base_url: str, query: str, start: str, end: str, limit: int, timeout: int
) -> Tuple[List[Dict], Optional[str]]:
    url = base_url.rstrip("/") + "/select/logsql/query"
    try:
        response = _http.SESSION.get(
            url,
            params={"query": query, "start": start, "end": end, "limit": str(limit)},
            timeout=timeout,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return [], f"VictoriaLogs query failed: {exc}"
    lines = []
    for raw in response.text.splitlines():
        raw = raw.strip()
        if raw:
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return lines, None


def query_pod_events(
    *,
    base_url: str,
    cluster_field: str,
    cluster: str,
    namespace: str,
    pod: str,
    start: str,
    end: str,
    limit: int,
    timeout: int = 30,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Optional[str]]:
    """Return (events newest first, query metadata, error)."""
    query = build_query(cluster_field, cluster, namespace, pod)
    # Ask for more than needed: the phrase filter also matches longer Pod names.
    lines, error = _fetch(base_url, query, start, end, max(limit * 4, 50), timeout)
    meta = {"logsql": query, "time_range": {"start": start, "end": end}, "backend_lines": len(lines)}
    if error:
        return [], meta, error
    events = []
    for line in lines:
        event = parse_event(line.get("_msg", ""))
        if not event or event["involved_name"] != pod or event["involved_namespace"] != namespace:
            continue
        event["timestamp"] = line.get("_time")
        events.append(event)
    meta["matched"] = len(events)
    return events[:limit], meta, None
