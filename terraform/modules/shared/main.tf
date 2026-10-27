// Shared (non-engine-specific) infrastructure for the 2026 benchmark.
//
// Provisions the resources every engine pipeline depends on:
//   - S3 staging bucket for TPC-DS / DSB / ClickBench Parquet
//   - Estuary IAM user + access keys (least-privilege read on the bucket)
//
// Engine-specific resources (Snowflake warehouse, Databricks SQL endpoint,
// BigQuery reservation, Redshift Serverless namespace, Fabric workspace,
// ClickHouse Cloud service record, MotherDuck config) live under their own modules.

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.51"
    }
  }
}

variable "bucket_name" {
  description = "Name for the S3 staging bucket (must be globally unique)."
  type        = string
}

variable "region" {
  description = "AWS region for the bucket. Defaults to us-east-1."
  type        = string
  default     = "us-east-1"
}

variable "force_destroy" {
  description = "Allow Terraform to delete a non-empty bucket on destroy."
  type        = bool
  default     = false
}

variable "estuary_iam_user_name" {
  description = "IAM user the Estuary S3 source assumes for reads."
  type        = string
  default     = "benchmark-2026-estuary-reader"
}

resource "aws_s3_bucket" "staging" {
  bucket        = var.bucket_name
  force_destroy = var.force_destroy

  tags = {
    Project = "benchmark-2026"
    Purpose = "tpcds-dsb-clickbench-staging"
  }
}

resource "aws_s3_bucket_versioning" "staging" {
  bucket = aws_s3_bucket.staging.id
  versioning_configuration {
    status = "Disabled"
  }
}

resource "aws_s3_bucket_public_access_block" "staging" {
  bucket                  = aws_s3_bucket.staging.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "staging" {
  bucket = aws_s3_bucket.staging.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_iam_user" "estuary_reader" {
  name = var.estuary_iam_user_name
  tags = {
    Project = "benchmark-2026"
    Purpose = "estuary-s3-source"
  }
}

resource "aws_iam_user_policy" "estuary_reader" {
  name = "${var.estuary_iam_user_name}-s3-read"
  user = aws_iam_user.estuary_reader.name

  # Read access on the entire bucket — captures (source-s3 connector) need
  # GetObject + ListBucket on data/*. Plus write access under
  # `estuary-staging/` so the materialize-motherduck and materialize-redshift
  # connectors can stage intermediate files there (BigQuery stages in GCS). The bucket is split:
  #   data/<suite>/sf*/    — read-only, populated by `benchmark stage-data`
  #   estuary-staging/...  — read+write, populated/consumed by materializers
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadStagedData"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket",
          "s3:GetBucketLocation",
        ]
        Resource = [
          aws_s3_bucket.staging.arn,
          "${aws_s3_bucket.staging.arn}/*",
        ]
      },
      {
        Sid    = "WriteMaterializerStaging"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:AbortMultipartUpload",
        ]
        Resource = [
          "${aws_s3_bucket.staging.arn}/estuary-staging/*",
        ]
      },
    ]
  })
}

resource "aws_iam_access_key" "estuary_reader" {
  user = aws_iam_user.estuary_reader.name
}

output "bucket_name" {
  description = "Name of the staging bucket."
  value       = aws_s3_bucket.staging.id
}

output "bucket_arn" {
  description = "ARN of the staging bucket."
  value       = aws_s3_bucket.staging.arn
}

output "region" {
  description = "Region the bucket lives in."
  value       = var.region
}

output "estuary_iam_user_name" {
  description = "Estuary reader IAM user name."
  value       = aws_iam_user.estuary_reader.name
}

output "estuary_access_key_id" {
  description = "Access key id for the Estuary reader user."
  value       = aws_iam_access_key.estuary_reader.id
  sensitive   = true
}

output "estuary_secret_access_key" {
  description = "Secret access key for the Estuary reader user."
  value       = aws_iam_access_key.estuary_reader.secret
  sensitive   = true
}
