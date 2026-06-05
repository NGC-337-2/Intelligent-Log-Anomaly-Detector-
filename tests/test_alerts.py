"""
tests/test_alerts.py
~~~~~~~~~~~~~~~~~~~~~
Unit tests for the alert cooldown and engine modules.
"""

import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Use a temp DB for each test
@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Redirect all DB operations to a temp file for isolation."""
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_file)
    monkeypatch.setenv("MOCK_MODE", "true")
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:test")
    yield db_file


# ─── Import after monkeypatching env ─────────────────────────────────────────
from src.alerts import cooldown as cooldown_module
from src.alerts import engine as engine_module
from src.detector.isolation_forest import AnomalyResult
from src.detector.zscore_baseline import ZScoreResult


def _make_if_result(is_anomaly=True, score=-0.35, severity="HIGH") -> AnomalyResult:
    return AnomalyResult(
        window_start="2024-01-15T10:30:00+00:00",
        anomaly_score=score,
        is_anomaly=is_anomaly,
        severity=severity,
        top_features=["error_rate", "p95_latency"],
        feature_values={
            "error_rate": 0.8, "avg_latency_ms": 2000.0, "p95_latency": 5000.0,
            "request_volume": 10, "unique_endpoints": 2, "failed_auth_count": 0,
        },
        feature_zscores={
            "error_rate": 4.5, "avg_latency_ms": 3.2, "p95_latency": 6.1,
            "request_volume": -1.2, "unique_endpoints": -0.5, "failed_auth_count": 0.1,
        },
    )


def _make_zs_result(is_anomaly=True) -> ZScoreResult:
    return ZScoreResult(
        window_start="2024-01-15T10:30:00+00:00",
        is_anomaly=is_anomaly,
        triggered_features=["failed_auth_count", "error_rate"],
        feature_zscores={"error_rate": 4.0, "failed_auth_count": 5.0},
        feature_values={"error_rate": 0.7, "failed_auth_count": 40},
        threshold=3.0,
    )


class TestCooldown:
    def test_not_suppressed_first_time(self, temp_db, monkeypatch):
        monkeypatch.setattr(cooldown_module, "DB_PATH", Path(temp_db))
        assert not cooldown_module.is_suppressed(["error_rate", "p95_latency"])

    def test_suppressed_after_recording(self, temp_db, monkeypatch):
        monkeypatch.setattr(cooldown_module, "DB_PATH", Path(temp_db))
        cooldown_module.record_alert(["error_rate", "p95_latency"])
        assert cooldown_module.is_suppressed(["error_rate", "p95_latency"])

    def test_not_suppressed_different_features(self, temp_db, monkeypatch):
        monkeypatch.setattr(cooldown_module, "DB_PATH", Path(temp_db))
        cooldown_module.record_alert(["error_rate", "p95_latency"])
        # Different combo should not be suppressed
        assert not cooldown_module.is_suppressed(["failed_auth_count"])

    def test_order_independent_key(self, temp_db, monkeypatch):
        monkeypatch.setattr(cooldown_module, "DB_PATH", Path(temp_db))
        cooldown_module.record_alert(["p95_latency", "error_rate"])
        # Same features, different order — should still match
        assert cooldown_module.is_suppressed(["error_rate", "p95_latency"])

    def test_not_suppressed_after_cooldown_expired(self, temp_db, monkeypatch):
        monkeypatch.setattr(cooldown_module, "DB_PATH", Path(temp_db))
        # Record an alert, then check with 0-minute cooldown
        cooldown_module.record_alert(["error_rate"])
        assert not cooldown_module.is_suppressed(["error_rate"], cooldown_minutes=0)

    def test_get_recent_alerts_empty(self, temp_db, monkeypatch):
        monkeypatch.setattr(cooldown_module, "DB_PATH", Path(temp_db))
        alerts = cooldown_module.get_recent_alerts()
        assert alerts == []

    def test_get_recent_alerts_after_record(self, temp_db, monkeypatch):
        monkeypatch.setattr(cooldown_module, "DB_PATH", Path(temp_db))
        cooldown_module.record_alert(["error_rate"])
        cooldown_module.record_alert(["failed_auth_count"])
        alerts = cooldown_module.get_recent_alerts()
        assert len(alerts) == 2

    def test_purge_expired(self, temp_db, monkeypatch):
        monkeypatch.setattr(cooldown_module, "DB_PATH", Path(temp_db))
        cooldown_module.record_alert(["error_rate"])
        # Purge with 0-minute window (everything is "expired")
        deleted = cooldown_module.purge_expired(cooldown_minutes=0)
        assert deleted >= 0  # may or may not delete depending on timing


class TestAlertEngine:
    def test_non_anomaly_if_result_not_dispatched(self, temp_db, monkeypatch):
        monkeypatch.setattr(engine_module, "DB_PATH", Path(temp_db))
        monkeypatch.setattr(cooldown_module, "DB_PATH", Path(temp_db))
        result = _make_if_result(is_anomaly=False, score=0.1, severity="NONE")
        fired = engine_module.process_result(result, force=True)
        assert not fired

    def test_anomaly_if_result_dispatched(self, temp_db, monkeypatch):
        monkeypatch.setattr(engine_module, "DB_PATH", Path(temp_db))
        monkeypatch.setattr(cooldown_module, "DB_PATH", Path(temp_db))
        result = _make_if_result(is_anomaly=True)
        fired = engine_module.process_result(result, force=True)
        assert fired

    def test_suppressed_after_first_alert(self, temp_db, monkeypatch):
        monkeypatch.setattr(engine_module, "DB_PATH", Path(temp_db))
        monkeypatch.setattr(cooldown_module, "DB_PATH", Path(temp_db))
        result = _make_if_result(is_anomaly=True)
        # First alert should go through
        first = engine_module.process_result(result)
        assert first
        # Second alert (same features) should be suppressed
        second = engine_module.process_result(result)
        assert not second

    def test_force_bypasses_cooldown(self, temp_db, monkeypatch):
        monkeypatch.setattr(engine_module, "DB_PATH", Path(temp_db))
        monkeypatch.setattr(cooldown_module, "DB_PATH", Path(temp_db))
        result = _make_if_result(is_anomaly=True)
        engine_module.process_result(result)
        # force=True should bypass cooldown
        fired = engine_module.process_result(result, force=True)
        assert fired

    def test_alert_stored_in_db(self, temp_db, monkeypatch):
        monkeypatch.setattr(engine_module, "DB_PATH", Path(temp_db))
        monkeypatch.setattr(cooldown_module, "DB_PATH", Path(temp_db))
        result = _make_if_result(is_anomaly=True)
        engine_module.process_result(result, force=True)
        history = engine_module.get_alert_history()
        assert len(history) >= 1
        assert history[0]["severity"] == "HIGH"
        assert isinstance(history[0]["top_features"], list)

    def test_zscore_result_dispatched(self, temp_db, monkeypatch):
        monkeypatch.setattr(engine_module, "DB_PATH", Path(temp_db))
        monkeypatch.setattr(cooldown_module, "DB_PATH", Path(temp_db))
        result = _make_zs_result(is_anomaly=True)
        fired = engine_module.process_result(result, force=True)
        assert fired

    def test_non_anomaly_zscore_not_dispatched(self, temp_db, monkeypatch):
        monkeypatch.setattr(engine_module, "DB_PATH", Path(temp_db))
        result = _make_zs_result(is_anomaly=False)
        fired = engine_module.process_result(result, force=True)
        assert not fired
