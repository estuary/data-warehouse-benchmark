variable "aws_region" {
  description = "AWS region for shared resources (S3 staging + Redshift)."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "Named AWS profile used by the aws provider. Matches the `benchmark` profile in ~/.aws/credentials."
  type        = string
  default     = "benchmark"
}

variable "staging_bucket_name" {
  description = "Globally-unique S3 bucket name for benchmark staging data."
  type        = string
}

variable "staging_force_destroy" {
  description = "Allow Terraform to delete a non-empty staging bucket on destroy."
  type        = bool
  default     = false
}

# --- BigQuery ----------------------------------------------------------------
variable "gcp_project_id" {
  description = "GCP project ID. Leave empty to skip the bigquery module."
  type        = string
  default     = ""
}

variable "gcp_region" {
  type    = string
  default = "us-central1"
}

variable "bigquery_reservation_enabled" {
  description = "Provision Enterprise Edition reservation + assignment. When false, queries use on-demand billing."
  type        = bool
  default     = false
}

variable "bigquery_reservation_slot_capacity" {
  description = "Enterprise reservation baseline slots. Sized per scale by `benchmark prepare-scale` via sizing.auto.tfvars.json (run_plan.yaml: 100/100/200/500 for SF1/10/100/1000)."
  type        = number
  default     = 100
}

variable "bigquery_staging_bucket_name" {
  type    = string
  default = ""
}

variable "bigquery_staging_force_destroy" {
  type    = bool
  default = false
}

# --- Snowflake ---------------------------------------------------------------
variable "snowflake_organization" {
  description = "Snowflake organization name (v2 provider). Optional when snowflake_account holds the full locator."
  type        = string
  default     = ""
}

variable "snowflake_account" {
  description = "Snowflake account identifier (e.g. xy12345). Empty disables the module."
  type        = string
  default     = ""
}

variable "snowflake_admin_user" {
  description = "Privileged Snowflake user (typically ACCOUNTADMIN) that terraform authenticates as while creating the database, warehouses, role, and BENCHMARK_USER. Distinct from the harness's BENCHMARK_USER, which terraform creates with a JWT key-pair on it."
  type        = string
  sensitive   = true
  default     = ""
}

variable "snowflake_admin_private_key_path" {
  description = "Path to the unencrypted PEM private key for snowflake_admin_user. Used by the terraform provider with authenticator=SNOWFLAKE_JWT — key-pair auth bypasses MFA (required when the admin user has MFA enabled) and, unlike a PAT, does not expire on a clock or require a network policy. Register the matching public key once via `ALTER USER <admin> SET RSA_PUBLIC_KEY='<base64 body>';`. Empty disables the module's provider auth (placeholder)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "snowflake_benchmark_user_network_policy" {
  description = "Name of an EXISTING Snowflake NETWORK POLICY to assign to BENCHMARK_USER (e.g. 'MY_VPN_POLICY'). Takes precedence over the terraform-managed policy. Leave empty to let terraform manage one from snowflake_benchmark_access_cidrs + snowflake_estuary_egress_cidrs. Required when the account has REQUIRE_NETWORK_POLICY=TRUE."
  type        = string
  default     = ""
}

variable "snowflake_estuary_egress_cidrs" {
  description = "Estuary Cloud egress IPs to allowlist on BENCHMARK_USER's network policy so the materialize-snowflake connector (and flowctl publish-time validation) can reach Snowflake. Defaults to AWS us-east-1 c1; refresh from the Estuary dashboard if your data plane differs."
  type        = list(string)
  default     = ["107.20.68.5/32", "98.89.112.85/32"]
}

variable "snowflake_benchmark_access_cidrs" {
  description = "Operator/VPN CIDRs allowed to connect as BENCHMARK_USER (the harness runs from your network). When snowflake_benchmark_user_network_policy is empty and this is non-empty, terraform creates + assigns BENCHMARK_NETWORK_POLICY = these + snowflake_estuary_egress_cidrs."
  type        = list(string)
  default     = []
}

variable "snowflake_benchmark_user_rsa_public_key" {
  description = "PEM-formatted RSA PUBLIC KEY for BENCHMARK_USER (TYPE = SERVICE; auth is JWT key-pair). Generate the keypair locally — see the description on var benchmark_user_rsa_public_key in modules/snowflake/main.tf for the openssl recipe. The private half goes into env/snowflake.env's SNOWFLAKE_BENCHMARK_PRIVATE_KEY_FILE; only the public half lives here."
  type        = string
  default     = ""
}

variable "snowflake_region" {
  type    = string
  default = "us-east-1"
}

# --- Databricks --------------------------------------------------------------
variable "databricks_host" {
  description = "Workspace URL (e.g. https://adb-...azuredatabricks.net, or https://dbc-...cloud.databricks.com on AWS). Empty disables the workspace-contents module. For the Azure bring-up, set it from the databricks_azure_workspace_url output after the stage-1 targeted apply."
  type        = string
  default     = ""
}

variable "databricks_token" {
  description = "Workspace PAT. Leave empty with an Azure host to authenticate the databricks provider off `az login` instead (the bring-up path — no PAT exists before the workspace does; the module then mints one, exported as databricks_harness_token)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "databricks_warehouse_size" {
  description = "Serverless SQL warehouse size: 2X-Small ... 4X-Large. Sized per scale by `benchmark prepare-scale` via sizing.auto.tfvars.json (run_plan.yaml: X-Small/Small/Medium/Large for SF1/10/100/1000)."
  type        = string
  default     = "Medium"
}

