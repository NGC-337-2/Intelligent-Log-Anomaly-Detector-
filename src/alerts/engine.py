"""
src/alerts/engine.py
~~~~~~~~~~~~~~~~~~~~~
Alert engine — evaluates anomaly results, applies cooldown filtering,
publishes to AWS SNS, writes to CloudWatch custom metrics, and stores
alert records in SQLite for dashboard consumption.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

from src.alerts.cooldown import is_suppressed, record_alert
from src.detector.isolation_forest import AnomalyResult
from src.detector.zscore_baseline import ZScoreResult

load_dotenv()

logger = logging.getLogger(__name__)

SNS_TOPIC_ARN = os.getenv("SNS_TOPIC_ARN", "")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
METRICS_NAMESPACE = os.getenv("CLOUDWATCH_METRICS_NAMESPACE", "LogAnomalyDetector")
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() == "true"
DB_PATH = Path(os.getenv("DB_PATH", "logs.db"))

SEVERITY_PRIORITY = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS anomaly_alerts (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            window_start      TEXT NOT NULL,
            anomaly_score     REAL,
            severity          TEXT,
            top_features      TEXT,    -- JSON array
            feature_values    TEXT,    -- JSON object
            feature_zscores   TEXT,    -- JSON object
            model_name        TEXT,
            sns_published     INTEGER DEFAULT 0,
            created_at        TEXT DEFAULT (datetime('now'))
        )
    """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_window ON anomaly_alerts(window_start)")
    conn.commit()
    return conn


def _build_sns_payload(result: Union[AnomalyResult, ZScoreResult], sample_logs: list[str]) -> dict:
    """Build a human-readable + machine-parseable SNS alert payload."""
    if isinstance(result, AnomalyResult):
        score = result.anomaly_score
        severity = result.severity
        top_features = result.top_features
        feature_values = result.feature_values
        model = result.model_name
        window_start = result.window_start
    else:
        score = None
        severity = "HIGH" if len(result.triggered_features) >= 2 else "MEDIUM"
        top_features = result.triggered_features
        feature_values = result.feature_values
        model = result.model_name
        window_start = result.window_start

    payload = {
        "alert_type": "LOG_ANOMALY",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "window_start": window_start,
        "severity": severity,
        "anomaly_score": score,
        "model": model,
        "top_features": top_features,
        "feature_values": {k: round(float(v), 4) for k, v in feature_values.items()},
        "sample_logs": sample_logs[:5],
        "dashboard_url": "http://localhost:3000/d/log-anomaly/log-anomaly-detector",
    }
    return payload


def _publish_sns(payload: dict) -> bool:
    """Publish alert payload to SNS. Returns True on success."""
    if not SNS_TOPIC_ARN:
        logger.warning("SNS_TOPIC_ARN not configured — skipping SNS publish")
        return False
    if MOCK_MODE:
        logger.info("[MOCK] Would publish SNS: %s", json.dumps(payload, indent=2))
        return True
    try:
        client = boto3.client("sns", region_name=AWS_REGION)
        severity = payload.get("severity", "UNKNOWN")
        subject = f"[{severity}] Log Anomaly Detected — {payload.get('window_start', '')}"
        client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject[:100],  # SNS subject limit
            Message=json.dumps(payload, indent=2),
            MessageAttributes={
                "severity": {
                    "DataType": "String",
                    "StringValue": severity,
                },
                "model": {
                    "DataType": "String",
                    "StringValue": payload.get("model", "unknown"),
                },
            },
        )
        logger.info("SNS alert published: %s | severity=%s", subject, severity)
        return True
    except ClientError as e:
        logger.error("SNS publish failed: %s", e)
        return False


def _put_cloudwatch_metrics(result: Union[AnomalyResult, ZScoreResult]) -> None:
    """Write anomaly metrics to CloudWatch custom namespace."""
    if MOCK_MODE:
        return

    metrics = []

    if isinstance(result, AnomalyResult):
        metrics.append(
            {
                "MetricName": "AnomalyScore",
                "Value": result.anomaly_score,
                "Unit": "None",
                "Dimensions": [{"Name": "Model", "Value": "IsolationForest"}],
            }
        )
        metrics.append(
            {
                "MetricName": "IsAnomaly",
                "Value": 1 if result.is_anomaly else 0,
                "Unit": "Count",
                "Dimensions": [{"Name": "Model", "Value": "IsolationForest"}],
            }
        )

    # Also publish feature values as individual metrics
    feature_values = result.feature_values if hasattr(result, "feature_values") else {}
    metric_map = {
        "error_rate": ("ErrorRate", "Percent"),
        "avg_latency_ms": ("AvgLatency", "Milliseconds"),
        "p95_latency": ("P95Latency", "Milliseconds"),
        "request_volume": ("RequestVolume", "Count"),
        "failed_auth_count": ("FailedAuthCount", "Count"),
    }
    for feat, (metric_name, unit) in metric_map.items():
        val = feature_values.get(feat)
        if val is not None:
            metrics.append(
                {
                    "MetricName": metric_name,
                    "Value": float(val),
                    "Unit": unit,
                    "Dimensions": [{"Name": "Pipeline", "Value": "LogAnomalyDetector"}],
                }
            )

    if not metrics:
        return

    try:
        cw = boto3.client("cloudwatch", region_name=AWS_REGION)
        # CloudWatch accepts max 20 metrics per call
        for i in range(0, len(metrics), 20):
            cw.put_metric_data(
                Namespace=METRICS_NAMESPACE,
                MetricData=metrics[i : i + 20],
            )
        logger.debug("Published %d CloudWatch metrics", len(metrics))
    except ClientError as e:
        logger.warning("CloudWatch metric publish failed: %s", e)


