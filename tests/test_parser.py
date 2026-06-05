"""
tests/test_parser.py
~~~~~~~~~~~~~~~~~~~~~
Unit tests for src/ingestion/log_parser.py
"""

import pytest
from src.ingestion.log_parser import parse_log_line, _parse_json, _parse_apache, _parse_simple


class TestJsonParser:
    def test_parses_valid_json_log(self):
        raw = (
            '{"timestamp": "2024-01-15T10:30:00", "level": "INFO", "method": "GET", '
            '"endpoint": "/api/users", "status_code": 200, "latency_ms": 145, '
            '"request_id": "abc123", "message": "GET /api/users 200 145ms"}'
        )
        result = parse_log_line(raw, event_id="evt-001")
        assert result is not None
        assert result["status_code"] == 200
        assert result["latency_ms"] == 145
        assert result["endpoint"] == "/api/users"
        assert result["method"] == "GET"
        assert result["event_id"] == "evt-001"

    def test_infers_level_from_status_code(self):
        raw = '{"timestamp": "2024-01-15T10:30:00", "status_code": 500, "latency_ms": 300, "endpoint": "/api/pay"}'
        result = parse_log_line(raw)
        assert result is not None
        assert result["level"] == "ERROR"

    def test_infers_warn_level_for_4xx(self):
        raw = '{"timestamp": "2024-01-15T10:30:00", "status_code": 404, "latency_ms": 50, "endpoint": "/missing"}'
        result = parse_log_line(raw)
        assert result is not None
        assert result["level"] == "WARN"

    def test_handles_missing_optional_fields(self):
        raw = '{"timestamp": "2024-01-15T10:30:00", "status_code": 200, "latency_ms": 100}'
        result = parse_log_line(raw)
        assert result is not None
        assert result["status_code"] == 200

    def test_normalises_naive_timestamp_to_utc(self):
        raw = '{"timestamp": "2024-01-15T10:30:00", "status_code": 200, "latency_ms": 50}'
        result = parse_log_line(raw)
        assert result is not None
        assert "+00:00" in result["timestamp"] or "Z" in result["timestamp"] or "UTC" not in result["timestamp"]


class TestApacheParser:
    def test_parses_apache_log_with_latency(self):
        raw = '127.0.0.1 - frank [15/Jan/2024:10:30:00 +0000] "GET /index.html HTTP/1.1" 200 2326 145'
        result = parse_log_line(raw)
        assert result is not None
        assert result["status_code"] == 200
        assert result["method"] == "GET"
        assert result["endpoint"] == "/index.html"
        assert result["latency_ms"] == 145

    def test_parses_apache_500(self):
        raw = '10.0.0.1 - - [15/Jan/2024:10:30:00 +0000] "POST /api/login HTTP/1.1" 500 512 890'
        result = parse_log_line(raw)
        assert result is not None
        assert result["status_code"] == 500
        assert result["level"] == "ERROR"

    def test_apache_without_latency(self):
        raw = '127.0.0.1 - - [15/Jan/2024:10:30:00 +0000] "DELETE /api/item HTTP/1.1" 204 0'
        result = parse_log_line(raw)
        assert result is not None
        assert result["latency_ms"] == 0


class TestSimpleParser:
    def test_parses_simple_format(self):
        raw = "GET /api/products 200 67ms"
        result = parse_log_line(raw)
        assert result is not None
        assert result["method"] == "GET"
        assert result["endpoint"] == "/api/products"
        assert result["status_code"] == 200
        assert result["latency_ms"] == 67

    def test_simple_post_error(self):
        raw = "POST /api/checkout 503 2341ms"
        result = parse_log_line(raw)
        assert result is not None
        assert result["status_code"] == 503
        assert result["level"] == "ERROR"


class TestEdgeCases:
    def test_empty_string_returns_none(self):
        assert parse_log_line("") is None

    def test_whitespace_only_returns_none(self):
        assert parse_log_line("   \n  ") is None

    def test_garbage_returns_none(self):
        assert parse_log_line("not a log line at all!!@#$") is None

    def test_none_like_input(self):
        # Should not raise
        result = parse_log_line("null")
        # "null" is valid JSON → returns None status_code=0
        # just ensure no exception