# --- Azure Databricks workspace (ARM layer) -----------------------------------
variable "databricks_azure_enabled" {
  description = "Provision the Azure Databricks workspace + Unity Catalog storage (modules/databricks_azure). Uses the shared Azure providers, so fabric_subscription_id + fabric_tenant_id must be set. Two-stage bring-up: apply -target='module.databricks_azure' first — see terraform/README.md."
  type        = bool
  default     = false

  validation {
    condition     = !var.databricks_azure_enabled || var.fabric_subscription_id != ""
    error_message = "databricks_azure_enabled requires fabric_subscription_id (the shared Azure subscription) to be set."
  }
}

variable "databricks_azure_location" {
  description = "Azure region for the Databricks workspace. Must be a region with automatic Unity Catalog enablement (all current regions qualify)."
  type        = string
  default     = "eastus"
}

# --- Redshift Serverless -----------------------------------------------------
variable "redshift_enabled" {
  description = "Whether to provision the Redshift Serverless module. Admin password is auto-generated; pull it from `terraform output -raw redshift_admin_password`."
  type        = bool
  default     = false
}

variable "redshift_admin_password_override" {
  description = "Optional override for the Redshift admin password. Leave empty (default) to use the module's random_password generator."
  type        = string
  sensitive   = true
  default     = ""
}

variable "redshift_baseline_rpu" {
  description = "Redshift Serverless baseline RPU (8 minimum). Sized per scale by `benchmark prepare-scale` via sizing.auto.tfvars.json (run_plan.yaml: 8/8/16/64 for SF1/10/100/1000)."
  type        = number
  default     = 8
}

# --- Microsoft Fabric --------------------------------------------------------
# fabric_subscription_id / fabric_tenant_id also target the shared root Azure
# providers (azurerm/azapi/azuread), which the databricks_azure module uses too.
variable "fabric_subscription_id" {
  description = "Azure subscription ID. Empty disables the Fabric module (and, via validation, databricks_azure). Also targets the shared root azurerm/azapi providers."
  type        = string
  default     = ""
}

variable "fabric_tenant_id" {
  description = "Entra tenant ID for the shared root Azure providers (fabric + databricks_azure)."
  type        = string
  default     = ""
}

variable "fabric_enabled" {
  description = "Provision the Fabric module when fabric_subscription_id is set. Defaults true, so setting the subscription enables Fabric. Set false to use the shared Azure subscription for databricks_azure only."
  type        = bool
  default     = true
}

variable "fabric_location" {
  type    = string
  default = "eastus"
}

variable "fabric_capacity_sku" {
  description = "Fabric capacity tier (F2, F4, F8, F16, F32, F64, F128). Sized per scale by `benchmark prepare-scale` via sizing.auto.tfvars.json (run_plan.yaml: F2/F8/F32/F128 for SF1/10/100/1000); pause the capacity when idle to control cost."
  type        = string
  default     = "F2"
}

variable "fabric_capacity_admin_members" {
  description = "Fabric capacity administrators. Entra USERS as UPNs (emails), SERVICE PRINCIPALS as object IDs. Required when Fabric is enabled, e.g. [\"you@example.com\"]."
  type        = list(string)
  default     = []
}

# --- ClickHouse Cloud --------------------------------------------------------
# The ClickHouse module is a no-op (see modules/clickhouse/main.tf). The
# service is managed in the ClickHouse Cloud UI; terraform only records the
# service UUID for state symmetry. Connection details + API key go straight
# into env/clickhouse.env from the Cloud UI.
variable "clickhouse_service_id" {
  description = "UUID of the pre-existing 'Estuary Benchmark' ClickHouse Cloud service. Recorded by the terraform module for state symmetry. Empty disables the module."
  type        = string
  default     = ""
}

variable "clickhouse_num_replicas" {
  description = "Operator-declared expected replica count of the out-of-band ClickHouse Cloud service. Surfaced in engine_compute so the runtime Cloud API check can validate the running service. null = no expectation (validation skipped)."
  type        = number
  default     = null
}

# --- Datagen EC2 (opt-in, ephemeral) ------------------------------------------
variable "datagen_enabled" {
  description = "Whether to provision the datagen EC2 instance (TPC-DS/DSB generation + S3 staging at SF100+). Flip to false and re-apply to tear it down between campaigns."
  type        = bool
  default     = false
}

variable "datagen_instance_type" {
  description = "Instance type for the datagen box."
  type        = string
  default     = "m8i.8xlarge"
}

variable "datagen_data_volume_gb" {
  description = "gp3 data volume size (mounted at /data). ~200 GB for SF100, ~2500 GB for SF1000."
  type        = number
  default     = 3500
}

variable "datagen_ssh_public_key" {
  description = "OpenSSH public key for operator SSH. Leave empty to use SSM Session Manager only (no SSH ingress)."
  type        = string
  default     = ""
}

variable "datagen_allowed_ssh_cidr" {
  description = "CIDR allowed to SSH to the datagen box (e.g. the VPN egress /32)."
  type        = string
  default     = ""
}

# --- MotherDuck --------------------------------------------------------------
variable "motherduck_database" {
  type    = string
  default = "benchmark_db"
}

variable "motherduck_tier" {
  type    = string
  default = "standard"
}
