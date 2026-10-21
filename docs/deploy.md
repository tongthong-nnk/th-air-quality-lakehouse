# Deploy to AWS

Terraform in `infra/` creates the whole pipeline in ap-southeast-1. It stays within the AWS Free
plan; the [cost estimate](../README.md#cost) is in the README. The daily schedule is off until
step 8.

## What you need

- An AWS account and an IAM user with administrator rights, signed in to the AWS CLI v2.
- Terraform 1.11 or later.
- A free OpenAQ API key: sign up at https://explore.openaq.org/register, then copy the key from
  https://explore.openaq.org/account.
- A free NASA FIRMS MAP_KEY from https://firms.modaps.eosdis.nasa.gov/api/map_key/.
- This repository on GitHub, with Actions enabled.

## 1. Publish the dashboard

In the repository settings, open Pages and set the source to GitHub Actions. Then run the
Dashboard workflow from the Actions tab. Until step 7 the page shows the data committed in
`dashboard/data/`.

## 2. Credentials for Terraform

Terraform's AWS provider cannot read an `aws login` session yet. This function hands it the
session's current credentials for each command, without leaving them in your shell:

```bash
aws login
export AWS_REGION=ap-southeast-1
tf() ( eval "$(aws configure export-credentials --format env)" && terraform "$@" )
```

With access keys or an IAM Identity Center profile, plain `terraform` works too.

## 3. State bucket

Created once, outside Terraform, because Terraform keeps its state in it. Versioning keeps every
earlier state, so a bad apply can be undone.

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
STATE_BUCKET="thaq-tfstate-$ACCOUNT_ID"
aws s3api create-bucket --bucket "$STATE_BUCKET" \
  --create-bucket-configuration LocationConstraint=ap-southeast-1
aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" \
  --versioning-configuration Status=Enabled
```

## 4. API keys

The keys go to SSM Parameter Store as SecureString parameters. They are created here, not in
Terraform, so they never reach the state file. `read -rs` does not echo the key or keep it in the
shell history.

```bash
printf "OpenAQ API key: " && read -rs KEY && echo
aws ssm put-parameter --name /thaq/openaq-api-key --type SecureString --value "$KEY"
printf "FIRMS MAP_KEY: " && read -rs KEY && echo
aws ssm put-parameter --name /thaq/firms-map-key --type SecureString --value "$KEY"
unset KEY
```

To replace a key later, run the same command with `--overwrite`.

## 5. Create the stack

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars    # set alert_email and github_repo
tf init -backend-config="bucket=$STATE_BUCKET"
tf apply
```

An account can have only one OIDC provider for GitHub. If the apply stops with
`EntityAlreadyExists` for `aws_iam_openid_connect_provider.github`, import the one you have and
apply again:

```bash
tf import aws_iam_openid_connect_provider.github \
  "arn:aws:iam::$ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
tf apply
```

AWS then emails `alert_email` a confirmation link for the alerts. Open it, or no alert arrives.

## 6. First run

```bash
SFN=$(tf output -raw state_machine_arn)
aws stepfunctions start-execution --state-machine-arn "$SFN"
```

With no input, the run loads the days R-7 to R-4. Follow it in the Step Functions console. The
Silver step can wait a few minutes before the Glue job starts, because Flex runs on spare capacity.

## 7. Backfill

Each run loads at most 366 days, and only one run may go at a time: the Glue job allows a single
run, and two runs would write to the same tables. This function starts a run and waits for it:

```bash
run_window() {
  arn=$(aws stepfunctions start-execution --state-machine-arn "$SFN" \
    --input "{\"start_date\": \"$1\", \"end_date\": \"$2\"}" \
    --query executionArn --output text) || return 1
  while status=$(aws stepfunctions describe-execution --execution-arn "$arn" \
      --query status --output text) && [ "$status" = RUNNING ]; do
    sleep 60
  done
  echo "$1 to $2: $status"
  [ "$status" = SUCCEEDED ]
}

run_window 2021-06-01 2022-05-31 &&
  run_window 2022-06-01 2023-05-31 &&
  run_window 2023-06-01 2024-05-31 &&
  run_window 2024-06-01 2025-05-31 &&
  run_window 2025-06-01 2026-05-31 &&
  run_window 2026-06-01 2026-10-17    # end four days before today
```

The numbers for the README: the duration of each execution in the Step Functions console, DPU
hours on each Glue job run, and data scanned in the Athena query history (workgroup `thaq`).

## 8. Daily export and schedule

Give the Dashboard workflow its role. In the repository settings, open Secrets and variables,
Actions, Variables, and add `AWS_DASHBOARD_ROLE_ARN` with the value of:

```bash
tf output -raw dashboard_role_arn
```

Run the Dashboard workflow once by hand. From then on it runs every day at 11:30 Bangkok time,
two hours after the pipeline. It uses the workgroup `thaq` and region ap-southeast-1, the
Terraform defaults.

Then set `schedule_enabled = true` in `terraform.tfvars` and run `tf apply`. This starts the daily
run at 09:30 Bangkok time and adds an alarm that emails when a whole day passes without a
successful run. The alarm can send one email on its first day, before a scheduled run has
finished. A scheduled run loads only R-7 to R-4, so if more than a few days have passed since the
backfill, load the days in between with `run_window` first.

## Remove everything

1. To keep the dashboard, commit its last export and delete the `AWS_DASHBOARD_ROLE_ARN`
   variable, so the workflow stops querying AWS:

   ```bash
   curl -fsSo dashboard/data/dashboard.json \
     https://tongthong-nnk.github.io/th-air-quality-lakehouse/data/dashboard.json
   git commit -am "Freeze dashboard data" && git push
   ```

2. If you imported an existing OIDC provider in step 5, keep it out of the destroy:
   `tf state rm aws_iam_openid_connect_provider.github`.
3. Delete the stack, including the lake bucket and its data: `tf destroy`.
4. Delete the API keys:
   `aws ssm delete-parameters --names /thaq/openaq-api-key /thaq/firms-map-key`.
5. Delete the state bucket in the S3 console with Empty, then Delete. It is versioned, so
   `aws s3 rb --force` would leave the old versions behind.
