// BigQuery module: datasets, service account, Enterprise reservation, and a
// GCS staging bucket for the Estuary materialize-bigquery connector (which
// performs BigQuery Load jobs from GCS).
//
// Layout: one dataset per suite and scale factor (tpcds_sf<N>, dsb_sf<N>), a
// single-scale clickbench dataset. Results are not kept in BigQuery: every
// measurement lives in results/benchmark.duckdb.
// Engine-specific resources live here; shared cross-cloud resources (S3
// bucket, Estuary IAM user) live in terraform/modules/shared.

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.37"
    }
  }
}

variable "project_id" {
  description = "GCP Project ID hosting the BigQuery datasets and the benchmark runner service account."
  type        = string
}

variable "region" {
  description = "BigQuery + GCS region for all benchmark resources."
  type        = string
  default     = "us-central1"
}

variable "reservation_enabled" {
  description = "Whether to provision the Enterprise Edition reservation and assignment. When false, queries use on-demand billing."
  type        = bool
  default     = false
}

variable "reservation_slot_capacity" {
  description = "Enterprise Edition baseline slot capacity (100-2000, no annual commitment). Only used when reservation_enabled = true."
  type        = number
  default     = 100
}

variable "gcs_staging_bucket_name" {
  description = "Globally-unique GCS bucket name for the Estuary materialize-bigquery staging area."
  type        = string

  validation {
    condition     = length(var.gcs_staging_bucket_name) > 0
    error_message = "gcs_staging_bucket_name must be set when the BigQuery module is enabled (set bigquery_staging_bucket_name in terraform.tfvars)."
  }
}

variable "gcs_force_destroy" {
  description = "Allow Terraform to delete the GCS staging bucket on destroy even if non-empty."
  type        = bool
  default     = false
}

variable "estuary_service_account_name" {
  description = "Service account used by the Estuary materialize-bigquery connector. GCP account_id must be 6-30 chars."
  type        = string
  default     = "benchmark-estuary-bq-writer"

  validation {
    condition     = length(var.estuary_service_account_name) >= 6 && length(var.estuary_service_account_name) <= 30
    error_message = "GCP service account account_id must be between 6 and 30 characters."
  }
}

# Provider is configured at the root (terraform/main.tf) so this module can be
# instantiated with count/for_each. Don't add a nested `provider "google"`
# here — Terraform forbids it on modules with count.

# ─── Per-scale TPC-DS datasets ──────────────────────────────────────────────
locals {
  tpcds_scales = ["sf1", "sf10", "sf100", "sf1000"]
}

resource "google_bigquery_dataset" "tpcds_scale" {
  for_each      = toset(local.tpcds_scales)
  dataset_id    = "tpcds_${each.value}"
  friendly_name = "TPC-DS ${upper(each.value)}"
  description   = "TPC-DS benchmark data at ${upper(each.value)}; populated by Estuary materialize-bigquery."
  location      = var.region
  # Benchmark datasets are disposable: everything in them is re-materialised
  # by Estuary, so `terraform destroy` must be able to drop them with their
  # tables (BigQuery otherwise refuses with resourceInUse).
  delete_contents_on_destroy = true

  labels = {
    benchmark = "2026"
    scale     = each.value
    suite     = "tpcds"
  }
}

# DSB datasets follow the same per-scale convention. They're created up
# front so the Estuary materialize-bigquery target exists by the time
# `benchmark.datagen.dsb.generate_dsb_sf` produces data.
resource "google_bigquery_dataset" "dsb_scale" {
  for_each      = toset(local.tpcds_scales)
  dataset_id    = "dsb_${each.value}"
  friendly_name = "DSB ${upper(each.value)}"
  description   = "DSB benchmark data at ${upper(each.value)} (skewed TPC-DS schema)."
  location      = var.region
  # Benchmark datasets are disposable: everything in them is re-materialised
  # by Estuary, so `terraform destroy` must be able to drop them with their
  # tables (BigQuery otherwise refuses with resourceInUse).
  delete_contents_on_destroy = true

  labels = {
    benchmark = "2026"
    scale     = each.value
    suite     = "dsb"
  }
}

# Single-scale ClickBench dataset (hits ~100 GB).
resource "google_bigquery_dataset" "clickbench" {
  dataset_id    = "clickbench"
  friendly_name = "ClickBench"
  description   = "ClickBench hits dataset (single ~100 GB scale)."
  location      = var.region
  # Benchmark datasets are disposable: everything in them is re-materialised
  # by Estuary, so `terraform destroy` must be able to drop them with their
  # tables (BigQuery otherwise refuses with resourceInUse).
  delete_contents_on_destroy = true

  labels = {
    benchmark = "2026"
    suite     = "clickbench"
  }
}

# ─── Benchmark runner service account ────────────────────────────────────────
resource "google_service_account" "benchmark_runner" {
  account_id   = "benchmark-runner"
  display_name = "Benchmark Runner"
  description  = "Service account used by the benchmark harness to run BigQuery jobs."
}

resource "google_project_iam_member" "bq_admin" {
  project = var.project_id
  role    = "roles/bigquery.admin"
  member  = "serviceAccount:${google_service_account.benchmark_runner.email}"
}

