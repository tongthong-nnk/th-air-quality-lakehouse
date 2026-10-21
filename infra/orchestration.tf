locals {
  # sql/ddl in file order: the view comes last because it reads the tables. IF NOT EXISTS and
  # OR REPLACE make the statements safe to run on every run, so a new table or a changed view
  # reaches Athena with the next run.
  table_statements = [
    for file in sort(fileset("${path.module}/../sql/ddl", "*.sql")) :
    templatefile("${path.module}/../sql/ddl/${file}", { lake_bucket = aws_s3_bucket.lake.id })
  ]

  # Every MERGE adds files and a snapshot. OPTIMIZE rewrites small files into larger ones, and
  # VACUUM removes snapshots older than the 7 days set in the DDL, with the files only they used.
  maintenance_statements = flatten([
    for table in ["aq_gold.dim_station", "aq_gold.fct_station_day"] :
    ["OPTIMIZE ${table} REWRITE DATA USING BIN_PACK", "VACUUM ${table}"]
  ])

  execution_url = "https://${var.region}.console.aws.amazon.com/states/home?region=${var.region}#/v2/executions/details/"
  athena_task   = "arn:aws:states:::athena:startQueryExecution.sync"

  catch = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "NotifyFailure" }]

  # A Lambda that runs again writes the same S3 object, so any error is safe to retry.
  lambda_retry = [{
    ErrorEquals     = ["States.ALL"]
    IntervalSeconds = 10
    MaxAttempts     = 3
    BackoffRate     = 3
    JitterStrategy  = "FULL"
  }]

  # Two Map states run the SQL statements in $.statements one at a time, in order, and replace
  # them with the IDs of the Athena queries that ran them. Each key is the Map state's name and
  # each value the name of the state inside it: state names must be unique in the whole machine.
  statement_maps = {
    for map, step in { CreateTables = "CreateTable", MaintainGold = "MaintainTable" } :
    map => {
      Type           = "Map"
      ItemsPath      = "$.statements"
      MaxConcurrency = 1
      ItemSelector   = { "sql.$" = "$$.Map.Item.Value" }
      ItemProcessor = {
        ProcessorConfig = { Mode = "INLINE" }
        StartAt         = step
        States = {
          (step) = {
            Type     = "Task"
            Resource = local.athena_task
            Parameters = {
              "QueryString.$" = "$.sql"
              WorkGroup       = aws_athena_workgroup.main.name
            }
            ResultSelector = { "QueryExecutionId.$" = "$.QueryExecution.QueryExecutionId" }
            End            = true
          }
        }
      }
      ResultPath = "$.statements"
      Catch      = local.catch
    }
  }
}

