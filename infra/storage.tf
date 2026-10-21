# One bucket for all layers, split by prefix: raw/, silver/, gold/. IAM policies grant each
# component only the prefixes it needs. force_destroy lets `terraform destroy` remove the data too.
resource "aws_s3_bucket" "lake" {
  bucket        = local.lake_bucket
  force_destroy = true
}

resource "aws_s3_bucket" "athena" {
  bucket        = "${var.project}-athena-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "all" {
  for_each = { lake = aws_s3_bucket.lake.id, athena = aws_s3_bucket.athena.id }

  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "athena" {
  bucket = aws_s3_bucket.athena.id

  rule {
    id     = "expire-query-results"
    status = "Enabled"
    filter {}

    expiration {
      days = 7
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_glue_catalog_database" "layer" {
  for_each = { aq_silver = "silver", aq_gold = "gold" }

  name         = each.key
  location_uri = "s3://${aws_s3_bucket.lake.id}/${each.value}/"
}

resource "aws_athena_workgroup" "main" {
  name          = var.project
  force_destroy = true

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    # A query that would scan more than 1 GB is cancelled; every query in this project scans MBs.
    bytes_scanned_cutoff_per_query = 1073741824

    result_configuration {
      output_location = "s3://${aws_s3_bucket.athena.id}/results/"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }
}

locals {
  # What a role needs to run a query in the workgroup and read its result.
  athena_access = [
    {
      Effect = "Allow"
      Action = [
        "athena:StartQueryExecution",
        "athena:GetQueryExecution",
        "athena:GetQueryResults",
        "athena:StopQueryExecution",
      ]
      Resource = [aws_athena_workgroup.main.arn]
    },
    {
      Effect   = "Allow"
      Action   = ["s3:GetBucketLocation", "s3:ListBucket"]
      Resource = [aws_s3_bucket.athena.arn]
    },
    {
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
      Resource = ["${aws_s3_bucket.athena.arn}/results/*"]
    },
  ]
}