def _store_alert(result: Union[AnomalyResult, ZScoreResult], sns_published: bool) -> None:
    """Persist alert record to SQLite for dashboard queries."""
    conn = _get_conn()
    try:
        if isinstance(result, AnomalyResult):
            score = result.anomaly_score
            severity = result.severity
            top_features = result.top_features
            feature_values = result.feature_values
            feature_zscores = result.feature_zscores
            model = result.model_name
            window_start = result.window_start
        else:
            score = None
            severity = "HIGH" if len(result.triggered_features) >= 2 else "MEDIUM"
            top_features = result.triggered_features
            feature_values = result.feature_values
            feature_zscores = result.feature_zscores
            model = result.model_name
            window_start = result.window_start

        conn.execute(
            """INSERT INTO anomaly_alerts
               (window_start, anomaly_score, severity, top_features,
                feature_values, feature_zscores, model_name, sns_published)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(window_start),
                score,
                severity,
                json.dumps(top_features),
                json.dumps(feature_values),
                json.dumps(feature_zscores),
                model,
                1 if sns_published else 0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _get_sample_logs(window_start: str, limit: int = 5) -> list[str]:
    """Fetch a few raw log messages from the anomalous time window."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            """SELECT message FROM logs
               WHERE timestamp >= ? AND timestamp < datetime(?, '+1 minute')
               AND (status_code >= 400 OR latency_ms > 1000)
               ORDER BY latency_ms DESC
               LIMIT ?""",
            (window_start, window_start, limit),
        ).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []
    finally:
        conn.close()


def process_result(
    result: Union[AnomalyResult, ZScoreResult],
    cooldown_minutes: Optional[int] = None,
    force: bool = False,
) -> bool:
    """
    Main alert engine entry point.

    Evaluates an anomaly result, applies cooldown filtering, publishes to
    SNS and CloudWatch, and stores the record in SQLite.

    Args:
        result:           AnomalyResult or ZScoreResult to evaluate.
        cooldown_minutes: Override the default cooldown window.
        force:            Skip cooldown check (useful for testing).

    Returns:
        True if an alert was dispatched, False if suppressed or not anomalous.
    """
    # Check if this result is even anomalous
    if isinstance(result, AnomalyResult) and not result.is_anomaly:
        _put_cloudwatch_metrics(result)  # always push metrics
        return False
    if isinstance(result, ZScoreResult) and not result.is_anomaly:
        return False

    top_features = (
        result.top_features if isinstance(result, AnomalyResult) else result.triggered_features
    )

    # Cooldown check
    if not force:
        kwargs = {}
        if cooldown_minutes is not None:
            kwargs["cooldown_minutes"] = cooldown_minutes
        if is_suppressed(top_features, **kwargs):
            logger.info("Alert suppressed by cooldown | features=%s", top_features)
            return False

    # Fetch sample logs for the alert payload
    ws = result.window_start if hasattr(result, "window_start") else ""
    sample_logs = _get_sample_logs(str(ws))

    # Build and publish SNS alert
    payload = _build_sns_payload(result, sample_logs)
    sns_ok = _publish_sns(payload)

    # Push CloudWatch metrics
    _put_cloudwatch_metrics(result)

    # Store in SQLite
    _store_alert(result, sns_published=sns_ok)

    # Record in cooldown tracker
    record_alert(top_features)

    severity = payload.get("severity", "?")
    logger.warning(
        "🚨 ALERT dispatched | severity=%s | features=%s | score=%s",
        severity,
        top_features,
        payload.get("anomaly_score"),
    )
    return True


def get_alert_history(limit: int = 100) -> list[dict]:
    """
    Retrieve recent alerts from SQLite for dashboard display.

    Returns:
        List of alert dicts sorted by created_at descending.
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT window_start, anomaly_score, severity, top_features,
                      feature_values, model_name, sns_published, created_at
               FROM anomaly_alerts
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["top_features"] = json.loads(d["top_features"] or "[]")
            d["feature_values"] = json.loads(d["feature_values"] or "{}")
            results.append(d)
        return results
    finally:
        conn.close()
