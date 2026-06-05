"""
src/ingestion/cloudwatch_reader.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Polls AWS CloudWatch Logs for new events, deduplicates by eventId,
and persists structured rows to the local SQLite database.
"""

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

from src.ingestion.log_parser import parse_log_line

load_dotenv()

logger = logging.getLogger(__name__)

LOG_GROUP = os.getenv("CLOUDWATCH_LOG_GROUP", "/log-anomaly-detector/app")
LOG_STREAM = os.getenv("CLOUDWATCH_LOG_STREAM", "app-stream")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
DB_PATH = Path(os.getenv("DB_PATH", "logs.db"))

# How far back to look on the first poll (in minutes)
INITIAL_LOOKBACK_MINUTES = int(os.getenv("INITIAL_LOOKBACK_MINUTES", "10"))


def _get_db_connection() -> sqlite3.Connection:
    """Open (and initialise) the local SQLite database."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS logs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id     TEXT UNIQUE,
            timestamp    TEXT NOT NULL,
            level        TEXT,
            method       TEXT,
            endpoint     TEXT,
            status_code  INTEGER,
            latency_ms   INTEGER,
            request_id   TEXT,
            message      TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp);
        CREATE INDEX IF NOT EXISTS idx_logs_event_id  ON logs(event_id);

        CREATE TABLE IF NOT EXISTS poll_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """
    )
    conn.commit()


def _get_last_poll_time(conn: sqlite3.Connection) -> int:
    """Return the last-polled CloudWatch timestamp (ms epoch), or a default."""
    row = conn.execute("SELECT value FROM poll_state WHERE key = 'last_poll_ms'").fetchone()
    if row:
        return int(row["value"])
    # Default: look back INITIAL_LOOKBACK_MINUTES
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=INITIAL_LOOKBACK_MINUTES)
    return int(cutoff.timestamp() * 1000)


def _save_poll_time(conn: sqlite3.Connection, ts_ms: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO poll_state (key, value) VALUES ('last_poll_ms', ?)",
        (str(ts_ms),),
    )
    conn.commit()


def _fetch_cloudwatch_events(
    client,
    start_time_ms: int,
    end_time_ms: int,
) -> Iterator[dict]:
    """
    Yield raw CloudWatch log events between two epoch-ms timestamps.
    Handles pagination transparently.
    """
    kwargs = {
        "logGroupName": LOG_GROUP,
        "logStreamNames": [LOG_STREAM],
        "startTime": start_time_ms,
        "endTime": end_time_ms,
        "interleaved": True,
    }
    while True:
        try:
            response = client.filter_log_events(**kwargs)
        except ClientError as e:
            logger.error("CloudWatch filter_log_events failed: %s", e)
            break

        for event in response.get("events", []):
            yield event

        next_token = response.get("nextToken")
        if not next_token:
            break
        kwargs["nextToken"] = next_token


def _store_parsed_logs(conn: sqlite3.Connection, parsed: list[dict]) -> int:
    """
    Insert parsed log dicts into the logs table.
    Returns the number of rows actually inserted (duplicates skipped).
    """
    inserted = 0
    for log in parsed:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO logs
                   (event_id, timestamp, level, method, endpoint,
                    status_code, latency_ms, request_id, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    log.get("event_id"),
                    log.get("timestamp"),
                    log.get("level"),
                    log.get("method"),
                    log.get("endpoint"),
                    log.get("status_code"),
                    log.get("latency_ms"),
                    log.get("request_id"),
                    log.get("message"),
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except sqlite3.Error as e:
            logger.warning("Failed to insert log row: %s | %s", e, log)

    conn.commit()
    return inserted


def poll_once() -> int:
    """
    Run a single poll cycle: fetch new CloudWatch events and persist them.

    Returns:
        Number of new log rows stored.
    """
    conn = _get_db_connection()
    client = boto3.client("logs", region_name=AWS_REGION)

    start_ms = _get_last_poll_time(conn)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    logger.info(
        "Polling CloudWatch: %s → %s",
        datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(),
        datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).isoformat(),
    )

    raw_events = list(_fetch_cloudwatch_events(client, start_ms, end_ms))
    logger.info("Fetched %d raw events from CloudWatch", len(raw_events))

    parsed = []
    for event in raw_events:
        result = parse_log_line(event["message"], event_id=event.get("eventId"))
        if result:
            parsed.append(result)

    stored = _store_parsed_logs(conn, parsed)
    _save_poll_time(conn, end_ms)
    conn.close()

    logger.info("Stored %d new log rows (skipped %d duplicates)", stored, len(parsed) - stored)
    return stored


def poll_loop(interval_seconds: int = 60) -> None:
    """
    Continuously poll CloudWatch every `interval_seconds` seconds.
    Intended for the main scheduler loop.
    """
    logger.info("Starting CloudWatch poll loop (interval=%ds)", interval_seconds)
    while True:
        try:
            count = poll_once()
            logger.info("Poll complete: %d new rows ingested", count)
        except Exception as e:
            logger.error("Poll cycle failed: %s", e, exc_info=True)
        time.sleep(interval_seconds)
