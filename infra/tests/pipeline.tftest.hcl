# Runs offline with mocked providers: `terraform init -backend=false && terraform test`.
# Mocked ARNs only need the right shape, because the provider checks ARN arguments.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_sfn_state_machine" {
    defaults = { arn = "arn:aws:states:ap-southeast-1:123456789012:stateMachine:mock" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:ap-southeast-1:123456789012:mock" }
  }
}

mock_provider "archive" {}

variables {
  alert_email = "alerts@example.com"
  github_repo = "octo-org/th-air-quality-lakehouse"
}

run "schedule_and_alarm_are_off_by_default" {
  command = plan

  assert {
    condition     = aws_scheduler_schedule.daily.state == "DISABLED"
    error_message = "A new deployment must not start daily runs before the first manual run."
  }

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.no_successful_run) == 0
    error_message = "Without a schedule there are no runs, so the alarm would always fire."
  }
}

run "schedule_on" {
  command = plan

  variables {
    schedule_enabled = true
  }

  assert {
    condition     = aws_scheduler_schedule.daily.state == "ENABLED"
    error_message = "schedule_enabled = true must enable the schedule."
  }

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.no_successful_run) == 1
    error_message = "A scheduled pipeline needs the missed-run alarm."
  }
}

run "state_machine" {
  override_resource {
    target = aws_s3_bucket.lake
    values = { id = "thaq-lake-123456789012" }
  }

  assert {
    condition = alltrue([
      for name, state in jsondecode(aws_sfn_state_machine.pipeline.definition).States :
      try(state.Catch[0].Next, "") == "NotifyFailure"
      if contains(["Task", "Map"], state.Type) && !startswith(name, "Notify")
    ])
    error_message = "Every Task and Map state must send its errors to NotifyFailure, or a failure sends no email."
  }

  assert {
    condition = [
      for sql in jsondecode(aws_sfn_state_machine.pipeline.definition).States.TableStatements.Result :
      regex("^CREATE (?:EXTERNAL TABLE|TABLE|OR REPLACE VIEW) (?:IF NOT EXISTS )?(\\S+)", sql)[0]
      ] == [
      "aq_silver.locations", "aq_gold.dim_station", "aq_gold.fct_station_day", "aq_gold.v_station_day",
    ]
    error_message = "The DDL must run in file order, with the view after the tables it reads."
  }

  assert {
    condition = strcontains(
      jsondecode(aws_sfn_state_machine.pipeline.definition).States.TableStatements.Result[2],
      "LOCATION 's3://thaq-lake-123456789012/gold/fct_station_day/'",
    )
    error_message = "The DDL templates must be rendered with the lake bucket."
  }
}

run "dashboard_role" {
  assert {
    condition = jsondecode(aws_iam_role.dashboard.assume_role_policy).Statement[0].Condition == {
      StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:octo-org/th-air-quality-lakehouse:ref:refs/heads/main"
      }
    }
    error_message = "Only workflows on the main branch of this repository may assume the dashboard role."
  }
}

run "github_repo_is_checked" {
  command = plan

  variables {
    github_repo = "https://github.com/octo-org/th-air-quality-lakehouse"
  }

  expect_failures = [var.github_repo]
}
