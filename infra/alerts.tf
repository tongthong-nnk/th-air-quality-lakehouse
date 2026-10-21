resource "aws_sns_topic" "alerts" {
  name = "${var.project}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# A run that fails sends its own email. This alarm covers the runs that never start, for example
# when the schedule or its role is broken: with no run, the metric has no data at all.
resource "aws_cloudwatch_metric_alarm" "no_successful_run" {
  count = var.schedule_enabled ? 1 : 0

  alarm_name          = "${var.project}-no-successful-run"
  alarm_description   = "No pipeline run has succeeded in the last 24 hours."
  namespace           = "AWS/States"
  metric_name         = "ExecutionsSucceeded"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.pipeline.arn }
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
