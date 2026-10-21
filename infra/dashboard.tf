# GitHub Actions exchanges its OIDC token for short-lived credentials of this role, so the
# repository stores no AWS keys. An account can have only one provider for this URL: if yours
# already has one, import it (docs/deploy.md).
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "dashboard" {
  name = "${var.project}-dashboard"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:ref:refs/heads/main"
        }
      }
    }]
  })
}

# Read-only: query the gold view through Athena. No access to raw/ or silver/.
resource "aws_iam_role_policy" "dashboard" {
  role = aws_iam_role.dashboard.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.athena_access, [
      {
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetTable"]
        Resource = [
          "arn:aws:glue:${var.region}:${local.account_id}:catalog",
          aws_glue_catalog_database.layer["aq_gold"].arn,
          "arn:aws:glue:${var.region}:${local.account_id}:table/aq_gold/*",
        ]
      },
      {
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = [aws_s3_bucket.lake.arn]
        Condition = { StringLike = { "s3:prefix" = ["gold/*"] } }
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${aws_s3_bucket.lake.arn}/gold/*"]
      },
    ])
  })
}
