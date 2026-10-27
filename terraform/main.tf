// Root Terraform stack for the 2026 benchmark.
//
// Every engine module is opt-in: provide credentials/IDs for the platforms
// you want to provision, leave the rest blank. The shared module (S3 staging
// bucket + Estuary IAM user) is always required.

terraform {
  required_version = ">= 1.6.0"

  # The state bucket is supplied at init time:
  #   terraform init -backend-config="bucket=<your-state-bucket>"
  backend "gcs" {
    prefix = "terraform/state"
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.51"
    }
    google = {
      source  = "hashicorp/google"
      version = "~> 7.37"
    }
    snowflake = {
      source  = "snowflakedb/snowflake"
      version = "~> 2.17"
    }
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.118"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.78"
    }
    azapi = {
      source  = "azure/azapi"
      version = "~> 2.10"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.9"
    }
    fabric = {
      source  = "microsoft/fabric"
      version = "~> 1.11"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile
}

provider "google" {
  project = var.gcp_project_id != "" ? var.gcp_project_id : null
  region  = var.gcp_region
}

provider "snowflake" {
  # snowflakedb/snowflake v2 deprecated `account` + `region` in favor of
  # `organization_name` + `account_name`. Defaults are placeholder strings
  # so the provider's eager credential check succeeds even when the module
  # is disabled (count = 0); the placeholder is never used at apply time.
  organization_name = var.snowflake_organization != "" ? var.snowflake_organization : "DISABLED"
  account_name      = var.snowflake_account != "" ? var.snowflake_account : "disabled"
  user              = var.snowflake_admin_user != "" ? var.snowflake_admin_user : "disabled"
  # Key-pair (JWT) auth — same method the module puts on BENCHMARK_USER.
  # Bypasses MFA like a PAT, but doesn't expire on a clock and needs no
  # network policy. The file() guard is required because file() raises a
  # filesystem error on an empty/placeholder path; when the module is enabled,
  # a missing or invalid key still fails loudly at connect time (fail-fast).
  authenticator = "SNOWFLAKE_JWT"
  private_key = (
    var.snowflake_admin_private_key_path != ""
    ? file(var.snowflake_admin_private_key_path)
    : "disabled"
  )
  # Pin the bootstrap role explicitly — never inherit it from the credential's
  # default. This module CREATES BENCHMARK_ROLE, so it must connect as a role
  # that already exists and can create roles.
  role = "ACCOUNTADMIN"
}

# Azure bring-up chicken-and-egg: no PAT can exist before the workspace does,
# so when a host is set WITHOUT a token the provider falls back to the
# operator's `az login` (mirrors the fabric provider's use_cli). Once the
# module has minted the harness PAT you may put it in databricks_token, but
# staying on azure-cli auth is fine too. Placeholders keep the provider's
# eager credential check happy while the module is disabled (count = 0).
locals {
  databricks_auth_azure_cli = var.databricks_host != "" && var.databricks_token == ""
}

provider "databricks" {
  host      = var.databricks_host != "" ? var.databricks_host : "https://disabled.cloud.databricks.com"
  token     = local.databricks_auth_azure_cli ? null : (var.databricks_token != "" ? var.databricks_token : "disabled")
  auth_type = local.databricks_auth_azure_cli ? "azure-cli" : "pat"
}

# Shared Azure control plane — the fabric AND databricks_azure modules both
# provision through this provider (same tenant/subscription vars).
provider "azurerm" {
  features {}
  tenant_id       = var.fabric_tenant_id
  subscription_id = var.fabric_subscription_id
}

provider "azapi" {
  tenant_id       = var.fabric_tenant_id
  subscription_id = var.fabric_subscription_id
}

provider "azuread" {
  tenant_id = var.fabric_tenant_id
}

# Fabric control-plane provider (workspace / warehouse items).
# Authenticates off the operator's `az login` (use_cli).
provider "fabric" {
  use_cli   = true
  tenant_id = var.fabric_tenant_id
}

# ─── Shared: S3 staging bucket + Estuary IAM user (always on) ───────────────
module "shared" {
  source = "./modules/shared"

  bucket_name   = var.staging_bucket_name
  region        = var.aws_region
  force_destroy = var.staging_force_destroy
}

# ─── BigQuery (opt-in via gcp_project_id) ────────────────────────────────────
module "bigquery" {
  source = "./modules/bigquery"
  count  = var.gcp_project_id != "" ? 1 : 0