resource "aws_sfn_state_machine" "pipeline" {
  name     = "${var.project}-pipeline"
  role_arn = aws_iam_role.pipeline.arn

  definition = jsonencode({
    Comment        = "Loads the days R-7..R-4. Start it with start_date and end_date to load up to 366 past days."
    StartAt        = "Plan"
    TimeoutSeconds = 7200
    States = {
      Plan = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.fn["plan"].arn
          "Payload.$"  = "$"
        }
        ResultSelector = {
          "dates.$"        = "$.Payload.dates"
          "location_ids.$" = "$.Payload.location_ids"
          "start_date.$"   = "$.Payload.start_date"
          "end_date.$"     = "$.Payload.end_date"
        }
        ResultPath = "$.plan"
        Retry      = local.lambda_retry
        Catch      = local.catch
        Next       = "IngestDays"
      }

      IngestDays = {
        Type      = "Map"
        ItemsPath = "$.plan.dates"
        # Two Lambdas per day, so at most 8 at once: new accounts can have a concurrency limit of 10.
        MaxConcurrency = 4
        ItemSelector = {
          "date.$"         = "$$.Map.Item.Value"
          "location_ids.$" = "$.plan.location_ids"
        }
        ItemProcessor = {
          ProcessorConfig = { Mode = "INLINE" }
          StartAt         = "IngestDay"
          States = {
            IngestDay = {
              Type = "Parallel"
              Branches = [
                {
                  StartAt = "OpenAQ"
                  States = {
                    OpenAQ = {
                      Type     = "Task"
                      Resource = "arn:aws:states:::lambda:invoke"
                      Parameters = {
                        FunctionName = aws_lambda_function.fn["openaq"].arn
                        Payload      = { "date.$" = "$.date", "location_ids.$" = "$.location_ids" }
                      }
                      OutputPath = "$.Payload"
                      Retry      = local.lambda_retry
                      End        = true
                    }
                  }
                },
                {
                  StartAt = "FIRMS"
                  States = {
                    FIRMS = {
                      Type     = "Task"
                      Resource = "arn:aws:states:::lambda:invoke"
                      Parameters = {
                        FunctionName = aws_lambda_function.fn["firms"].arn
                        Payload      = { "date.$" = "$.date" }
                      }
                      OutputPath = "$.Payload"
                      Retry      = local.lambda_retry
                      End        = true
                    }
                  }
                },
              ]
              End = true
            }
          }
        }
        ResultPath = null
        Catch      = local.catch
        Next       = "Silver"
      }

      Silver = {
        Type     = "Task"
        Resource = "arn:aws:states:::glue:startJobRun.sync"
        Parameters = {
          JobName = aws_glue_job.silver.name
          Arguments = {
            "--start_date.$" = "$.plan.start_date"
            "--end_date.$"   = "$.plan.end_date"
          }
        }
        ResultPath = null
        Catch      = local.catch
        Next       = "TableStatements"
      }

      TableStatements = {
        Type       = "Pass"
        Result     = local.table_statements
        ResultPath = "$.statements"
        Next       = "CreateTables"
      }
      CreateTables = merge(local.statement_maps["CreateTables"], { Next = "BuildDimStation" })

      BuildDimStation = {
        Type     = "Task"
        Resource = local.athena_task
        Parameters = {
          QueryString = file("${path.module}/../sql/gold/dim_station.sql")
          WorkGroup   = aws_athena_workgroup.main.name
        }
        ResultPath = null
        Catch      = local.catch
        Next       = "BuildFactStationDay"
      }

      BuildFactStationDay = {
        Type     = "Task"
        Resource = local.athena_task
        Parameters = {
          QueryString = file("${path.module}/../sql/gold/fct_station_day.sql")
          WorkGroup   = aws_athena_workgroup.main.name
        }
        ResultPath = null
        Catch      = local.catch
        Next       = "CheckGold"
      }

      CheckGold = {
        Type     = "Task"
        Resource = local.athena_task
        Parameters = {
          QueryString = file("${path.module}/../sql/gold/checks.sql")
          WorkGroup   = aws_athena_workgroup.main.name
        }
        ResultSelector = { "QueryExecutionId.$" = "$.QueryExecution.QueryExecutionId" }
        ResultPath     = "$.checks"
        Catch          = local.catch
        Next           = "ReadChecks"
      }

      ReadChecks = {
        Type       = "Task"
        Resource   = "arn:aws:states:::athena:getQueryResults"
        Parameters = { "QueryExecutionId.$" = "$.checks.QueryExecutionId" }
        # Row 0 holds the column names; row 1 is the one row checks.sql returns.
        ResultSelector = {
          "failed.$"  = "States.StringToJson($.ResultSet.Rows[1].Data[0].VarCharValue)"
          "details.$" = "$.ResultSet.Rows[1].Data[1].VarCharValue"
        }
        ResultPath = "$.checks"
        Catch      = local.catch
        Next       = "ChecksPassed"
      }

      ChecksPassed = {
        Type    = "Choice"
        Choices = [{ Variable = "$.checks.failed", NumericEquals = 0, Next = "MaintenanceStatements" }]
        Default = "NotifyChecksFailed"
      }

      NotifyChecksFailed = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.alerts.arn
          Subject     = "${var.project}: gold checks failed"
          "Message.$" = "States.Format('Gold checks after loading {} to {} found {} problem(s): {}. Execution: ${local.execution_url}{}', $.plan.start_date, $.plan.end_date, $.checks.failed, $.checks.details, $$.Execution.Id)"
        }
        ResultPath = null
        Next       = "ChecksFailed"
      }

      ChecksFailed = {
        Type      = "Fail"
        Error     = "GoldChecksFailed"
        CausePath = "$.checks.details"
      }

      MaintenanceStatements = {
        Type       = "Pass"
        Result     = local.maintenance_statements
        ResultPath = "$.statements"
        Next       = "MaintainGold"
      }
      MaintainGold = merge(local.statement_maps["MaintainGold"], { End = true })

      NotifyFailure = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.alerts.arn
          Subject     = "${var.project}: pipeline run failed"
          "Message.$" = "States.Format('Run {} failed. Execution: ${local.execution_url}{} Error: {}', $$.Execution.Name, $$.Execution.Id, States.JsonToString($.error))"
        }
        ResultPath = null
        Next       = "Failed"
      }

      Failed = {
        Type      = "Fail"
        ErrorPath = "$.error.Error"
        CausePath = "$.error.Cause"
      }
    }
  })
}

