output "lake_bucket" {
  value = aws_s3_bucket.lake.id
}

output "state_machine_arn" {
  description = "Start a run: aws stepfunctions start-execution --state-machine-arn <this>"
  value       = aws_sfn_state_machine.pipeline.arn
}

output "dashboard_role_arn" {
  description = "Set as the AWS_DASHBOARD_ROLE_ARN variable of the GitHub repository."
  value       = aws_iam_role.dashboard.arn
}

output "athena_workgroup" {
  value = aws_athena_workgroup.main.name
}