  project_id                = var.gcp_project_id
  region                    = var.gcp_region
  reservation_enabled       = var.bigquery_reservation_enabled
  reservation_slot_capacity = var.bigquery_reservation_slot_capacity
  gcs_staging_bucket_name   = var.bigquery_staging_bucket_name
  gcs_force_destroy         = var.bigquery_staging_force_destroy
}

# ─── Snowflake (opt-in via snowflake_account) ────────────────────────────────
module "snowflake" {
  source = "./modules/snowflake"
  count  = var.snowflake_account != "" ? 1 : 0

  account                       = var.snowflake_account
  admin_user                    = var.snowflake_admin_user
  region                        = var.snowflake_region
  benchmark_user_network_policy = var.snowflake_benchmark_user_network_policy
  estuary_egress_cidrs          = var.snowflake_estuary_egress_cidrs
  benchmark_access_cidrs        = var.snowflake_benchmark_access_cidrs
  benchmark_user_rsa_public_key = var.snowflake_benchmark_user_rsa_public_key
}

# ─── Azure Databricks workspace (opt-in via databricks_azure_enabled) ────────
# ARM resources only (workspace + UC access connector + catalog storage);
# uses the shared azurerm provider (fabric_subscription_id/fabric_tenant_id).
# Two-stage bring-up: `terraform apply -target='module.databricks_azure'`
# first, then set databricks_host from the workspace_url output and re-apply
# so module.databricks can fill the workspace — see terraform/README.md.
module "databricks_azure" {
  source = "./modules/databricks_azure"
  count  = var.databricks_azure_enabled ? 1 : 0

  location = var.databricks_azure_location
}

# ─── Databricks workspace contents (opt-in via databricks_host) ──────────────
module "databricks" {
  source = "./modules/databricks"
  count  = var.databricks_host != "" ? 1 : 0

  host           = var.databricks_host
  token          = var.databricks_token
  warehouse_size = var.databricks_warehouse_size

  # Azure-only UC wiring; all no-ops on an AWS workspace or when
  # databricks_azure is disabled.
  azure_unity_catalog       = var.databricks_azure_enabled
  azure_access_connector_id = length(module.databricks_azure) > 0 ? module.databricks_azure[0].access_connector_id : ""
  storage_root              = length(module.databricks_azure) > 0 ? module.databricks_azure[0].catalog_storage_root : ""
  # No PAT in tfvars means the provider is on azure-cli auth — mint the PAT
  # the harness + Estuary need.
  create_harness_pat = local.databricks_auth_azure_cli
}

# ─── Redshift Serverless (opt-in via redshift_enabled) ──────────────────────
# The module generates a random admin password by default; pull it from
# `terraform output -raw redshift_admin_password` after apply. Set
# `redshift_admin_password_override` only if you need a specific value.
module "redshift" {
  source = "./modules/redshift"
  count  = var.redshift_enabled ? 1 : 0

  region                  = var.aws_region
  admin_password_override = var.redshift_admin_password_override
  baseline_rpu            = var.redshift_baseline_rpu
  # Pin max == base: benchmarks must run at a fixed size — Serverless vertical
  # autoscaling toward a higher max would add scaling noise to every pass.
  # Loads that want a temporary higher ceiling set it out-of-band via
  # scripts/resize/redshift_resize.py --max-rpu (terraform re-pins on next apply).
  max_rpu            = var.redshift_baseline_rpu
  staging_bucket_arn = module.shared.bucket_arn
}

# ─── Microsoft Fabric (opt-in via fabric_subscription_id + fabric_enabled) ───
# fabric_enabled (default true) exists so the shared Azure subscription can be
# set for databricks_azure without dragging the Fabric capacity in.
module "fabric" {
  source = "./modules/fabric"
  count  = var.fabric_subscription_id != "" && var.fabric_enabled ? 1 : 0

  # Subscription/tenant targeting is on the root azurerm/azapi/azuread/fabric
  # provider blocks (they read var.fabric_subscription_id + var.fabric_tenant_id);
  # the module only needs tenant_id for its passthrough output.
  tenant_id              = var.fabric_tenant_id
  location               = var.fabric_location
  capacity_sku           = var.fabric_capacity_sku
  capacity_admin_members = var.fabric_capacity_admin_members
}

# ─── ClickHouse Cloud (opt-in via clickhouse_service_id) ────────────────────
# No-op module (see modules/clickhouse/main.tf). The "Estuary Benchmark"
# service is pre-provisioned out-of-band and managed in the Cloud UI; the
# provider doesn't expose a service data source, and importing the resource
# would put it under terraform's destroy authority. So this module just
# records the service UUID for state symmetry. Copy host/port/password/API
# key from the Cloud UI into env/clickhouse.env directly.
module "clickhouse" {
  source = "./modules/clickhouse"
  count  = var.clickhouse_service_id != "" ? 1 : 0

