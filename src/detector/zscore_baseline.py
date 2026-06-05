"""
src/detector/zscore_baseline.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Rolling Z-Score anomaly baseline detector.

Maintains a rolling window of per-feature statistics and flags any
window where a feature exceeds N standard deviations from the mean.

Complements the Isolation Forest as a lightweight, explainable fallback.
"""

import logging
import os
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np
from dotenv import load_dotenv

from src.features.extractor import FEATURE_COLUMNS

load_dotenv()

logger = logging.getLogger(__name__)

ZSCORE_THRESHOLD = float(os.getenv("ZSCORE_THRESHOLD", "3.0"))
ROLLING_WINDOW = int(os.getenv("ZSCORE_ROLLING_WINDOW", "30"))  # windows (= 30 minutes)


@dataclass
class ZScoreResult:
    """Result of Z-Score anomaly check on a single feature window."""

    window_start: str
    is_anomaly: bool
    triggered_features: list[str]       # features that breached threshold
    feature_zscores: dict               # z-score per feature
    feature_values: dict                # raw values
    threshold: float
    model_name: str = "ZScoreBaseline"

    def to_dict(self) -> dict:
        return {
            "window_start": self.window_start,
            "is_anomaly": self.is_anomaly,
            "triggered_features": self.triggered_features,
            "feature_zscores": {k: round(v, 4) for k, v in self.feature_zscores.items()},
            "feature_values": {k: round(v, 4) for k, v in self.feature_values.items()},
            "threshold": self.threshold,
            "model_name": self.model_name,
        }


class ZScoreDetector:
    """
    Stateful rolling Z-Score detector.

    Maintains a circular buffer of recent feature vectors to compute
    rolling mean and standard deviation, then flags outliers.

    Usage:
        detector = ZScoreDetector()
        result = detector.check(feature_row)
    """

    def __init__(
        self,
        window_size: int = ROLLING_WINDOW,
        threshold: float = ZSCORE_THRESHOLD,
    ):
        self.window_size = window_size
        self.threshold = threshold
        # One deque per feature for the rolling buffer
        self._buffers: dict[str, deque] = {
            col: deque(maxlen=window_size) for col in FEATURE_COLUMNS
        }

    @property
    def is_warmed_up(self) -> bool:
        """True once the rolling buffer has enough data to be meaningful."""
        return all(len(self._buffers[col]) >= 5 for col in FEATURE_COLUMNS)

    def _get_stats(self, col: str) -> tuple[float, float]:
        """Return (mean, std) for a feature column from the rolling buffer."""
        vals = list(self._buffers[col])
        if len(vals) < 2:
            return 0.0, 1.0
        mean = float(np.mean(vals))
        std = float(np.std(vals)) + 1e-9
        return mean, std

    def update(self, feature_row: dict) -> None:
        """Add a feature row to the rolling buffer (without scoring it)."""
        for col in FEATURE_COLUMNS:
            val = feature_row.get(col, 0.0)
            self._buffers[col].append(float(val))

    def check(self, feature_row: dict) -> ZScoreResult:
        """
        Score a single feature window using the rolling Z-Score baseline.

        The window is added to the rolling buffer AFTER scoring so it
        doesn't influence its own score.

        Args:
            feature_row: Dict with keys matching FEATURE_COLUMNS + 'window_start'.

        Returns:
            ZScoreResult describing which features (if any) triggered.
        """
        ws = feature_row.get("window_start", "")
        if hasattr(ws, "isoformat"):
            ws = ws.isoformat()

        feature_values = {col: float(feature_row.get(col, 0.0)) for col in FEATURE_COLUMNS}
        zscores: dict[str, float] = {}
        triggered: list[str] = []

        if not self.is_warmed_up:
            logger.debug("ZScore detector not yet warmed up (%d windows seen)", len(self._buffers[FEATURE_COLUMNS[0]]))
        else:
            for col in FEATURE_COLUMNS:
                mean, std = self._get_stats(col)
                val = feature_values[col]
                z = (val - mean) / std
                zscores[col] = z
                if abs(z) > self.threshold:
                    triggered.append(col)

        # Update buffer with this window's values
        self.update(feature_row)

        return ZScoreResult(
            window_start=str(ws),
            is_anomaly=len(triggered) > 0,
            triggered_features=triggered,
            feature_zscores=zscores,
            feature_values=feature_values,
            threshold=self.threshold,
        )

    def reset(self) -> None:
        """Clear the rolling buffer (e.g. after a long gap in data)."""
        for col in FEATURE_COLUMNS:
            self._buffers[col].clear()


# ─── Module-level singleton ───────────────────────────────────────────────────
_detector: Optional[ZScoreDetector] = None


def get_detector() -> ZScoreDetector:
    """Return the module-level singleton ZScoreDetector."""
    global _detector
    if _detector is None:
        _detector = ZScoreDetector()
    return _detector


def check(feature_row: dict) -> ZScoreResult:
    """Convenience wrapper around the module-level singleton detector."""
    return get_detector().check(feature_row)
