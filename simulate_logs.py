"""
simulate_logs.py
~~~~~~~~~~~~~~~~
Generates realistic synthetic application logs and pushes them to
AWS CloudWatch Logs. Supports normal traffic and anomaly injection modes.

Usage:
    python simulate_logs.py --days 7 --mode normal
    python simulate_logs.py --days 1 --mode anomaly
    python simulate_logs.py --days 7 --mode normal --dry-run
"""

import json
import logging
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

import boto3
import click
import numpy as _np
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

load_dotenv()

console = Console()
logging.basicConfig(level=logging.WARNING)

# ─── Config ──────────────────────────────────────────────────────────────────
LOG_GROUP = os.getenv("CLOUDWATCH_LOG_GROUP", "/log-anomaly-detector/app")
LOG_STREAM = os.getenv("CLOUDWATCH_LOG_STREAM", "app-stream")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() == "true"

# ─── Realistic endpoint pool ──────────────────────────────────────────────────
ENDPOINTS = [
    "/api/users", "/api/products", "/api/orders", "/api/payment",
    "/api/auth/login", "/api/auth/logout", "/api/search", "/api/cart",
    "/api/checkout", "/api/profile", "/api/recommendations", "/health",
    "/api/inventory", "/api/reviews", "/api/notifications",
]

METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH"]
METHOD_WEIGHTS = [50, 25, 10, 5, 10]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "python-requests/2.31.0",
    "curl/7.88.1",
    "PostmanRuntime/7.36.0",
]


def _normal_log(ts: datetime) -> dict:
    """Generate a single normal log entry."""
    endpoint = random.choice(ENDPOINTS)
    method = random.choices(METHODS, weights=METHOD_WEIGHTS)[0]

    # Normal: mostly 2xx, occasional 4xx
    status_weights = [70, 15, 10, 5]  # 200, 201, 400, 404
    status = random.choices([200, 201, 400, 404], weights=status_weights)[0]

    # Normal latency: 30–400ms, log-normal distribution
    latency = max(10, int(random.lognormvariate(4.5, 0.6)))

    return {
        "timestamp": ts.isoformat(),
        "level": "ERROR" if status >= 500 else ("WARN" if status >= 400 else "INFO"),
        "method": method,
        "endpoint": endpoint,
        "status_code": status,
        "latency_ms": latency,
        "request_id": str(uuid.uuid4())[:8],
        "user_agent": random.choice(USER_AGENTS),
        "message": f"{method} {endpoint} {status} {latency}ms",
    }


def _error_spike_log(ts: datetime) -> dict:
    """Generate a log entry during an error spike (500s)."""
    log = _normal_log(ts)
    log["status_code"] = random.choices([500, 502, 503, 504], weights=[40, 20, 30, 10])[0]
    log["level"] = "ERROR"
    log["latency_ms"] = random.randint(800, 3000)
    log["message"] = (
        f"{log['method']} {log['endpoint']} {log['status_code']} "
        f"{log['latency_ms']}ms [upstream timeout]"
    )
    return log


def _latency_spike_log(ts: datetime) -> dict:
    """Generate a log entry during a latency spike."""
    log = _normal_log(ts)
    log["latency_ms"] = random.randint(2000, 8000)
    log["status_code"] = random.choices([200, 504], weights=[60, 40])[0]
    if log["status_code"] == 504:
        log["level"] = "ERROR"
    log["message"] = (
        f"{log['method']} {log['endpoint']} {log['status_code']} "
        f"{log['latency_ms']}ms [slow query detected]"
    )
    return log


def _auth_storm_log(ts: datetime) -> dict:
    """Generate a log entry during an auth failure storm."""
    log = _normal_log(ts)
    log["endpoint"] = "/api/auth/login"
    log["method"] = "POST"
    log["status_code"] = 401
    log["level"] = "WARN"
    log["latency_ms"] = random.randint(50, 200)
    log["message"] = f"POST /api/auth/login 401 {log['latency_ms']}ms [invalid credentials]"
    return log


def _volume_spike_log(ts: datetime) -> dict:
    """Generate a log entry during a traffic volume spike."""
    log = _normal_log(ts)
    return log  # same format, just more of them


