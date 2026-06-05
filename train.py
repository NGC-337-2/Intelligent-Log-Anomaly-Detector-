"""
train.py
~~~~~~~~~
One-shot model training script.

Loads all feature windows from SQLite and fits an Isolation Forest.
Optionally uploads the serialized model to S3.

Usage:
    python train.py
    python train.py --lookback-days 14
    python train.py --no-s3
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).parent))

from src.features.extractor import extract_features, store_features, FEATURE_COLUMNS
from src.detector.isolation_forest import train, MODEL_PATH

console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@click.command()
@click.option(
    "--lookback-days",
    default=7,
    show_default=True,
    help="Number of days of historical data to train on",
)
@click.option(
    "--contamination",
    default=0.05,
    show_default=True,
    help="Expected fraction of anomalies in training data (IsolationForest parameter)",
)
@click.option(
    "--n-estimators",
    default=100,
    show_default=True,
    help="Number of isolation trees",
)
@click.option(
    "--no-s3",
    is_flag=True,
    help="Skip S3 upload (use local model file only)",
)
def main(lookback_days: int, contamination: float, n_estimators: int, no_s3: bool):
    """Train the Isolation Forest anomaly detector on historical log features."""
    console.print("\n[bold cyan]🤖 Isolation Forest Trainer[/bold cyan]\n")

    # ── Step 1: Extract features ──────────────────────────────────────────────
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=lookback_days)

    console.print(
        f"  Extracting features | "
        f"[dim]{start.strftime('%Y-%m-%d')}[/dim] → [dim]{end.strftime('%Y-%m-%d')}[/dim]"
    )
    features_df = extract_features(start=start, end=end)

    if features_df.empty:
        console.print(
            "[bold red]✗ No features found. "
            "Run simulate_logs.py first to generate training data.[/bold red]"
        )
        sys.exit(1)

    console.print(f"  [green]✓ Extracted {len(features_df):,} feature windows[/green]")

    # Show feature stats
    table = Table(title="Feature Summary", show_header=True)
    table.add_column("Feature", style="cyan")
    table.add_column("Mean", justify="right")
    table.add_column("Std", justify="right")
    table.add_column("Min", justify="right")
    table.add_column("Max", justify="right")

    for col in FEATURE_COLUMNS:
        s = features_df[col]
        table.add_row(
            col,
            f"{s.mean():.4f}",
            f"{s.std():.4f}",
            f"{s.min():.4f}",
            f"{s.max():.4f}",
        )
    console.print(table)

    # ── Step 2: Persist features to SQLite ───────────────────────────────────
    console.print("  Storing features in SQLite...")
    store_features(features_df)
    console.print("  [green]✓ Features stored[/green]")

    # ── Step 3: Train model ───────────────────────────────────────────────────
    console.print(
        f"\n  Training IsolationForest | "
        f"n_estimators=[yellow]{n_estimators}[/yellow] | "
        f"contamination=[yellow]{contamination}[/yellow]"
    )
    model = train(
        features_df,
        n_estimators=n_estimators,
        contamination=contamination,
        save_to_s3=not no_s3,
    )
    console.print(f"  [green]✓ Model trained and saved to {MODEL_PATH}[/green]")

    # ── Step 4: Quick sanity check ────────────────────────────────────────────
    import numpy as np
    from sklearn.ensemble import IsolationForest as IF

    X = features_df[FEATURE_COLUMNS].values
    scores = model.decision_function(X)
    n_anomalies = int((scores < -0.1).sum())
    pct = 100 * n_anomalies / len(scores)

    console.print(
        f"\n  Sanity check on training data: "
        f"[yellow]{n_anomalies}[/yellow] / {len(scores)} windows flagged as anomalous "
        f"([yellow]{pct:.1f}%[/yellow])"
    )
    if abs(pct - contamination * 100) > 5:
        console.print(
            "  [yellow]⚠ Anomaly rate differs significantly from contamination parameter — "
            "consider re-running simulate_logs.py with cleaner normal data.[/yellow]"
        )

    console.print("\n[bold green]✅ Training complete![/bold green]")
    console.print("  Next step: [cyan]python main.py --once[/cyan] to run the pipeline\n")


if __name__ == "__main__":
    main()
