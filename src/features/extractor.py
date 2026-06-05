"""
src/features/extractor.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Aggregates raw logs from SQLite into 1-minute tumbling window feature vectors.

Features per window:
    - error_rate         : fraction of 4xx/5xx responses
    - avg_latency_ms     : mean response time
    - p95_latency        : 95th-percentile latency
    - request_volume     : total request count
    - unique_endpoints   : number of distinct endpoints hit
    - failed_auth_count  : count of 401 responses
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", "logs.db"))
WINDOW_MINUTES = int(os.getenv("WINDOW_MINUTES", "1"))

FEATURE_COLUMNS = [
    "error_rate",
    "avg_latency_ms",
    "p95_latency",
    "request_volume",
    "unique_endpoints",
    "failed_auth_count",
]


def _get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _load_logs(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Load raw logs from SQLite for the given time range."""
    query = """
        SELECT timestamp, status_code, latency_ms, endpoint
        FROM logs
        WHERE timestamp >= ? AND timestamp < ?
        ORDER BY timestamp
    """
    rows = conn.execute(
        query,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    if not rows:
        return pd.DataFrame(columns=["timestamp", "status_code", "latency_ms", "endpoint"])

    df = pd.DataFrame([dict(r) for r in rows])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["latency_ms"] = pd.to_numeric(df["latency_ms"], errors="coerce").fillna(0)
    df["status_code"] = pd.to_numeric(df["status_code"], errors="coerce").fillna(0).astype(int)
    return df


def _compute_window_features(window_df: pd.DataFrame) -> dict:
    """Compute all feature values for a single time window."""
    n = len(window_df)
    if n == 0:
        return {col: 0.0 for col in FEATURE_COLUMNS}

    error_mask = window_df["status_code"] >= 400
    auth_mask = window_df["status_code"] == 401

    latencies = window_df["latency_ms"].values
    error_rate = float(error_mask.sum()) / n
    avg_latency = float(np.mean(latencies)) if n > 0 else 0.0
    p95_latency = float(np.percentile(latencies, 95)) if n > 0 else 0.0
    unique_ep = int(window_df["endpoint"].nunique())
    failed_auth = int(auth_mask.sum())

    return {
        "error_rate": round(error_rate, 4),
        "avg_latency_ms": round(avg_latency, 2),
        "p95_latency": round(p95_latency, 2),
        "request_volume": n,
        "unique_endpoints": unique_ep,
        "failed_auth_count": failed_auth,
    }


def extract_features(
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    window_minutes: int = WINDOW_MINUTES,
) -> pd.DataFrame:
    """
    Extract time-windowed features from the logs table.

    Args:
        start:          Start of the feature extraction range (UTC).
                        Defaults to 24 hours ago.
        end:            End of the range (UTC). Defaults to now.
        window_minutes: Size of each tumbling window in minutes.

    Returns:
        DataFrame with columns: window_start + FEATURE_COLUMNS,
        one row per time window, sorted ascending by window_start.
    """
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if end is None:
        end = now
    if start is None:
        start = end - timedelta(hours=24)

    conn = _get_db_connection()
    try:
        raw_df = _load_logs(conn, start, end)
    finally:
        conn.close()

    if raw_df.empty:
        logger.warning("No logs found for %s → %s", start.isoformat(), end.isoformat())
        return pd.DataFrame(columns=["window_start"] + FEATURE_COLUMNS)

    # Assign each log row to a 1-minute bucket
    raw_df["window_start"] = raw_df["timestamp"].dt.floor(f"{window_minutes}min")

    # Build complete list of windows (even empty ones get zeros)
    window_starts = pd.date_range(
        start=start.replace(tzinfo=timezone.utc),
        end=end.replace(tzinfo=timezone.utc),
        freq=f"{window_minutes}min",
        inclusive="left",
    )

    records = []
    for ws in window_starts:
        window_df = raw_df[raw_df["window_start"] == ws]
        feats = _compute_window_features(window_df)
        feats["window_start"] = ws
        records.append(feats)

    result = pd.DataFrame(records, columns=["window_start"] + FEATURE_COLUMNS)
    result = result.sort_values("window_start").reset_index(drop=True)
    return result


def extract_latest_window(window_minutes: int = WINDOW_MINUTES) -> Optional[dict]:
    """
    Extract features for the most recently completed 1-minute window.

    Returns:
        Dict of feature values with a 'window_start' key, or None if no data.
    """
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    end = now
    start = end - timedelta(minutes=window_minutes)

    df = extract_features(start=start, end=end, window_minutes=window_minutes)
    if df.empty:
        return None

    row = df.iloc[-1]
    return row.to_dict()


def store_features(features_df: pd.DataFrame) -> None:
    """
    Persist computed feature windows to the SQLite features table.

    Args:
        features_df: DataFrame as returned by extract_features().
    """
    if features_df.empty:
        return

    conn = _get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS features (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            window_start     TEXT UNIQUE NOT NULL,
            error_rate       REAL,
            avg_latency_ms   REAL,
            p95_latency      REAL,
            request_volume   INTEGER,
            unique_endpoints INTEGER,
            failed_auth_count INTEGER,
            created_at       TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_features_window ON features(window_start)"
    )

    for _, row in features_df.iterrows():
        ws = row["window_start"]
        if hasattr(ws, "isoformat"):
            ws = ws.isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO features
               (window_start, error_rate, avg_latency_ms, p95_latency,
                request_volume, unique_endpoints, failed_auth_count)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                ws,
                row["error_rate"],
                row["avg_latency_ms"],
                row["p95_latency"],
                int(row["request_volume"]),
                int(row["unique_endpoints"]),
                int(row["failed_auth_count"]),
            ),
        )
    conn.commit()
    conn.close()
    logger.info("Stored %d feature windows", len(features_df))
