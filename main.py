"""
main.py
~~~~~~~~
Pipeline entry point and scheduler loop.

Orchestrates the full pipeline:
  1. Poll CloudWatch Logs for new events (ingestion)
  2. Parse + store to SQLite
  3. Extract 1-minute feature windows
  4. Score with Isolation Forest + Z-Score baseline
  5. Dispatch alerts if anomalies detected

Modes:
  --once   : Run a single pass and exit (for testing/demo)
  --loop   : Run continuously every POLL_INTERVAL_SECONDS (default)

Usage:
    python main.py
    python main.py --once
    python main.py --loop --interval 30
"""

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent))

from src.ingestion.cloudwatch_reader import poll_once  # noqa: E402
from src.features.extractor import extract_features, extract_latest_window  # noqa: E402
from src.detector.isolation_forest import load_model, score as if_score  # noqa: E402
from src.detector.zscore_baseline import get_detector as get_zscore_detector  # noqa: E402
from src.alerts.engine import process_result, get_alert_history  # noqa: E402
from src.alerts.cooldown import purge_expired  # noqa: E402

load_dotenv()

console = Console()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("pipeline.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() == "true"


# ─── Status display ───────────────────────────────────────────────────────────


def _make_status_table(stats: dict) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value", style="bold")

    table.add_row("Pipeline run #", str(stats.get("run_number", 0)))
    table.add_row("Last poll", stats.get("last_poll", "—"))
    table.add_row("New logs ingested", str(stats.get("new_logs", 0)))
    table.add_row("IF score (last window)", str(stats.get("if_score", "—")))
    table.add_row("ZScore triggered", str(stats.get("zscore_triggered", "—")))
    table.add_row("Alerts fired (session)", str(stats.get("alerts_fired", 0)))
    table.add_row("Total alerts (DB)", str(stats.get("total_alerts", 0)))
    return table


def _print_run_header(run_number: int) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    console.rule(f"[bold cyan]Pipeline Run #{run_number}[/bold cyan] — {ts}")


# ─── Single pipeline pass ──────────────────────────────────────────────────────


def run_pipeline_once(
    model,
    zscore_detector,
    run_number: int = 1,
) -> dict:
    """
    Execute a single pipeline pass.

    Returns:
        Stats dict for status display.
    """
    stats: dict = {"run_number": run_number, "alerts_fired": 0}

    # 1. Ingest from CloudWatch
    try:
        if MOCK_MODE:
            logger.info("MOCK_MODE: skipping CloudWatch poll")
            new_logs = 0
        else:
            new_logs = poll_once()
        stats["new_logs"] = new_logs
        stats["last_poll"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        logger.info("Ingested %d new log rows", new_logs)
    except Exception as e:
        logger.error("Ingestion failed: %s", e, exc_info=True)
        stats["new_logs"] = 0

    # 2. Extract features for the last completed minute
    try:
        feature_row = extract_latest_window()
        if feature_row is None:
            logger.info("No feature data for latest window — skipping scoring")
            return stats
    except Exception as e:
        logger.error("Feature extraction failed: %s", e, exc_info=True)
        return stats

    # 3. Score with Isolation Forest
    if_result = None
    if model is not None:
        try:
            if_result = if_score(feature_row, model=model)
            if if_result:
                stats["if_score"] = round(if_result.anomaly_score, 4)
                logger.info(
                    "IF score=%.4f | is_anomaly=%s | severity=%s",
                    if_result.anomaly_score,
                    if_result.is_anomaly,
                    if_result.severity,
                )
        except Exception as e:
            logger.error("IF scoring failed: %s", e, exc_info=True)
    else:
        logger.warning("No trained model — running Z-Score only")

    # 4. Score with Z-Score baseline
    zs_result = None
    try:
        zs_result = zscore_detector.check(feature_row)
        stats["zscore_triggered"] = zs_result.triggered_features or "none"
        if zs_result.is_anomaly:
            logger.warning("ZScore anomaly | features=%s", zs_result.triggered_features)
    except Exception as e:
        logger.error("Z-Score scoring failed: %s", e, exc_info=True)

    # 5. Dispatch alerts
    if if_result and if_result.is_anomaly:
        fired = process_result(if_result)
        if fired:
            stats["alerts_fired"] += 1
            console.print(
                f"  [bold red]🚨 ALERT[/bold red] | severity=[red]{if_result.severity}[/red] | "
                f"score=[yellow]{if_result.anomaly_score:.4f}[/yellow] | "
                f"features={if_result.top_features}"
            )

    if zs_result and zs_result.is_anomaly:
        # Only dispatch Z-Score alert if IF didn't already fire on same features
        if not if_result or not if_result.is_anomaly:
            fired = process_result(zs_result)
            if fired:
                stats["alerts_fired"] += 1
                console.print(
                    f"  [bold yellow]⚡ ZSCORE ALERT[/bold yellow] | "
                    f"features={zs_result.triggered_features}"
                )

    # 6. Periodic housekeeping
    if run_number % 10 == 0:
        deleted = purge_expired()
        if deleted:
            logger.debug("Purged %d expired cooldown records", deleted)

    # Total alerts in DB
    try:
        history = get_alert_history(limit=1000)
        stats["total_alerts"] = len(history)
    except Exception:
        pass

    return stats


# ─── CLI ──────────────────────────────────────────────────────────────────────


@click.command()
@click.option("--once", "mode", flag_value="once", help="Run pipeline once and exit")
@click.option(
    "--loop",
    "mode",
    flag_value="loop",
    default=True,
    show_default=True,
    help="Run pipeline continuously (default)",
)
@click.option(
    "--interval",
    default=POLL_INTERVAL,
    show_default=True,
    help="Seconds between pipeline runs in loop mode",
)
@click.option("--retrain", is_flag=True, help="Re-train the model before starting")
def main(mode: str, interval: int, retrain: bool):
    """
    Intelligent Log Anomaly Detector — pipeline entry point.

    Ingests CloudWatch logs, extracts features, scores anomalies,
    and dispatches alerts via SNS.
    """
    console.print(
        Panel.fit(
            "[bold cyan]Intelligent Log Anomaly Detector[/bold cyan]\n"
            f"mode=[yellow]{mode}[/yellow] | "
            f"interval=[yellow]{interval}s[/yellow] | "
            f"mock=[yellow]{MOCK_MODE}[/yellow]",
            border_style="cyan",
        )
    )

    # Load model
    if retrain:
        console.print("  [yellow]--retrain: running train.py first...[/yellow]")
        import subprocess

        subprocess.run([sys.executable, "train.py"], check=True)

    model = load_model()
    if model is None:
        console.print(
            "[yellow]⚠ No trained model found. "
            "Run [bold]python train.py[/bold] first for full detection. "
            "Continuing with Z-Score baseline only.[/yellow]"
        )

    zscore_detector = get_zscore_detector()

    # Warm up Z-Score with recent history
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=30)
        warmup_df = extract_features(start=start, end=end)
        if not warmup_df.empty:
            for _, row in warmup_df.iterrows():
                zscore_detector.update(row.to_dict())
            console.print(
                f"  [green]✓ Z-Score warmed up with {len(warmup_df)} historical windows[/green]"
            )
    except Exception as e:
        logger.warning("Z-Score warm-up failed: %s", e)

    console.print()

    if mode == "once":
        _print_run_header(1)
        stats = run_pipeline_once(model, zscore_detector, run_number=1)
        _print_stats(stats)
        console.print("\n[bold green]✅ Single-pass complete[/bold green]\n")
        return

    # Continuous loop
    run_number = 0
    session_alerts = 0
    try:
        while True:
            run_number += 1
            _print_run_header(run_number)
            stats = run_pipeline_once(model, zscore_detector, run_number=run_number)
            session_alerts += stats.get("alerts_fired", 0)
            stats["alerts_fired"] = session_alerts
            _print_stats(stats)
            console.print(f"  [dim]Next run in {interval}s — press Ctrl+C to stop[/dim]\n")
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Pipeline stopped by user.[/bold yellow]\n")


def _print_stats(stats: dict) -> None:
    table = _make_status_table(stats)
    console.print(table)


if __name__ == "__main__":
    main()