  service_id = var.clickhouse_service_id
}

# ─── MotherDuck (always on; manages config only, not credentials) ────────────
module "motherduck" {
  source = "./modules/motherduck"

  database_name = var.motherduck_database
  tier          = var.motherduck_tier
}

# ─── Datagen EC2 (opt-in via datagen_enabled; ephemeral) ─────────────────────
# On-demand box for TPC-DS/DSB generation + in-region S3 staging at SF100+.
# Stateless — flip datagen_enabled off and re-apply to stop paying for it.
module "datagen" {
  source = "./modules/datagen"
  count  = var.datagen_enabled ? 1 : 0

  region              = var.aws_region
  instance_type       = var.datagen_instance_type
  data_volume_gb      = var.datagen_data_volume_gb
  ssh_public_key      = var.datagen_ssh_public_key
  allowed_ssh_cidr    = var.datagen_allowed_ssh_cidr
  staging_bucket_arn  = module.shared.bucket_arn
  staging_bucket_name = module.shared.bucket_name
}

# ─── Aggregated outputs ──────────────────────────────────────────────────────
output "staging_bucket_name" {
  value = module.shared.bucket_name
}

output "datagen_instance_id" {
  value = length(module.datagen) > 0 ? module.datagen[0].instance_id : null
}

output "datagen_public_ip" {
  value = length(module.datagen) > 0 ? module.datagen[0].public_ip : null
}

output "datagen_ssh_command" {
  value = length(module.datagen) > 0 ? module.datagen[0].ssh_command : null
}

output "staging_bucket_region" {
  value = module.shared.region
}

output "estuary_access_key_id" {
  value     = module.shared.estuary_access_key_id
  sensitive = true
}

output "estuary_secret_access_key" {
  value     = module.shared.estuary_secret_access_key
  sensitive = true
}

output "bigquery_project_id" {
  value = length(module.bigquery) > 0 ? module.bigquery[0].project_id : null
}

output "bigquery_region" {
  value = length(module.bigquery) > 0 ? module.bigquery[0].region : null
}

output "bigquery_runner_sa_email" {
  value = length(module.bigquery) > 0 ? module.bigquery[0].benchmark_runner_service_account_email : null
}

output "bigquery_runner_sa_key" {
  value     = length(module.bigquery) > 0 ? module.bigquery[0].benchmark_runner_service_account_key_json : null
  sensitive = true
}

output "bigquery_estuary_staging_bucket" {
  value = length(module.bigquery) > 0 ? module.bigquery[0].estuary_bigquery_staging_bucket : null
}

output "bigquery_estuary_sa_key" {
  value     = length(module.bigquery) > 0 ? module.bigquery[0].estuary_bigquery_service_account_key_json : null
  sensitive = true
}

output "snowflake_database" {
  value = length(module.snowflake) > 0 ? module.snowflake[0].database : null
}

output "snowflake_warehouses" {
  value = length(module.snowflake) > 0 ? module.snowflake[0].warehouses : null
}

# → SNOWFLAKE_BENCHMARK_USER in env/snowflake.env (harness + Estuary; service user)
output "snowflake_benchmark_user" {
  value = length(module.snowflake) > 0 ? module.snowflake[0].benchmark_user : null
}

# (No snowflake_benchmark_private_key output: the RSA keypair is generated
# outside terraform, so the private half never enters state.
# env/snowflake.env's SNOWFLAKE_BENCHMARK_PRIVATE_KEY_FILE points at the local key file.)

# → tfvars databricks_host (stage 2 of the Azure bring-up); strip the scheme
# for DATABRICKS_HOST in env/databricks.env.
output "databricks_azure_workspace_url" {
  value = length(module.databricks_azure) > 0 ? module.databricks_azure[0].workspace_url : null
}

output "databricks_azure_workspace_id" {
  value = length(module.databricks_azure) > 0 ? module.databricks_azure[0].workspace_id : null
}

output "databricks_azure_resource_group" {
  value = length(module.databricks_azure) > 0 ? module.databricks_azure[0].resource_group_name : null
}

output "databricks_catalog" {
  value = length(module.databricks) > 0 ? module.databricks[0].catalog : null
}

output "databricks_sql_http_path" {
  value = length(module.databricks) > 0 ? module.databricks[0].sql_http_path : null
}

# → DATABRICKS_TOKEN in env/databricks.env + the Estuary materialization env.
# Minted only on the Azure az-cli auth path (create_harness_pat).
output "databricks_harness_token" {
  value     = length(module.databricks) > 0 ? module.databricks[0].harness_token : null
  sensitive = true
}

