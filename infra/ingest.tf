locals {
  openaq_key_param = "/${var.project}/openaq-api-key"
  firms_key_param  = "/${var.project}/firms-map-key"

  # Each function gets its own role: it may write only its own raw/ prefix and read only the
  # API key it uses. The OpenAQ archive is a public bucket, so reading it needs no permission.
  functions = {
    plan = {
      handler   = "plan.handler"
      timeout   = 120
      prefix    = "raw/openaq_locations/"
      key_param = local.openaq_key_param
      env       = { API_KEY_PARAM = local.openaq_key_param }
    }
    openaq = {
      handler   = "openaq.handler"
      timeout   = 300
      prefix    = "raw/openaq/"
      key_param = null
      env       = {}
    }
    firms = {
      handler   = "firms.handler"
      timeout   = 120
      prefix    = "raw/firms/"
      key_param = local.firms_key_param
      env       = { MAP_KEY_PARAM = local.firms_key_param }
    }
  }
}

data "archive_file" "ingest" {
  type        = "zip"
  source_dir  = "${path.module}/../src/ingest"
  output_path = "${path.module}/build/ingest.zip"
  excludes    = ["__pycache__", "__pycache__/*"]
}

resource "aws_iam_role" "fn" {
  for_each = local.functions

  name = "${var.project}-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "fn" {
  for_each = local.functions

  role = aws_iam_role.fn[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Effect   = "Allow"
          Action   = ["s3:PutObject"]
          Resource = ["${aws_s3_bucket.lake.arn}/${each.value.prefix}*"]
        },
        {
          Effect   = "Allow"
          Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
          Resource = ["${aws_cloudwatch_log_group.fn[each.key].arn}:*"]
        },
      ],
      each.value.key_param == null ? [] : [{
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter${each.value.key_param}"]
      }],
    )
  })
}

resource "aws_cloudwatch_log_group" "fn" {
  for_each = local.functions

  name              = "/aws/lambda/${var.project}-${each.key}"
  retention_in_days = 14
}

resource "aws_lambda_function" "fn" {
  for_each = local.functions

  function_name    = "${var.project}-${each.key}"
  role             = aws_iam_role.fn[each.key].arn
  handler          = each.value.handler
  runtime          = "python3.13"
  architectures    = ["arm64"]
  memory_size      = 512
  timeout          = each.value.timeout
  filename         = data.archive_file.ingest.output_path
  source_code_hash = data.archive_file.ingest.output_base64sha256

  environment {
    variables = merge({ LAKE_BUCKET = aws_s3_bucket.lake.id }, each.value.env)
  }

  logging_config {
    log_format            = "JSON"
    application_log_level = "INFO"
    system_log_level      = "WARN"
    log_group             = aws_cloudwatch_log_group.fn[each.key].name
  }
}
