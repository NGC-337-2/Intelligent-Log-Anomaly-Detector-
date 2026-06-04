terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ─── CloudWatch Log Group ─────────────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "app_logs" {
  name              = var.log_group_name
  retention_in_days = 30

  tags = {
    Project     = "log-anomaly-detector"
    Environment = var.environment
  }
}

resource "aws_cloudwatch_log_stream" "app_stream" {
  name           = var.log_stream_name
  log_group_name = aws_cloudwatch_log_group.app_logs.name
}

# ─── S3 Bucket for Models & Raw Log Exports ──────────────────────────────────
resource "aws_s3_bucket" "models" {
  bucket        = var.s3_bucket_name
  force_destroy = true

  tags = {
    Project     = "log-anomaly-detector"
    Environment = var.environment
  }
}

resource "aws_s3_bucket_versioning" "models" {
  bucket = aws_s3_bucket.models.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "models" {
  bucket = aws_s3_bucket.models.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ─── SNS Topic for Alerts ─────────────────────────────────────────────────────
resource "aws_sns_topic" "alerts" {
  name = var.sns_topic_name

  tags = {
    Project     = "log-anomaly-detector"
    Environment = var.environment
  }
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ─── IAM Role for the Detector Application ───────────────────────────────────
resource "aws_iam_role" "detector" {
  name = "log-anomaly-detector-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ec2.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Project = "log-anomaly-detector"
  }
}

resource "aws_iam_policy" "detector" {
  name        = "log-anomaly-detector-policy"
  description = "Least-privilege policy for the log anomaly detector"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogsRead"
        Effect = "Allow"
        Action = [
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
          "logs:FilterLogEvents",
          "logs:GetLogEvents",
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.app_logs.arn}:*"
      },
      {
        Sid    = "CloudWatchMetricsPut"
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData",
          "cloudwatch:GetMetricData",
          "cloudwatch:ListMetrics"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = var.metrics_namespace
          }
        }
      },
      {
        Sid    = "SNSPublish"
        Effect = "Allow"
        Action = ["sns:Publish"]
        Resource = aws_sns_topic.alerts.arn
      },
      {
        Sid    = "S3ModelAccess"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.models.arn,
          "${aws_s3_bucket.models.arn}/*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "detector" {
  role       = aws_iam_role.detector.name
  policy_arn = aws_iam_policy.detector.arn
}

# ─── CloudWatch Alarm on Anomaly Score ────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "anomaly_score" {
  alarm_name          = "log-anomaly-score-critical"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "AnomalyScore"
  namespace           = var.metrics_namespace
  period              = 60
  statistic           = "Average"
  threshold           = -0.3
  alarm_description   = "Anomaly score dropped below critical threshold"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "log-anomaly-detector"
  }
}