output "redshift_workgroup" {
  value = length(module.redshift) > 0 ? module.redshift[0].workgroup : null
}

output "redshift_endpoint_address" {
  description = "Flat host:port string for the Estuary materialize-redshift `address` field."
  value       = length(module.redshift) > 0 ? module.redshift[0].endpoint_address : null
}

output "redshift_admin_username" {
  value = length(module.redshift) > 0 ? module.redshift[0].admin_username : null
}

output "redshift_admin_password" {
  value     = length(module.redshift) > 0 ? module.redshift[0].admin_password : null
  sensitive = true
}

# → FABRIC_TENANT_ID
output "fabric_tenant_id" {
  value = length(module.fabric) > 0 ? module.fabric[0].tenant_id : null
}

output "fabric_workspace_id" {
  value = length(module.fabric) > 0 ? module.fabric[0].workspace_id : null
}

output "fabric_dw_id" {
  value = length(module.fabric) > 0 ? module.fabric[0].dw_id : null
}

# → FABRIC_DW_ENDPOINT
output "fabric_dw_endpoint" {
  value = length(module.fabric) > 0 ? module.fabric[0].dw_endpoint : null
}

# → FABRIC_DW_DATABASE
output "fabric_dw_database" {
  value = length(module.fabric) > 0 ? module.fabric[0].dw_database : null
}

output "fabric_harness_client_id" {
  value = length(module.fabric) > 0 ? module.fabric[0].harness_client_id : null
}

output "fabric_harness_client_secret" {
  value     = length(module.fabric) > 0 ? module.fabric[0].harness_client_secret : null
  sensitive = true
}

output "fabric_harness_sp_object_id" {
  value = length(module.fabric) > 0 ? module.fabric[0].harness_sp_object_id : null
}

# → FABRIC_STAGING_STORAGE_ACCOUNT
output "fabric_estuary_storage_account" {
  value = length(module.fabric) > 0 ? module.fabric[0].estuary_storage_account_name : null
}

# → FABRIC_STAGING_STORAGE_KEY
output "fabric_estuary_storage_key" {
  value     = length(module.fabric) > 0 ? module.fabric[0].estuary_storage_account_key : null
  sensitive = true
}

# → FABRIC_STAGING_CONTAINER
output "fabric_estuary_storage_container" {
  value = length(module.fabric) > 0 ? module.fabric[0].estuary_storage_container : null
}

output "clickhouse_service_id" {
  value = length(module.clickhouse) > 0 ? module.clickhouse[0].service_id : null
}

output "motherduck_database" {
  value = module.motherduck.database_name
}

output "motherduck_tier" {
  value = module.motherduck.tier
}

# Consolidated provisioned-compute view, keyed by platform. Exported via
# `benchmark export-terraform-outputs` (terraform output -json) and read by the
# harness (benchmark.terraform_outputs.provisioned_compute), which records it
# into engine_environment per (platform, scale_factor, run_type, compute_label).
# Snowflake pre-creates every warehouse size (warehouse_sizes map, resolved per
# scale in the harness); the others hold the one size currently applied, which
# `benchmark prepare-scale` sets per scale through sizing.auto.tfvars.json (or an
# out-of-band resize changes). Engines not provisioned resolve to null.
output "engine_compute" {
  value = {
    snowflake = length(module.snowflake) > 0 ? {
      warehouses      = module.snowflake[0].warehouses
      warehouse_sizes = module.snowflake[0].warehouse_sizes
    } : null
    bigquery = length(module.bigquery) > 0 ? {
      edition           = "ENTERPRISE"
      reservation_slots = var.bigquery_reservation_slot_capacity
    } : null
    databricks = length(module.databricks) > 0 ? {
      warehouse_size = var.databricks_warehouse_size
      serverless     = true
    } : null
    redshift = length(module.redshift) > 0 ? {
      baseline_rpu = module.redshift[0].baseline_rpu
      max_rpu      = module.redshift[0].max_rpu
    } : null
    fabric = length(module.fabric) > 0 ? {
      capacity_sku = var.fabric_capacity_sku
    } : null
    clickhouse = length(module.clickhouse) > 0 ? {
      service_id = var.clickhouse_service_id
      # Service is provisioned out-of-band; num_replicas is the operator-declared
      # expectation the runtime ClickHouse Cloud API check validates against.
      num_replicas = var.clickhouse_num_replicas
    } : null
    motherduck = {
      tier = module.motherduck.tier
    }
  }
}
