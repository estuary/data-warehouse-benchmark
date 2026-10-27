// Snowflake module — database, per-scale schemas, warehouses, RBAC role.
//
// Layout: BENCHMARK_DB with one schema per suite and scale factor
// (TPCDS_<SF>, DSB_<SF>) plus CLICKBENCH, five warehouses (XSMALL-XLARGE)
// sized for each scale tier, and a BENCHMARK_ROLE granted USAGE on everything.
// BENCHMARK_ROLE also receives the SNOWFLAKE.USAGE_VIEWER database role so the
// harness can read QUERY_ATTRIBUTION_HISTORY for per-query cost reconciliation
// (see the grant near the warehouse grants below).

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    snowflake = {
      source  = "snowflakedb/snowflake"
      version = "~> 2.17"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }
}

variable "account" {
  description = "Snowflake account identifier (e.g. xy12345)."
  type        = string
}

variable "admin_user" {
  description = "Privileged Snowflake user (ACCOUNTADMIN-grade) the provider authenticates as while creating module resources. Not the harness's service user — that's BENCHMARK_USER, created below."
  type        = string
  sensitive   = true
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "database_name" {
  type    = string
  default = "BENCHMARK_DB"
}

variable "role_name" {
  type    = string
  default = "BENCHMARK_ROLE"
}

variable "benchmark_user_name" {
  description = "Service user the harness + Estuary authenticate as. Receives BENCHMARK_ROLE."
  type        = string
  default     = "BENCHMARK_USER"
}

variable "benchmark_user_network_policy" {
  description = "Name of an EXISTING Snowflake NETWORK POLICY to assign to BENCHMARK_USER (e.g. 'MY_VPN_POLICY'). Takes precedence over the terraform-managed policy below. Leave empty to let terraform manage one (see benchmark_access_cidrs) or to skip assignment entirely. Required when the account has REQUIRE_NETWORK_POLICY=TRUE."
  type        = string
  default     = ""
}

variable "estuary_egress_cidrs" {
  description = "Estuary Cloud egress IPs to allowlist so the materialize-snowflake connector (and flowctl's publish-time endpoint validation) can reach Snowflake. Defaults to the AWS us-east-1 c1 public data plane — confirm/refresh in the Estuary dashboard (Admin > Settings > Data Planes > Public). Folded into the terraform-managed network policy."
  type        = list(string)
  default     = ["107.20.68.5/32", "98.89.112.85/32"]
}

variable "benchmark_access_cidrs" {
  description = "Operator/VPN CIDRs allowed to connect as BENCHMARK_USER (the harness runs from your network). When benchmark_user_network_policy is empty AND this is non-empty, terraform creates BENCHMARK_NETWORK_POLICY = these + estuary_egress_cidrs and assigns it (Snowflake allows one policy per user, so it must contain both). Find your VPN ranges via: DESCRIBE NETWORK POLICY MY_VPN_POLICY."
  type        = list(string)
  default     = []
}

variable "benchmark_user_rsa_public_key" {
  description = <<-EOT
    PEM-formatted RSA PUBLIC KEY for BENCHMARK_USER. Operator generates the
    keypair outside terraform (so the private key never enters state). Both
    BEGIN/END markers and embedded newlines are stripped automatically.

    Generation steps (one-time):
      mkdir -p ~/.snowflake && chmod 700 ~/.snowflake
      openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM \
        -out ~/.snowflake/benchmark_key.p8 -nocrypt
      openssl rsa -in ~/.snowflake/benchmark_key.p8 -pubout \
        -out ~/.snowflake/benchmark_key.pub
      chmod 600 ~/.snowflake/benchmark_key.p8

    Then set:
      * this variable to the contents of benchmark_key.pub (heredoc-friendly)
      * env/snowflake.env's SNOWFLAKE_BENCHMARK_PRIVATE_KEY_FILE = ~/.snowflake/benchmark_key.p8
  EOT
  type        = string
}

variable "scale_factors" {
  description = "Scale-factor tokens used to derive per-suite schemas. The module emits TPCDS_<SF> + DSB_<SF> per entry, plus a single CLICKBENCH schema (clickbench is single-scale)."
  type        = list(string)
  default     = ["SF1", "SF10", "SF100", "SF1000"]
}

variable "extra_schemas" {
  description = "Schemas in addition to per-suite per-scale (CLICKBENCH for the single-scale ClickBench block)."
  type        = list(string)
  default     = ["CLICKBENCH"]
}

locals {
  # Three-block per-suite per-scale schemas: TPCDS_SF1, DSB_SF1, TPCDS_SF10,
  # DSB_SF10, etc. ClickBench is single-scale and lives under extra_schemas.
  per_suite_scale_schemas = [
    for pair in setproduct(["TPCDS", "DSB"], var.scale_factors) :
    "${pair[0]}_${pair[1]}"
  ]
}

variable "warehouse_sizes" {
  description = "Map of logical name -> Snowflake size."
  type        = map(string)
  default = {
    BENCHMARK_WH_XSMALL = "XSMALL"
    BENCHMARK_WH_SMALL  = "SMALL"
    BENCHMARK_WH_MEDIUM = "MEDIUM"
    BENCHMARK_WH_LARGE  = "LARGE"
    BENCHMARK_WH_XLARGE = "XLARGE"
  }
}

# Provider is configured at the root (terraform/main.tf) so this module can
# be instantiated with count/for_each. Don't add a nested `provider "snowflake"`
# here — Terraform forbids it on modules with count.

resource "snowflake_database" "benchmark" {
  name                        = var.database_name
  comment                     = "2026 Cloud Warehouse Benchmark."
  data_retention_time_in_days = 1
}

resource "snowflake_schema" "per_suite_scale" {
  for_each = toset(local.per_suite_scale_schemas)
  database = snowflake_database.benchmark.name
  name     = each.value
  comment  = "Per-suite per-scale schema (one of TPCDS_<SF>, DSB_<SF>)."
}

resource "snowflake_schema" "extra" {
  for_each = toset(var.extra_schemas)
  database = snowflake_database.benchmark.name
  name     = each.value
  comment  = "Aux schema: ${each.value}."
}

resource "snowflake_warehouse" "wh" {
  for_each       = var.warehouse_sizes
  name           = each.key
  warehouse_size = each.value
  generation     = "2" # Gen2 Standard per CLAUDE.md (provider: generation "1"/"2"; resource_constraint is snowpark-only)
  auto_suspend   = each.value == "XLARGE" ? 300 : 60
  auto_resume    = true
  comment        = "Benchmark warehouse: ${each.value}"
}

resource "snowflake_account_role" "benchmark" {
  name    = var.role_name
  comment = "Benchmark harness execution role."
}

resource "snowflake_grant_privileges_to_account_role" "db_usage" {
  account_role_name = snowflake_account_role.benchmark.name
  privileges        = ["USAGE"]
  on_account_object {
    object_type = "DATABASE"
    object_name = snowflake_database.benchmark.name
  }
}

resource "snowflake_grant_privileges_to_account_role" "schema_usage" {
  for_each          = toset(concat(local.per_suite_scale_schemas, var.extra_schemas))
  account_role_name = snowflake_account_role.benchmark.name
  # CREATE STAGE + CREATE FILE FORMAT are belt-and-suspenders for Estuary's
  # materialize-snowflake connector: the current version uses table-scoped
  # internal stages (no CREATE STAGE needed), but a future version that switches to
  # a named stage / COPY-from-external-stage path would need these.
  privileges = [
    "USAGE",
    "CREATE TABLE",
    "CREATE VIEW",
    "CREATE STAGE",
    "CREATE FILE FORMAT",
  ]
  on_schema {
    schema_name = "\"${var.database_name}\".\"${each.value}\""
  }
  depends_on = [snowflake_schema.per_suite_scale, snowflake_schema.extra]
}

resource "snowflake_grant_privileges_to_account_role" "table_future" {
  # FUTURE table grants — apply to tables created AFTER terraform runs.
  # Required for Estuary: the materializer creates the destination tables
  # at load time. Without this, only the auto-owner rights apply (sufficient
  # in practice since BENCHMARK_ROLE will own what it creates) but the
  # explicit grant chain stays consistent if a different role ever creates
  # a table in these schemas.
  for_each          = toset(concat(local.per_suite_scale_schemas, var.extra_schemas))
  account_role_name = snowflake_account_role.benchmark.name
  privileges        = ["SELECT", "INSERT", "UPDATE", "DELETE", "REFERENCES", "TRUNCATE"]
  on_schema_object {
    future {
      object_type_plural = "TABLES"
      in_schema          = "\"${var.database_name}\".\"${each.value}\""
    }
  }
  depends_on = [snowflake_schema.per_suite_scale, snowflake_schema.extra]
}

resource "snowflake_grant_privileges_to_account_role" "warehouse_usage" {
  for_each          = snowflake_warehouse.wh
  account_role_name = snowflake_account_role.benchmark.name
  privileges        = ["USAGE", "OPERATE"]
  on_account_object {
    object_type = "WAREHOUSE"
    object_name = each.value.name
  }
}

# Read access to the cost/usage Account Usage views so the harness can back-fill
# authoritative per-query credits (`benchmark reconcile`). The reconcile + cost
# paths read exactly one Account Usage view — SNOWFLAKE.ACCOUNT_USAGE.QUERY_
# ATTRIBUTION_HISTORY (connectors/snowflake.py, analysis/reconcile.py) — which
# per the docs is exposed by the USAGE_VIEWER *or* GOVERNANCE_VIEWER database
# role. USAGE_VIEWER ("historical usage information") is the minimal of the two;
# this is deliberately narrower than `GRANT IMPORTED PRIVILEGES ON DATABASE
# SNOWFLAKE`, which would bundle all four viewer roles. Granting a shared
# SNOWFLAKE database role requires the provider's admin_user to be ACCOUNTADMIN-grade.
# Docs: docs.snowflake.com/en/sql-reference/account-usage/query_attribution_history
resource "snowflake_grant_database_role" "usage_viewer" {
  database_role_name = "\"SNOWFLAKE\".\"USAGE_VIEWER\""
  parent_role_name   = snowflake_account_role.benchmark.name
}

# ─── Service user + RSA key-pair ────────────────────────────────────────────
# BENCHMARK_USER is the service principal both the harness and Estuary
# materialize-snowflake authenticate as. We use snowflake_service_user
# (TYPE = SERVICE) + RSA key-pair auth — both consumers support JWT:
#   * Harness: snowflake-connector-python with private_key_file=
#   * Estuary materialize-snowflake: credentials.auth_type=jwt with PEM
#
# Key generation is OUT-OF-BAND (operator runs openssl locally; see the
# module-level comment / tfvars.example). The operator pastes the PUBLIC
# key PEM into var.benchmark_user_rsa_public_key; the private key never
# touches terraform state.

locals {
  # Snowflake's rsa_public_key attribute expects the base64 body only —
  # strip the BEGIN/END PUBLIC KEY markers and embedded newlines so the
  # operator can paste the full PEM (with markers) into tfvars verbatim.
  benchmark_user_pubkey_body = replace(
    replace(
      replace(
        replace(
          var.benchmark_user_rsa_public_key,
          "-----BEGIN PUBLIC KEY-----", ""
        ),
        "-----END PUBLIC KEY-----", ""
      ),
      "\n", ""
    ),
    "\r", ""
  )
}

# Terraform-managed network policy for BENCHMARK_USER: combines the operator's
# access/VPN CIDRs with Estuary Cloud's egress IPs, so BOTH the harness (from
# your network) and Estuary's materialize-snowflake connector (publish-time
# validation + the actual load) can reach Snowflake. Snowflake allows one policy
# per user, so a single policy must carry both sets. Created only when no
# external policy name is supplied and access CIDRs are given.
resource "snowflake_network_policy" "benchmark" {
  count           = var.benchmark_user_network_policy == "" && length(var.benchmark_access_cidrs) > 0 ? 1 : 0
  name            = "BENCHMARK_NETWORK_POLICY"
  allowed_ip_list = concat(var.benchmark_access_cidrs, var.estuary_egress_cidrs)
  comment         = "Benchmark: operator/VPN access + Estuary Cloud egress IPs for BENCHMARK_USER."
}

resource "snowflake_service_user" "benchmark" {
  name           = var.benchmark_user_name
  rsa_public_key = local.benchmark_user_pubkey_body
  default_role   = snowflake_account_role.benchmark.name
  disabled       = false
  # Policy resolution: an explicit external policy name wins; else the
  # terraform-managed BENCHMARK_NETWORK_POLICY (when access CIDRs were given);
  # else null (inherit account-default, if any). Required when the account has
  # REQUIRE_NETWORK_POLICY=TRUE — a VPN-only policy blocks Estuary's load.
  network_policy = (
    var.benchmark_user_network_policy != ""
    ? var.benchmark_user_network_policy
    : one(snowflake_network_policy.benchmark[*].name)
  )
  comment = "Benchmark harness + Estuary service user (BENCHMARK_ROLE) — JWT key-pair auth."
}

resource "snowflake_grant_account_role" "user_benchmark_role" {
  role_name = snowflake_account_role.benchmark.name
  user_name = snowflake_service_user.benchmark.name
}

output "database" {
  value = snowflake_database.benchmark.name
}

output "role" {
  value = snowflake_account_role.benchmark.name
}

output "schemas" {
  value = concat(
    [for s in snowflake_schema.per_suite_scale : s.name],
    [for s in snowflake_schema.extra : s.name],
  )
}

output "warehouses" {
  value = [for w in snowflake_warehouse.wh : w.name]
}

# Per-scale warehouse size map (e.g. {1 = "XSMALL", 100 = "MEDIUM", ...}) —
# Snowflake is the one engine that sizes compute by scale. The harness resolves
# this to the size for each run's scale (terraform_outputs.provisioned_compute).
output "warehouse_sizes" {
  value = var.warehouse_sizes
}

# → SNOWFLAKE_BENCHMARK_USER (both env/snowflake.env and Estuary YAML)
output "benchmark_user" {
  value = snowflake_service_user.benchmark.name
}

# (No private_key output: the key is generated outside terraform, so the
# private half never enters state. Point env/snowflake.env's
# SNOWFLAKE_BENCHMARK_PRIVATE_KEY_FILE directly at the local key file.)