def generate_logs(
    start_time: datetime,
    end_time: datetime,
    mode: str = "normal",
    base_rps: float = 5.0,
) -> list:
    """
    Generate a list of log dicts between start_time and end_time.

    Args:
        start_time: Start of the generation window.
        end_time: End of the generation window.
        mode: "normal" or "anomaly".
        base_rps: Average requests per second during normal traffic.

    Returns:
        List of log entry dicts sorted by timestamp.
    """
    logs = []
    current = start_time
    total_seconds = int((end_time - start_time).total_seconds())

    # Anomaly windows: inject at random 5-minute intervals across the day
    anomaly_windows = []
    if mode == "anomaly":
        # Pick 3–5 random anomaly bursts distributed across the time range
        num_anomalies = random.randint(3, 5)
        for _ in range(num_anomalies):
            anomaly_start_offset = random.randint(0, total_seconds - 300)
            anomaly_start = start_time + timedelta(seconds=anomaly_start_offset)
            anomaly_end = anomaly_start + timedelta(minutes=5)
            anomaly_type = random.choice(
                ["error_spike", "latency_spike", "auth_storm", "volume_spike"]
            )
            anomaly_windows.append((anomaly_start, anomaly_end, anomaly_type))
            console.print(
                f"  [yellow]⚡ Anomaly [{anomaly_type}] at "
                f"{anomaly_start.strftime('%Y-%m-%d %H:%M:%S')} → "
                f"{anomaly_end.strftime('%H:%M:%S')}[/yellow]"
            )

    while current < end_time:
        # Determine if we're in an anomaly window
        active_anomaly = None
        for (a_start, a_end, a_type) in anomaly_windows:
            if a_start <= current <= a_end:
                active_anomaly = a_type
                break

        # Decide request rate and log generator for this second
        if active_anomaly == "volume_spike":
            rps = base_rps * random.uniform(8, 15)
            log_fn = _volume_spike_log
        elif active_anomaly == "error_spike":
            rps = base_rps * random.uniform(2, 4)
            log_fn = _error_spike_log
        elif active_anomaly == "latency_spike":
            rps = base_rps * random.uniform(1, 2)
            log_fn = _latency_spike_log
        elif active_anomaly == "auth_storm":
            rps = base_rps * random.uniform(3, 6)
            log_fn = _auth_storm_log
        else:
            # Normal: slight randomness with time-of-day pattern
            hour = current.hour
            if 9 <= hour <= 18:
                multiplier = random.uniform(1.0, 2.0)
            elif 0 <= hour <= 6:
                multiplier = random.uniform(0.1, 0.4)
            else:
                multiplier = random.uniform(0.5, 1.2)
            rps = base_rps * multiplier
            log_fn = _normal_log

        # Generate logs for this second
        num_requests = max(0, int(random.poisson(rps) if rps > 0 else 0))
        for _ in range(num_requests):
            jitter_ms = random.randint(0, 999)
            ts = current + timedelta(milliseconds=jitter_ms)
            logs.append(log_fn(ts))

        current += timedelta(seconds=1)

    logs.sort(key=lambda x: x["timestamp"])
    return logs


def _get_cw_client():
    return boto3.client("logs", region_name=AWS_REGION)


