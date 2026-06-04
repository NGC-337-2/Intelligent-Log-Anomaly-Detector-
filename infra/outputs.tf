output "log_group_name" {
  description = "CloudWatch Log Group name"
  value       = aws_cloudwatch_log_group.app_logs.name
}

output "log_group_arn" {
  description = "CloudWatch Log Group ARN"
  value       = aws_cloudwatch_log_group.app_logs.arn
}

output "log_stream_name" {
  description = "CloudWatch Log Stream name"
  value       = aws_cloudwatch_log_stream.app_stream.name
}

output "s3_bucket_name" {
  description = "S3 bucket name for models"
  value       = aws_s3_bucket.models.bucket
}

output "s3_bucket_arn" {
  description = "S3 bucket ARN"
  value       = aws_s3_bucket.models.arn
}

output "sns_topic_arn" {
  description = "SNS topic ARN — paste this into .env as SNS_TOPIC_ARN"
  value       = aws_sns_topic.alerts.arn
}

output "iam_role_arn" {
  description = "IAM role ARN for the detector application"
  value       = aws_iam_role.detector.arn
}

output "cloudwatch_alarm_name" {
  description = "CloudWatch alarm for critical anomaly scores"
  value       = aws_cloudwatch_metric_alarm.anomaly_score.alarm_name
}
