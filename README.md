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

## Quick Start

### 1. Prerequisites
- Python 3.11+
- Docker & Docker Compose
- AWS CLI configured (`aws configure`)
- Terraform (for infrastructure setup)

### 2. Clone & Configure
```bash
git clone <repo-url>
cd log-anomaly-detector
cp .env.example .env
# Fill in your AWS credentials and resource ARNs in .env
```

### 3. Provision AWS Infrastructure
```bash
cd infra/
terraform init
terraform plan
terraform apply
# Copy outputs (SNS_TOPIC_ARN, etc.) into .env
```

### 4. Install Python Dependencies
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 5. Generate Training Data & Train Model
```bash
# Generate 7 days of normal synthetic logs → CloudWatch + SQLite
python simulate_logs.py --days 7 --mode normal

# Train Isolation Forest on the synthetic data
python train.py
```

### 6. Run the Full Pipeline (Single Pass)
```bash
# Inject anomalies + run detection once
python simulate_logs.py --days 1 --mode anomaly
python main.py --once
```

### 7. Run Continuously (60s loop)
```bash
python main.py
```

### 8. Launch Grafana Dashboard
```bash
docker compose up -d
# Open http://localhost:3000 (admin / admin)
# Dashboard: "Log Anomaly Detector"
```

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
