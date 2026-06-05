"""
src/alerts/cooldown.py
~~~~~~~~~~~~~~~~~~~~~~~
SQLite-backed alert cooldown/deduplication logic.

Suppresses repeated alerts of the same "type" (derived from the top
triggering features) within a configurable cooldown window.
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", "logs.db"))
COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "10"))


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_cooldowns (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            feature_key     TEXT NOT NULL,
            last_alerted_at TEXT NOT NULL,
            alert_count     INTEGER DEFAULT 1
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cooldown_key "
        "ON alert_cooldowns(feature_key)"
    )
    conn.commit()
    return conn


def _make_feature_key(top_features: list[str]) -> str:
    """
    Create a stable string key from the top triggering features.
    E.g. ["error_rate", "p95_latency"] → "error_rate|p95_latency"
    """
    return "|".join(sorted(top_features[:3]))


def is_suppressed(top_features: list[str], cooldown_minutes: int = COOLDOWN_MINUTES) -> bool:
    """
    Check if an alert for this feature combination is within the cooldown window.

    Args:
        top_features:     Top features that triggered the anomaly.
        cooldown_minutes: Suppression window in minutes.

    Returns:
        True if the alert should be suppressed (recently fired), False otherwise.
    """
    key = _make_feature_key(top_features)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
    ).isoformat()

    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT last_alerted_at FROM alert_cooldowns WHERE feature_key = ?",
            (key,),
        ).fetchone()

        if row is None:
            return False  # Never alerted for this feature combo

        last_alerted = row["last_alerted_at"]
        return last_alerted >= cutoff  # Still within cooldown window
    finally:
        conn.close()


def record_alert(top_features: list[str]) -> None:
    """
    Record that an alert was fired for this feature combination right now.

    Args:
        top_features: Top triggering feature names.
    """
    key = _make_feature_key(top_features)
    now = datetime.now(timezone.utc).isoformat()

    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO alert_cooldowns (feature_key, last_alerted_at, alert_count)
               VALUES (?, ?, 1)
               ON CONFLICT(feature_key) DO UPDATE SET
                 last_alerted_at = excluded.last_alerted_at,
                 alert_count = alert_count + 1""",
            (key, now),
        )
        conn.commit()
        logger.debug("Recorded alert for key=%s at %s", key, now)
    finally:
        conn.close()


def get_recent_alerts(limit: int = 50) -> list[dict]:
    """
    Retrieve the most recent alert records.

    Args:
        limit: Maximum number of records to return.

    Returns:
        List of dicts with keys: feature_key, last_alerted_at, alert_count.
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT feature_key, last_alerted_at, alert_count
               FROM alert_cooldowns
               ORDER BY last_alerted_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def purge_expired(cooldown_minutes: int = COOLDOWN_MINUTES) -> int:
    """
    Delete cooldown records older than the cooldown window.

    Returns:
        Number of records deleted.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes * 2)
    ).isoformat()

    conn = _get_conn()
    try:
        conn.execute(
            "DELETE FROM alert_cooldowns WHERE last_alerted_at < ?", (cutoff,)
        )
        deleted = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return deleted
    finally:
        conn.close()
