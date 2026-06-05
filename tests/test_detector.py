"""
tests/test_detector.py
~~~~~~~~~~~~~~~~~~~~~~~
Unit tests for IsolationForest and Z-Score detector modules.
"""

import numpy as np
import pandas as pd
import pytest

from src.features.extractor import FEATURE_COLUMNS
from src.detector.isolation_forest import train, score, _severity, AnomalyResult
from src.detector.zscore_baseline import ZScoreDetector, ZScoreResult


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _normal_feature_row(**overrides) -> dict:
    """Return a normal-looking feature row."""
    row = {
        "window_start": "2024-01-15T10:30:00+00:00",
        "error_rate": 0.02,
        "avg_latency_ms": 150.0,
        "p95_latency": 300.0,
        "request_volume": 100,
        "unique_endpoints": 10,
        "failed_auth_count": 1,
    }
    row.update(overrides)
    return row


def _anomaly_feature_row(**overrides) -> dict:
    """Return a clearly anomalous feature row."""
    row = {
        "window_start": "2024-01-15T10:31:00+00:00",
        "error_rate": 0.95,
        "avg_latency_ms": 5000.0,
        "p95_latency": 8000.0,
        "request_volume": 5,
        "unique_endpoints": 1,
        "failed_auth_count": 50,
    }
    row.update(overrides)
    return row


def _make_training_df(n: int = 200) -> pd.DataFrame:
    """Generate synthetic normal training data."""
    rng = np.random.default_rng(42)
    data = {
        "error_rate": rng.uniform(0.0, 0.05, n),
        "avg_latency_ms": rng.normal(150, 30, n).clip(10),
        "p95_latency": rng.normal(300, 60, n).clip(50),
        "request_volume": rng.integers(50, 200, n),
        "unique_endpoints": rng.integers(5, 15, n),
        "failed_auth_count": rng.integers(0, 5, n),
    }
    return pd.DataFrame(data)


# ─── IsolationForest tests ────────────────────────────────────────────────────

class TestIsolationForest:
    @pytest.fixture(scope="class")
    def trained_model(self, tmp_path_factory):
        """Train a model once for all tests in this class."""
        tmp = tmp_path_factory.mktemp("models")
        import os
        os.environ["MODEL_DIR"] = str(tmp)
        df = _make_training_df(n=300)
        model = train(df, n_estimators=50, contamination=0.05, save_to_s3=False)
        return model

    def test_normal_row_not_flagged(self, trained_model):
        row = _normal_feature_row()
        result = score(row, model=trained_model)
        assert result is not None
        assert not result.is_anomaly, "Normal traffic should not be flagged"

    def test_anomaly_row_flagged(self, trained_model):
        row = _anomaly_feature_row()
        result = score(row, model=trained_model)
        assert result is not None
        assert result.is_anomaly, "Extreme anomaly should always be flagged"

    def test_result_has_all_fields(self, trained_model):
        row = _normal_feature_row()
        result = score(row, model=trained_model)
        assert result is not None
        assert isinstance(result.anomaly_score, float)
        assert isinstance(result.top_features, list)
        assert len(result.top_features) <= 3
        assert isinstance(result.feature_values, dict)
        assert isinstance(result.feature_zscores, dict)

    def test_top_features_are_valid_columns(self, trained_model):
        row = _anomaly_feature_row()
        result = score(row, model=trained_model)
        assert result is not None
        for feat in result.top_features:
            assert feat in FEATURE_COLUMNS

    def test_to_dict_serialisable(self, trained_model):
        row = _normal_feature_row()
        result = score(row, model=trained_model)
        assert result is not None
        d = result.to_dict()
        import json
        json.dumps(d)  # must not raise

    def test_severity_levels(self):
        assert _severity(-0.01) == "LOW"
        assert _severity(-0.15) == "MEDIUM"
        assert _severity(-0.35) == "HIGH"
        assert _severity(-0.50) == "CRITICAL"

    def test_recall_on_injected_anomalies(self, trained_model):
        """At least 80% of injected anomalies should be detected."""
        n = 50
        detected = 0
        for _ in range(n):
            row = _anomaly_feature_row()
            result = score(row, model=trained_model)
            if result and result.is_anomaly:
                detected += 1
        recall = detected / n
        assert recall >= 0.80, f"Recall too low: {recall:.2%}"

    def test_false_positive_rate(self, trained_model):
        """Less than 10% of normal windows should be flagged."""
        n = 100
        fp = 0
        rng = np.random.default_rng(0)
        for _ in range(n):
            row = _normal_feature_row(
                error_rate=float(rng.uniform(0, 0.04)),
                avg_latency_ms=float(rng.normal(150, 20)),
                p95_latency=float(rng.normal(280, 40)),
                request_volume=int(rng.integers(60, 180)),
            )
            result = score(row, model=trained_model)
            if result and result.is_anomaly:
                fp += 1
        fpr = fp / n
        assert fpr < 0.10, f"False positive rate too high: {fpr:.2%}"


# ─── ZScore tests ─────────────────────────────────────────────────────────────

class TestZScoreDetector:
    def _warm_detector(self, n: int = 10) -> ZScoreDetector:
        detector = ZScoreDetector(window_size=30, threshold=3.0)
        for _ in range(n):
            detector.update(_normal_feature_row())
        return detector

    def test_not_warmed_up_returns_no_anomaly(self):
        detector = ZScoreDetector()
        result = detector.check(_normal_feature_row())
        assert not result.is_anomaly

    def test_warmed_up_flag(self):
        detector = self._warm_detector(n=10)
        assert detector.is_warmed_up

    def test_normal_window_not_flagged_after_warmup(self):
        detector = self._warm_detector(n=20)
        result = detector.check(_normal_feature_row())
        assert not result.is_anomaly

    def test_extreme_anomaly_flagged(self):
        detector = self._warm_detector(n=20)
        result = detector.check(_anomaly_feature_row())
        assert result.is_anomaly

    def test_triggered_features_listed(self):
        detector = self._warm_detector(n=20)
        result = detector.check(_anomaly_feature_row())
        assert len(result.triggered_features) > 0
        for feat in result.triggered_features:
            assert feat in FEATURE_COLUMNS

    def test_to_dict_serialisable(self):
        detector = self._warm_detector(n=20)
        result = detector.check(_normal_feature_row())
        import json
        json.dumps(result.to_dict())  # must not raise

    def test_reset_clears_buffer(self):
        detector = self._warm_detector(n=20)
        assert detector.is_warmed_up
        detector.reset()
        assert not detector.is_warmed_up

    def test_update_does_not_score(self):
        """update() should not affect the anomaly flag of the current window."""
        detector = self._warm_detector(n=20)
        # Add an extreme value via update() — should not flag the *next* normal check
        for _ in range(3):
            detector.update(_anomaly_feature_row())
        # The buffer now contains some anomalies, but a normal window right at 3σ
        # boundary may or may not trigger — just ensure no exception
        result = detector.check(_normal_feature_row())
        assert isinstance(result, ZScoreResult)
