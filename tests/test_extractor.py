"""
tests/test_extractor.py
~~~~~~~~~~~~~~~~~~~~~~~~
Unit tests for src/features/extractor.py
"""

import sqlite3
import tempfile
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

# Patch DB_PATH before importing extractor
os.environ["DB_PATH"] = ":memory:"

from src.features.extractor import (
    _compute_window_features,
    extract_features,
    FEATURE_COLUMNS,
)


class TestComputeWindowFeatures:
    def _make_df(self, rows):
        df = pd.DataFrame(rows, columns=["timestamp", "status_code", "latency_ms", "endpoint"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df["latency_ms"] = pd.to_numeric(df["latency_ms"])
        df["status_code"] = pd.to_numeric(df["status_code"]).astype(int)
        return df

    def test_all_zeros_on_empty_df(self):
        feats = _compute_window_features(pd.DataFrame(
            columns=["timestamp", "status_code", "latency_ms", "endpoint"]
        ))
        assert feats["error_rate"] == 0.0
        assert feats["request_volume"] == 0
        assert feats["failed_auth_count"] == 0

    def test_error_rate_calculation(self):
        rows = [
            ["2024-01-01T00:00:00Z", 200, 100, "/api/a"],
            ["2024-01-01T00:00:01Z", 200, 120, "/api/b"],
            ["2024-01-01T00:00:02Z", 500, 800, "/api/c"],
            ["2024-01-01T00:00:03Z", 404, 50,  "/api/d"],
        ]
        feats = _compute_window_features(self._make_df(rows))
        assert feats["error_rate"] == pytest.approx(0.5, abs=0.001)   # 2/4

    def test_avg_latency(self):
        rows = [
            ["2024-01-01T00:00:00Z", 200, 100, "/a"],
            ["2024-01-01T00:00:01Z", 200, 200, "/b"],
            ["2024-01-01T00:00:02Z", 200, 300, "/c"],
        ]
        feats = _compute_window_features(self._make_df(rows))
        assert feats["avg_latency_ms"] == pytest.approx(200.0, abs=0.1)

    def test_p95_latency(self):
        rows = [
            ["2024-01-01T00:00:00Z", 200, lat, f"/ep{i}"]
            for i, lat in enumerate(range(1, 101))
        ]
        feats = _compute_window_features(self._make_df(rows))
        assert feats["p95_latency"] >= 95  # 95th percentile of 1..100

    def test_unique_endpoints(self):
        rows = [
            ["2024-01-01T00:00:00Z", 200, 100, "/api/a"],
            ["2024-01-01T00:00:01Z", 200, 100, "/api/a"],
            ["2024-01-01T00:00:02Z", 200, 100, "/api/b"],
            ["2024-01-01T00:00:03Z", 200, 100, "/api/c"],
        ]
        feats = _compute_window_features(self._make_df(rows))
        assert feats["unique_endpoints"] == 3

    def test_failed_auth_count(self):
        rows = [
            ["2024-01-01T00:00:00Z", 401, 50, "/api/auth/login"],
            ["2024-01-01T00:00:01Z", 401, 50, "/api/auth/login"],
            ["2024-01-01T00:00:02Z", 200, 100, "/api/users"],
        ]
        feats = _compute_window_features(self._make_df(rows))
        assert feats["failed_auth_count"] == 2

    def test_request_volume(self):
        rows = [
            ["2024-01-01T00:00:00Z", 200, 100, f"/ep{i}"]
            for i in range(42)
        ]
        feats = _compute_window_features(self._make_df(rows))
        assert feats["request_volume"] == 42

    def test_all_feature_columns_present(self):
        rows = [["2024-01-01T00:00:00Z", 200, 100, "/a"]]
        feats = _compute_window_features(self._make_df(rows))
        for col in FEATURE_COLUMNS:
            assert col in feats, f"Missing feature: {col}"

    def test_all_500s_gives_full_error_rate(self):
        rows = [
            ["2024-01-01T00:00:00Z", 500, 1000, "/api/pay"],
            ["2024-01-01T00:00:01Z", 503, 1200, "/api/pay"],
        ]
        feats = _compute_window_features(self._make_df(rows))
        assert feats["error_rate"] == pytest.approx(1.0, abs=0.001)