resource "google_project_iam_member" "sa_user" {
  project = var.project_id
  role    = "roles/iam.serviceAccountUser"
  member  = "serviceAccount:${google_service_account.benchmark_runner.email}"
}

resource "google_service_account_key" "benchmark_runner_key" {
  service_account_id = google_service_account.benchmark_runner.name
  public_key_type    = "TYPE_X509_PEM_FILE"
}

# ─── GCS staging bucket for Estuary materialize-bigquery ────────────────────
# materialize-bigquery batches updates to GCS then issues a BigQuery Load
# job. The bucket lives in the same region as BigQuery to avoid cross-region
# egress.
resource "google_storage_bucket" "estuary_staging" {
  name                        = var.gcs_staging_bucket_name
  location                    = var.region
  force_destroy               = var.gcs_force_destroy
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = false
  }

  lifecycle_rule {
    condition {
      age = 7 # Estuary stages temp files; reap after a week.
    }
    action {
      type = "Delete"
    }
  }

  labels = {
    benchmark = "2026"
    purpose   = "estuary-bigquery-staging"
  }
}

resource "google_service_account" "estuary_bq_writer" {
  account_id   = var.estuary_service_account_name
  display_name = "Estuary materialize-bigquery writer"
  description  = "Used by the Estuary materialize-bigquery connector to load data into BigQuery."
}

# Bucket-scoped object admin on the staging bucket only (not project-wide).
resource "google_storage_bucket_iam_member" "estuary_bucket_admin" {
  bucket = google_storage_bucket.estuary_staging.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.estuary_bq_writer.email}"
}

# BigQuery write access — `bigquery.dataEditor` lets the connector load into
# any dataset in the project; pair with `bigquery.jobUser` so it can launch
# load jobs. Least-privilege relative to the harness's bigquery.admin role.
resource "google_project_iam_member" "estuary_bq_data_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.estuary_bq_writer.email}"
}

resource "google_project_iam_member" "estuary_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.estuary_bq_writer.email}"
}

resource "google_service_account_key" "estuary_bq_writer_key" {
  service_account_id = google_service_account.estuary_bq_writer.name
  public_key_type    = "TYPE_X509_PEM_FILE"
}

# ─── Enterprise Edition reservation (baseline = reservation_slot_capacity) ──
# run_plan.yaml sizes the slots per scale and `benchmark prepare-scale` writes
# them to sizing.auto.tfvars.json; slot sweeps resize it out of band
# (`benchmark bq-reservation`, `benchmark sql --compute-size <slots>`).
# ignore_idle_slots matches what the sweep sets (compute/bigquery_reservation.py):
# a query cannot borrow idle slots past the reservation, so "N slots" measures N.
resource "google_bigquery_reservation" "enterprise" {
  count             = var.reservation_enabled ? 1 : 0
  name              = "enterprise-benchmark-reservation"
  location          = var.region
  slot_capacity     = var.reservation_slot_capacity
  ignore_idle_slots = true
  edition           = "ENTERPRISE"
}

resource "google_bigquery_reservation_assignment" "default" {
  count       = var.reservation_enabled ? 1 : 0
  assignee    = "projects/${var.project_id}"
  reservation = google_bigquery_reservation.enterprise[0].id
  job_type    = "QUERY"
}

# ─── Outputs ────────────────────────────────────────────────────────────────
output "project_id" {
  description = "GCP project hosting the benchmark."
  value       = var.project_id
}

output "region" {
  description = "BigQuery region for all datasets."
  value       = var.region
}

output "tpcds_dataset_ids" {
  description = "Mapping of scale label to dataset ID for TPC-DS data."
  value = {
    for s, d in google_bigquery_dataset.tpcds_scale : s => d.dataset_id
  }
}

output "dsb_dataset_ids" {
  description = "Mapping of scale label to dataset ID for DSB data."
  value = {
    for s, d in google_bigquery_dataset.dsb_scale : s => d.dataset_id
  }
}

output "clickbench_dataset_id" {
  value = google_bigquery_dataset.clickbench.dataset_id
}

output "reservation_name" {
  value = var.reservation_enabled ? google_bigquery_reservation.enterprise[0].name : null
}

output "benchmark_runner_service_account_email" {
  value = google_service_account.benchmark_runner.email
}

output "benchmark_runner_service_account_key_json" {
  description = "Base64-encoded service account JSON for the benchmark runner. `benchmark export-terraform-outputs` decodes it to the BIGQUERY_GCP_SA_KEY path."
  value       = google_service_account_key.benchmark_runner_key.private_key
  sensitive   = true
}

output "estuary_bigquery_staging_bucket" {
  description = "GCS bucket used by Estuary materialize-bigquery as a staging area."
  value       = google_storage_bucket.estuary_staging.name
}

output "estuary_bigquery_service_account_email" {
  value = google_service_account.estuary_bq_writer.email
}

output "estuary_bigquery_service_account_key_json" {
  description = "Base64-encoded service account JSON for the Estuary materialize-bigquery connector. `benchmark export-terraform-outputs` decodes it to the ESTUARY_BIGQUERY_SERVICE_ACCOUNT_KEY path, which `flow-publish` inlines into the materialization."
  value       = google_service_account_key.estuary_bq_writer_key.private_key
  sensitive   = true
}
