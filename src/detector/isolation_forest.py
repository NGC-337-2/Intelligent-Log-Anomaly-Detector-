"""
src/detector/isolation_forest.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Isolation Forest anomaly detection model.

Responsibilities:
  - train()  : fit a model on a feature DataFrame, serialize to disk + S3
  - score()  : load model, score a feature row, return AnomalyResult
  - explain(): identify which features contributed most to the anomaly
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import boto3
import joblib
import numpy as np
import pandas as pd
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from sklearn.ensemble import IsolationForest

from src.features.extractor import FEATURE_COLUMNS

load_dotenv()

logger = logging.getLogger(__name__)

MODEL_DIR = Path(os.getenv("MODEL_DIR", "models"))
MODEL_FILENAME = "isolation_forest.pkl"
MODEL_PATH = MODEL_DIR / MODEL_FILENAME

S3_BUCKET = os.getenv("S3_BUCKET", "log-anomaly-detector-models")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() == "true"

ANOMALY_THRESHOLD = float(os.getenv("ANOMALY_THRESHOLD", "-0.1"))
N_ESTIMATORS = int(os.getenv("IF_N_ESTIMATORS", "100"))
CONTAMINATION = float(os.getenv("IF_CONTAMINATION", "0.05"))
RANDOM_STATE = 42


@dataclass
class AnomalyResult:
    """Result of scoring a single feature window."""

    window_start: str
    anomaly_score: float          # decision_function output; lower = more anomalous
    is_anomaly: bool
    severity: str                 # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
    top_features: list[str]       # top contributing features by z-score
    feature_values: dict          # raw feature values for the window
    feature_zscores: dict         # per-feature z-scores against training stats
    model_name: str = "IsolationForest"

    def to_dict(self) -> dict:
        return {
            "window_start": self.window_start,
            "anomaly_score": round(self.anomaly_score, 4),
            "is_anomaly": self.is_anomaly,
            "severity": self.severity,
            "top_features": self.top_features,
            "feature_values": {k: round(v, 4) for k, v in self.feature_values.items()},
            "feature_zscores": {k: round(v, 4) for k, v in self.feature_zscores.items()},
            "model_name": self.model_name,
        }


def _severity(score: float) -> str:
    if score >= -0.05:
        return "LOW"
    elif score >= -0.2:
        return "MEDIUM"
    elif score >= -0.4:
        return "HIGH"
    else:
        return "CRITICAL"


def train(
    features_df: pd.DataFrame,
    n_estimators: int = N_ESTIMATORS,
    contamination: float = CONTAMINATION,
    save_to_s3: bool = True,
) -> IsolationForest:
    """
    Train an Isolation Forest on the provided feature DataFrame.

    Args:
        features_df:   DataFrame with at least FEATURE_COLUMNS present.
        n_estimators:  Number of isolation trees.
        contamination: Expected fraction of anomalies in training data.
        save_to_s3:    Whether to upload the serialized model to S3.

    Returns:
        Fitted IsolationForest model.
    """
    logger.info(
        "Training IsolationForest | n_estimators=%d | contamination=%.3f | rows=%d",
        n_estimators, contamination, len(features_df),
    )

    X = features_df[FEATURE_COLUMNS].values

    model = IsolationForest(
        n_estimators=n_estimators,
        contamination=contamination,
        random_state=RANDOM_STATE,
        n_jobs=-1,
    )
    model.fit(X)

    # Compute and attach training statistics for explainability
    model.training_mean_ = np.mean(X, axis=0)
    model.training_std_ = np.std(X, axis=0) + 1e-9  # avoid division by zero

    # Save locally
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    logger.info("Model saved to %s", MODEL_PATH)

    # Save metadata sidecar
    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_estimators": n_estimators,
        "contamination": contamination,
        "training_rows": len(features_df),
        "features": FEATURE_COLUMNS,
        "training_mean": model.training_mean_.tolist(),
        "training_std": model.training_std_.tolist(),
    }
    meta_path = MODEL_DIR / "isolation_forest_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2))

    # Upload to S3
    if save_to_s3 and not MOCK_MODE:
        _upload_to_s3(MODEL_PATH, MODEL_FILENAME)
        _upload_to_s3(meta_path, "isolation_forest_meta.json")

    return model


def _upload_to_s3(local_path: Path, s3_key: str) -> None:
    try:
        s3 = boto3.client("s3", region_name=AWS_REGION)
        s3.upload_file(str(local_path), S3_BUCKET, f"models/{s3_key}")
        logger.info("Uploaded %s → s3://%s/models/%s", local_path.name, S3_BUCKET, s3_key)
    except ClientError as e:
        logger.warning("S3 upload failed: %s", e)


def _download_from_s3() -> bool:
    """Try to download model from S3. Returns True on success."""
    if MOCK_MODE:
        return False
    try:
        s3 = boto3.client("s3", region_name=AWS_REGION)
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        s3.download_file(S3_BUCKET, f"models/{MODEL_FILENAME}", str(MODEL_PATH))
        logger.info("Downloaded model from S3")
        return True
    except ClientError:
        return False


def load_model() -> Optional[IsolationForest]:
    """
    Load the trained model from local disk, falling back to S3.

    Returns:
        Fitted IsolationForest, or None if no model is found.
    """
    if MODEL_PATH.exists():
        model = joblib.load(MODEL_PATH)
        logger.info("Loaded model from %s", MODEL_PATH)
        return model

    logger.info("Model not found locally; attempting S3 download...")
    if _download_from_s3() and MODEL_PATH.exists():
        return joblib.load(MODEL_PATH)

    logger.warning("No trained model available. Run train.py first.")
    return None


def score(
    feature_row: dict,
    model: Optional[IsolationForest] = None,
) -> Optional[AnomalyResult]:
    """
    Score a single feature window.

    Args:
        feature_row: Dict with keys matching FEATURE_COLUMNS + 'window_start'.
        model:       Pre-loaded model (will load from disk if None).

    Returns:
        AnomalyResult or None if model unavailable.
    """
    if model is None:
        model = load_model()
    if model is None:
        return None

    X = np.array([[feature_row[col] for col in FEATURE_COLUMNS]])
    raw_score = float(model.decision_function(X)[0])
    is_anomaly = raw_score < ANOMALY_THRESHOLD

    # Per-feature z-scores for explainability
    feature_values = {col: feature_row[col] for col in FEATURE_COLUMNS}
    if hasattr(model, "training_mean_") and hasattr(model, "training_std_"):
        zscores = {
            col: float((feature_row[col] - model.training_mean_[i]) / model.training_std_[i])
            for i, col in enumerate(FEATURE_COLUMNS)
        }
    else:
        zscores = {col: 0.0 for col in FEATURE_COLUMNS}

    # Top contributing features (sorted by absolute z-score)
    top_features = sorted(
        FEATURE_COLUMNS,
        key=lambda c: abs(zscores.get(c, 0)),
        reverse=True,
    )[:3]

    ws = feature_row.get("window_start", "")
    if hasattr(ws, "isoformat"):
        ws = ws.isoformat()

    return AnomalyResult(
        window_start=str(ws),
        anomaly_score=raw_score,
        is_anomaly=is_anomaly,
        severity=_severity(raw_score) if is_anomaly else "NONE",
        top_features=top_features,
        feature_values=feature_values,
        feature_zscores=zscores,
    )


def batch_score(
    features_df: pd.DataFrame,
    model: Optional[IsolationForest] = None,
) -> list[AnomalyResult]:
    """
    Score an entire DataFrame of feature windows.

    Returns:
        List of AnomalyResult, one per row.
    """
    if model is None:
        model = load_model()
    if model is None:
        return []

    results = []
    for _, row in features_df.iterrows():
        result = score(row.to_dict(), model=model)
        if result:
            results.append(result)
    return results