def _ensure_log_group_and_stream(client):
    """Create log group and stream if they don't exist."""
    try:
        client.create_log_group(logGroupName=LOG_GROUP)
        console.print(f"  [green]✓ Created log group:[/green] {LOG_GROUP}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise

    try:
        client.create_log_stream(logGroupName=LOG_GROUP, logStreamName=LOG_STREAM)
        console.print(f"  [green]✓ Created log stream:[/green] {LOG_STREAM}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise


def _push_to_cloudwatch(client, logs: list):
    """Push log entries to CloudWatch Logs in batches of 10,000."""
    BATCH_SIZE = 10_000
    MAX_BATCH_BYTES = 1_000_000

    # Get sequence token
    seq_token = None
    try:
        response = client.describe_log_streams(
            logGroupName=LOG_GROUP,
            logStreamNamePrefix=LOG_STREAM,
        )
        streams = response.get("logStreams", [])
        if streams:
            seq_token = streams[0].get("uploadSequenceToken")
    except ClientError:
        pass

    for i in range(0, len(logs), BATCH_SIZE):
        batch = logs[i: i + BATCH_SIZE]
        batch_bytes = 0
        sub_batches = []
        sub_batch = []

        for log in batch:
            msg = json.dumps(log)
            event = {
                "timestamp": int(
                    datetime.fromisoformat(log["timestamp"])
                    .replace(tzinfo=timezone.utc)
                    .timestamp() * 1000
                ),
                "message": msg,
            }
            event_bytes = len(msg.encode("utf-8")) + 26  # CloudWatch overhead
            if batch_bytes + event_bytes > MAX_BATCH_BYTES:
                sub_batches.append(sub_batch)
                sub_batch = []
                batch_bytes = 0
            sub_batch.append(event)
            batch_bytes += event_bytes

        if sub_batch:
            sub_batches.append(sub_batch)

        for sb in sub_batches:
            kwargs = {
                "logGroupName": LOG_GROUP,
                "logStreamName": LOG_STREAM,
                "logEvents": sorted(sb, key=lambda e: e["timestamp"]),
            }
            if seq_token:
                kwargs["sequenceToken"] = seq_token
            try:
                resp = client.put_log_events(**kwargs)
                seq_token = resp.get("nextSequenceToken")
            except ClientError as e:
                if e.response["Error"]["Code"] == "InvalidSequenceTokenException":
                    seq_token = e.response["Error"]["Message"].split()[-1]
                    kwargs["sequenceToken"] = seq_token
                    resp = client.put_log_events(**kwargs)
                    seq_token = resp.get("nextSequenceToken")
                else:
                    raise


def _push_to_sqlite(logs: list):
    """Persist logs directly to local SQLite (used in both modes for local storage)."""
    import sqlite3
    from pathlib import Path

    db_path = Path("logs.db")
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            level TEXT,
            method TEXT,
            endpoint TEXT,
            status_code INTEGER,
            latency_ms INTEGER,
            request_id TEXT,
            message TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)")
    conn.executemany(
        """INSERT INTO logs (timestamp, level, method, endpoint, status_code,
                              latency_ms, request_id, message)
           VALUES (:timestamp, :level, :method, :endpoint, :status_code,
                   :latency_ms, :request_id, :message)""",
        logs,
    )
    conn.commit()
    conn.close()


# Monkey-patch numpy random for Poisson since we don't import numpy elsewhere
random.poisson = _np.random.poisson  # type: ignore


@click.command()
@click.option("--days", default=7, show_default=True, help="Number of days of logs to generate")
@click.option(
    "--mode",
    type=click.Choice(["normal", "anomaly"]),
    default="normal",
    show_default=True,
    help="normal = clean traffic; anomaly = inject anomaly bursts",
)
@click.option("--base-rps", default=5.0, show_default=True, help="Base requests per second")
@click.option("--dry-run", is_flag=True, help="Generate logs but don't push anywhere")
def main(days: int, mode: str, base_rps: float, dry_run: bool):
    """
    Synthetic log generator for the Intelligent Log Anomaly Detector.

    Generates realistic app logs and pushes them to AWS CloudWatch Logs
    and local SQLite for development/testing.
    """
    console.print(
        f"\n[bold cyan]🔧 Log Simulator[/bold cyan] | "
        f"mode=[yellow]{mode}[/yellow] | "
        f"days=[yellow]{days}[/yellow] | "
        f"base_rps=[yellow]{base_rps}[/yellow] | "
        f"mock=[yellow]{MOCK_MODE}[/yellow]\n"
    )

    end_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start_time = end_time - timedelta(days=days)

    console.print(f"  Generating logs from [dim]{start_time}[/dim] → [dim]{end_time}[/dim]")
    if mode == "anomaly":
        console.print("  [bold yellow]Anomaly injection windows:[/bold yellow]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console,
    ) as progress:
        task = progress.add_task("Generating logs...", total=days)
        all_logs = []

        # Generate day by day for memory efficiency
        current_start = start_time
        for day in range(days):
            current_end = current_start + timedelta(days=1)
            day_logs = generate_logs(current_start, current_end, mode=mode, base_rps=base_rps)
            all_logs.extend(day_logs)
            progress.advance(task)
            current_start = current_end

    console.print(f"\n  [green]✓ Generated [bold]{len(all_logs):,}[/bold] log entries[/green]")

    if dry_run:
        console.print("  [dim]--dry-run: skipping push[/dim]")
        console.print(f"\n  Sample entry:\n  {json.dumps(all_logs[0], indent=2)}")
        return

    # Always push to local SQLite
    console.print("\n  [cyan]→ Writing to local SQLite (logs.db)...[/cyan]")
    _push_to_sqlite(all_logs)
    console.print(f"  [green]✓ Stored {len(all_logs):,} rows in logs.db[/green]")

    # Push to CloudWatch (unless mock mode)
    if MOCK_MODE:
        console.print("  [dim]MOCK_MODE=true — skipping CloudWatch push[/dim]")
    else:
        console.print(f"\n  [cyan]→ Pushing to CloudWatch ({LOG_GROUP})...[/cyan]")
        client = _get_cw_client()
        _ensure_log_group_and_stream(client)
        _push_to_cloudwatch(client, all_logs)
        console.print(f"  [green]✓ Pushed {len(all_logs):,} events to CloudWatch[/green]")

    console.print("\n[bold green]✅ Done![/bold green]\n")


if __name__ == "__main__":
    main()