resource "aws_iam_role" "pipeline" {
  name = "${var.project}-pipeline"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "states.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "pipeline" {
  role = aws_iam_role.pipeline.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.athena_access, [
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = flatten([for fn in aws_lambda_function.fn : [fn.arn, "${fn.arn}:*"]])
      },
      {
        Effect   = "Allow"
        Action   = ["glue:StartJobRun", "glue:GetJobRun", "glue:GetJobRuns", "glue:BatchStopJobRun"]
        Resource = [aws_glue_job.silver.arn]
      },
      {
        # Step Functions' Athena integration looks up the workgroup and the catalog.
        Effect = "Allow"
        Action = ["athena:GetWorkGroup", "athena:GetDataCatalog"]
        Resource = [
          aws_athena_workgroup.main.arn,
          "arn:aws:athena:${var.region}:${local.account_id}:datacatalog/AwsDataCatalog",
        ]
      },
      {
        # The DDL creates tables, and every MERGE, OPTIMIZE and VACUUM points a gold table at
        # a new Iceberg metadata file.
        Effect = "Allow"
        Action = [
          "glue:GetDatabase",
          "glue:GetTable",
          "glue:GetTables",
          "glue:GetPartitions",
          "glue:CreateTable",
          "glue:UpdateTable",
        ]
        Resource = [
          "arn:aws:glue:${var.region}:${local.account_id}:catalog",
          aws_glue_catalog_database.layer["aq_silver"].arn,
          aws_glue_catalog_database.layer["aq_gold"].arn,
          "arn:aws:glue:${var.region}:${local.account_id}:table/aq_silver/*",
          "arn:aws:glue:${var.region}:${local.account_id}:table/aq_gold/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation", "s3:ListBucket"]
        Resource = [aws_s3_bucket.lake.arn]
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.lake.arn}/raw/openaq_locations/*",
          "${aws_s3_bucket.lake.arn}/silver/*",
        ]
      },
      {
        # Delete is for VACUUM.
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = ["${aws_s3_bucket.lake.arn}/gold/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = [aws_sns_topic.alerts.arn]
      },
    ])
  })
}

# The window is R-7..R-4, so the time of day does not change what a run loads. 09:30 puts
# the alert emails in working hours.
resource "aws_scheduler_schedule" "daily" {
  name                         = "${var.project}-daily"
  state                        = var.schedule_enabled ? "ENABLED" : "DISABLED"
  schedule_expression          = "cron(30 9 * * ? *)"
  schedule_expression_timezone = "Asia/Bangkok"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sfn_state_machine.pipeline.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({})
  }
}

resource "aws_iam_role" "scheduler" {
  name = "${var.project}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "scheduler.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["states:StartExecution"]
      Resource = [aws_sfn_state_machine.pipeline.arn]
    }]
  })
}
