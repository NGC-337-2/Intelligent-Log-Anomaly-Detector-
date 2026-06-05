# 🔍 Intelligent Log Anomaly Detector

An ML-powered log anomaly detection system that ingests AWS CloudWatch logs, extracts time-series features, detects anomalies using Isolation Forest + Z-Score baselines, triggers SNS alerts, and visualizes everything through a Grafana dashboard.

---

## Architecture

```
CloudWatch Logs
      │
      ▼
┌─────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  Log Ingestion  │────▶│ Feature Extractor│────▶│ Anomaly Detector │
│  (boto3 poller) │     │ (1-min windows)  │     │ (IsolationForest)│
└─────────────────┘     └──────────────────┘     └────────┬─────────┘
                                                           │
                              ┌────────────────────────────┤
                              │                            │
                              ▼                            ▼
                    ┌──────────────────┐       ┌──────────────────────┐
                    │  Alert Engine    │       │  CloudWatch Metrics  │
                    │  (SNS + cooldown)│       │  (for Grafana)       │
                    └──────────────────┘       └──────────────────────┘
                              │
                              ▼
                         SNS → Email / Slack
```

## Setup & Run Guide

### Prerequisites

| Tool | Install |
|---|---|
| Python 3.11+ | [python.org](https://python.org) |
| Docker Desktop | [docker.com](https://docker.com) |
| AWS CLI | `winget install Amazon.AWSCLI` |
| Terraform *(optional)* | `winget install Hashicorp.Terraform` |

---

### Step 1 — Create your `.env` file

```bash
cd "d:\Project\Anomly Detection"
copy .env.example .env
```

Open `.env` and fill in your real values:

```ini
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=your_secret...
AWS_DEFAULT_REGION=us-east-1

CLOUDWATCH_LOG_GROUP=/log-anomaly-detector/app
CLOUDWATCH_LOG_STREAM=app-stream
CLOUDWATCH_METRICS_NAMESPACE=LogAnomalyDetector

SNS_TOPIC_ARN=arn:aws:sns:us-east-1:YOUR_ACCOUNT:log-anomaly-alerts
S3_BUCKET=log-anomaly-detector-models
```

---

### Step 2 — Provision AWS Infrastructure (Terraform)

```bash
cd infra/
terraform init
terraform apply -var="alert_email=you@example.com"
# Copy the sns_topic_arn output value into your .env
cd ..
```

> **Skip this step** if you've already created the CloudWatch Log Group and SNS topic manually in the AWS Console.

---

### Step 3 — Install Python dependencies

```bash
pip install -r requirements.txt
```

---

### Step 4 — Generate training data

Creates 7 days of realistic synthetic logs → pushes to CloudWatch **and** saves to local `logs.db`:

```bash
python simulate_logs.py --days 7 --mode normal
```

~300,000 log entries generated in ~30 seconds.

---

### Step 5 — Train the Isolation Forest model

```bash
python train.py
```

This will:
- Extract 1-minute feature windows from `logs.db`
- Print a feature stats summary table
- Save `models/isolation_forest.pkl` locally
- Upload it to your S3 bucket automatically

---

### Step 6 — Run the pipeline (single pass to test)

```bash
# Inject anomaly bursts into 1 day of logs
python simulate_logs.py --days 1 --mode anomaly

# Run one detection cycle
python main.py --once
```

Expected output:
```
━━━━━━━━━ Pipeline Run #1 ━━━━━━━━━
  IF score=-0.4231 | is_anomaly=True | severity=HIGH
  🚨 ALERT dispatched | severity=HIGH | features=['error_rate', 'p95_latency']
```

---

### Step 7 — Run continuously (60-second loop)

```bash
python main.py --loop
```

With a custom interval:

```bash
python main.py --loop --interval 30
```

Press `Ctrl+C` to stop. Logs are written to `pipeline.log`.

---

### Step 8 — Launch Grafana

```bash
docker compose up -d
```

Open **http://localhost:3000** in your browser.

| Field | Value |
|---|---|
| Username | `admin` |
| Password | `admin` |

Navigate to **Dashboards → Log Anomaly Detector**. All 7 panels populate automatically as metrics flow in from the pipeline.

> Grafana may take ~30 seconds to start. Check with `docker compose logs grafana`.

---

### Step 9 — Run tests

```bash
# MOCK_MODE=true skips all real AWS calls — no credentials needed
set MOCK_MODE=true
pytest tests/ -v --tb=short
```

---

## Full Pipeline Flow

```
simulate_logs.py  ──────►  CloudWatch Logs  +  logs.db (SQLite)
                                                      │
train.py  ──────────────►  models/isolation_forest.pkl + S3
                                                      │
main.py --loop                                        │
  ├── cloudwatch_reader  ◄── polls CloudWatch every 60s
  ├── log_parser          ──► structured rows → logs.db
  ├── extractor           ──► 1-min windows → 6 features
  ├── isolation_forest    ──► anomaly score
  ├── zscore_baseline     ──► rolling σ check
  └── alert engine        ──► SNS email + CloudWatch metrics
                                          │
                                    Grafana dashboard
                                   http://localhost:3000
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `No module named boto3` | Run `pip install -r requirements.txt` |
| `No trained model` | Run `python train.py` first |
| `SNS publish failed` | Check `SNS_TOPIC_ARN` in `.env` and IAM permissions |
| Grafana shows "No data" | Confirm AWS credentials are in `.env` and the pipeline has run at least once |
| `logs.db` missing | Run `simulate_logs.py` first — it creates the database |
| Docker Compose fails | Make sure Docker Desktop is running |

---

## Project Structure

```
log-anomaly-detector/
├── infra/                    # Terraform (CloudWatch, S3, SNS, IAM)
│   ├── main.tf
│   ├── variables.tf
│   └── outputs.tf
├── src/
│   ├── ingestion/
│   │   ├── cloudwatch_reader.py   # boto3 CloudWatch Logs poller
│   │   └── log_parser.py          # raw log line → structured JSON
│   ├── features/
│   │   └── extractor.py           # 1-min window feature aggregation
│   ├── detector/
│   │   ├── isolation_forest.py    # IsolationForest train + score
│   │   └── zscore_baseline.py     # rolling Z-Score baseline
│   └── alerts/
│       ├── engine.py              # SNS publisher + threshold logic
│       └── cooldown.py            # 10-min duplicate suppression
├── models/                   # joblib-serialized model files
├── dashboards/               # Grafana dashboard JSON export
├── grafana/                  # Grafana provisioning config
│   └── provisioning/
│       ├── datasources/
│       └── dashboards/
├── tests/                    # pytest unit tests
├── simulate_logs.py          # Synthetic log generator
├── train.py                  # Model training script
├── main.py                   # Scheduler entry point
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Features Extracted (per 1-minute window)

| Feature | Description |
|---|---|
| `error_rate` | % of 4xx/5xx responses |
| `avg_latency_ms` | Mean response time |
| `p95_latency` | 95th percentile latency |
| `request_volume` | Total requests |
| `unique_endpoints` | Distinct endpoints hit |
| `failed_auth_count` | Count of 401 responses |

---

## Anomaly Detection

- **Primary**: `IsolationForest(n_estimators=100, contamination=0.05)` — scores each feature window; score < `-0.1` triggers alert
- **Baseline**: Rolling Z-Score — flags features > 3σ from 30-window rolling mean
- Both models run in parallel; either can trigger an alert

---

## Alert Payload (SNS)

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "anomaly_score": -0.42,
  "top_features": ["error_rate", "p95_latency"],
  "severity": "HIGH",
  "sample_logs": ["2024-01-15T10:29:55Z ERROR /api/payment 503 1450ms"]
}
```

---

## Grafana Dashboard Panels

| Panel | Description |
|---|---|
| Request Volume | Line chart, 1-min buckets |
| Error Rate % | Line + 5% threshold annotation |
| Avg & P95 Latency | Dual-line chart |
| Anomaly Score | Time series from CloudWatch custom metric |
| Anomaly Events | Timeline annotations |
| Active Alerts | Table of recent alerts |

---

## Running Tests

```bash
pytest tests/ -v --tb=short
```

---

## CI/CD

GitHub Actions runs on every push:
1. `flake8` linting
2. `black` format check
3. `pytest` unit tests
4. Docker image build validation

---

## Resume Bullets

- Built an ML-powered log anomaly detection system ingesting AWS CloudWatch logs, extracting time-series features, and scoring with Isolation Forest to detect error spikes, latency anomalies, and auth failures
- Automated alert delivery via AWS SNS (email) with cooldown logic to suppress duplicate notifications within a 10-minute window
- Deployed observability dashboards in Grafana with CloudWatch as data source, visualizing anomaly events as timeline annotations alongside request volume and latency metrics
- Containerized the pipeline with Docker Compose and enabled continuous detection via a 60-second scheduler loop
