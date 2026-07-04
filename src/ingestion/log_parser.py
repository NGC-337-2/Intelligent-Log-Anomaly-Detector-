"""
src/ingestion/log_parser.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Parses raw log lines (JSON or Apache-style) into structured dicts
ready for the SQLite store and feature extractor.

Supports two formats:
  1. JSON  — produced by simulate_logs.py
  2. Plain — Apache/nginx combined log format (fallback regex)
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Regex: Apache/nginx combined log format ──────────────────────────────────
# Example: 127.0.0.1 - frank [10/Oct/2000:13:55:36 -0700] "GET /index.html HTTP/1.1" 200 2326 45
_APACHE_RE = re.compile(
    r"(?P<ip>[\d.]+)\s+"  # client IP
    r"\S+\s+\S+\s+"  # ident, auth
    r"\[(?P<dt>[^\]]+)\]\s+"  # timestamp
    r'"(?P<method>\w+)\s+'  # HTTP method
    r"(?P<endpoint>\S+)\s+"  # path
    r'HTTP/\S+"\s+'  # protocol
    r"(?P<status>\d{3})\s+"  # status code
    r"(?P<bytes>\d+|-)"  # bytes
    r"(?:\s+(?P<latency>\d+))?"  # optional latency (ms)
)

_APACHE_DT_FMT = "%d/%b/%Y:%H:%M:%S %z"

# ─── Regex: simple structured log (our simulator's "message" field) ───────────
# Example: "GET /api/users 200 143ms"
_SIMPLE_RE = re.compile(
    r"(?P<method>GET|POST|PUT|DELETE|PATCH)\s+"
    r"(?P<endpoint>/\S*)\s+"
    r"(?P<status>\d{3})\s+"
    r"(?P<latency>\d+)ms"
)

_STATUS_TO_LEVEL = {
    range(200, 300): "INFO",
    range(300, 400): "INFO",
    range(400, 500): "WARN",
    range(500, 600): "ERROR",
}


def _status_level(code: int) -> str:
    for r, lvl in _STATUS_TO_LEVEL.items():
        if code in r:
            return lvl
    return "INFO"


def _parse_json(raw: str, event_id: Optional[str] = None) -> Optional[dict]:
    """Parse a JSON-formatted log line (produced by simulate_logs.py)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # Normalise timestamp to ISO format with UTC
    ts_raw = data.get("timestamp", "")
    try:
        if ts_raw:
            dt = datetime.fromisoformat(ts_raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.isoformat()
        else:
            ts = datetime.now(timezone.utc).isoformat()
    except ValueError:
        ts = datetime.now(timezone.utc).isoformat()

    status_code = int(data.get("status_code", 0))

    return {
        "event_id": event_id or data.get("request_id"),
        "timestamp": ts,
        "level": data.get("level") or _status_level(status_code),
        "method": data.get("method", "GET"),
        "endpoint": data.get("endpoint", "/"),
        "status_code": status_code,
        "latency_ms": int(data.get("latency_ms", 0)),
        "request_id": data.get("request_id"),
        "message": data.get("message", raw[:200]),
    }


def _parse_apache(raw: str, event_id: Optional[str] = None) -> Optional[dict]:
    """Parse an Apache/nginx combined log line."""
    m = _APACHE_RE.search(raw)
    if not m:
        return None

    try:
        dt = datetime.strptime(m.group("dt"), _APACHE_DT_FMT)
    except ValueError:
        dt = datetime.now(timezone.utc)

    status_code = int(m.group("status"))
    latency_str = m.group("latency")
    latency_ms = int(latency_str) if latency_str else 0

    return {
        "event_id": event_id,
        "timestamp": dt.isoformat(),
        "level": _status_level(status_code),
        "method": m.group("method"),
        "endpoint": m.group("endpoint"),
        "status_code": status_code,
        "latency_ms": latency_ms,
        "request_id": None,
        "message": raw[:200],
    }


def _parse_simple(raw: str, event_id: Optional[str] = None) -> Optional[dict]:
    """Parse a minimal 'METHOD /path STATUS LATENCYms' log line."""
    m = _SIMPLE_RE.search(raw)
    if not m:
        return None

    status_code = int(m.group("status"))
    return {
        "event_id": event_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": _status_level(status_code),
        "method": m.group("method"),
        "endpoint": m.group("endpoint"),
        "status_code": status_code,
        "latency_ms": int(m.group("latency")),
        "request_id": None,
        "message": raw[:200],
    }


def parse_log_line(raw: str, event_id: Optional[str] = None) -> Optional[dict]:
    """
    Attempt to parse a raw log line into a structured dict.

    Tries parsers in order: JSON → Apache → Simple.
    Returns None if no parser matches.

    Args:
        raw:       Raw log line string.
        event_id:  CloudWatch eventId for deduplication.

    Returns:
        Structured dict with keys: event_id, timestamp, level, method,
        endpoint, status_code, latency_ms, request_id, message.
        Or None if unparseable.
    """
    if not raw or not raw.strip():
        return None

    stripped = raw.strip()

    result = (
        _parse_json(stripped, event_id)
        or _parse_apache(stripped, event_id)
        or _parse_simple(stripped, event_id)
    )

    if result is None:
        logger.debug("Unparseable log line: %r", stripped[:100])

    return result
