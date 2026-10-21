locals {
  glue_files      = ["silver_job.py", "silver.py", "transforms.py"]
  glue_path       = "s3://${aws_s3_bucket.lake.id}/_artifacts/glue"
  glue_log_prefix = "/aws-glue/${var.project}-silver"
}

resource "aws_s3_object" "glue" {
  for_each = toset(local.glue_files)

  bucket = aws_s3_bucket.lake.id
  key    = "_artifacts/glue/${each.key}"
  source = "${path.module}/../src/glue/${each.key}"
  etag   = filemd5("${path.module}/../src/glue/${each.key}")
}

resource "aws_iam_role" "glue" {
  name = "${var.project}-glue"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "glue.amazonaws.com" }
    }]
  })
}

# Glue Data Catalog, CloudWatch logs and Data Quality results.
resource "aws_iam_role_policy_attachment" "glue_service" {
  role       = aws_iam_role.glue.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

resource "aws_iam_role_policy" "glue_lake" {
  role = aws_iam_role.glue.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.lake.arn]
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.lake.arn}/raw/openaq/*",
          "${aws_s3_bucket.lake.arn}/raw/firms/*",
          "${aws_s3_bucket.lake.arn}/_artifacts/glue/*",
        ]
      },
      {
        # Delete is for Iceberg snapshot expiry, which removes files no snapshot uses.
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = ["${aws_s3_bucket.lake.arn}/silver/*"]
      },
    ]
  })
}

# Glue 5 always streams job logs to CloudWatch. With a prefix of our own the job writes to these
# two groups, which expire, instead of the account-wide /aws-glue/jobs/* groups, which never do.
# The service role policy already allows writing to /aws-glue/*.
resource "aws_cloudwatch_log_group" "glue" {
  for_each = toset(["error", "output"])

  name              = "${local.glue_log_prefix}/${each.key}"
  retention_in_days = 14
}

resource "aws_glue_job" "silver" {
  name              = "${var.project}-silver"
  role_arn          = aws_iam_role.glue.arn
  glue_version      = "5.1"
  worker_type       = "G.1X"
  number_of_workers = 2
  # Flex runs on spare capacity at a lower price; this batch job can wait a minute to start.
  execution_class = "FLEX"
  timeout         = 30
  max_retries     = 0

  command {
    name            = "glueetl"
    python_version  = "3"
    script_location = "${local.glue_path}/silver_job.py"
  }

  execution_property {
    max_concurrent_runs = 1
  }

  default_arguments = {
    "--datalake-formats"       = "iceberg"
    "--extra-py-files"         = "${local.glue_path}/silver.py,${local.glue_path}/transforms.py"
    "--custom-logGroup-prefix" = local.glue_log_prefix
    "--lake_bucket"            = aws_s3_bucket.lake.id
    "--lake_region"            = var.region
  }

  # The log groups must exist before the first run, or Glue creates them without the retention.
  depends_on = [aws_s3_object.glue, aws_cloudwatch_log_group.glue]
}
