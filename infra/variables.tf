variable "aws_region" {
  description = "AWS region to deploy resources"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "dev"
}

variable "log_group_name" {
  description = "CloudWatch Log Group name"
  type        = string
  default     = "/log-anomaly-detector/app"
}

variable "log_stream_name" {
  description = "CloudWatch Log Stream name"
  type        = string
  default     = "app-stream"
}

variable "s3_bucket_name" {
  description = "S3 bucket for models and raw log exports"
  type        = string
  default     = "log-anomaly-detector-models"
}

variable "sns_topic_name" {
  description = "SNS topic name for anomaly alerts"
  type        = string
  default     = "log-anomaly-alerts"
}

variable "alert_email" {
  description = "Email address to receive SNS alerts (leave empty to skip)"
  type        = string
  default     = ""
}

variable "metrics_namespace" {
  description = "CloudWatch custom metrics namespace"
  type        = string
  default     = "LogAnomalyDetector"
}
