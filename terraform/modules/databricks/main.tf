// Databricks SQL module — workspace catalog, per-scale schemas, serverless
// SQL warehouse, and (on the Azure az-cli path) a harness PAT.
//
// Serverless rather than Classic Pro: it is Databricks' recommended default
// (Pro is positioned as the no-serverless-region/custom-networking fallback);
// its all-inclusive DBU rate keeps DBU-based cost accounting honest (Pro's
// Azure VM cost bills OUTSIDE system.billing and would be invisible to the
// harness); and Pro's VMs launch in the customer's subscription, where a
// regional vCPU quota can stop clusters from launching at all. Serverless
// compute runs in Databricks' account: no VM quota, 2-6 s starts (vs ~4 min).
//
// Cloud-agnostic: everything here talks to the workspace control plane via
// the root databricks provider, so it works against an AWS or an Azure
// workspace. The Azure-only extras (Unity Catalog storage credential /
// external location / catalog storage_root, minted harness PAT) activate when
// the root wires in modules/databricks_azure outputs — see the two-stage
// bring-up in terraform/README.md "Azure Databricks bring-up".

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.118"
    }
  }
}

variable "host" {
  description = "Databricks workspace URL (with https://)."
  type        = string
}

variable "token" {
  type      = string
  sensitive = true
}

variable "catalog_name" {
  type    = string
  default = "benchmark_catalog"
}

variable "warehouse_name" {
  type    = string
  default = "benchmark-sql-serverless"
}

variable "warehouse_size" {
  description = "Per-cluster compute size (2X-Small … 4X-Large). Sized per scale by `benchmark prepare-scale` from run_plan.yaml. A warehouse scales out by adding clusters of this size, not by resizing; benchmarks run pinned to one cluster."
  type        = string
  default     = "Medium"
}

variable "warehouse_min_clusters" {
  description = "Lower bound of the warehouse's cluster range. Benchmarks run PINNED (min == max == 1)."
  type        = number
  default     = 1
}

variable "warehouse_max_clusters" {
  description = "Upper bound of the cluster range. Benchmarks run PINNED at 1 — multi-cluster autoscaling (IWM) would flex capacity under the concurrency pass and break the fixed-size semantics used for Redshift (max_capacity == base_capacity) and Snowflake (single-cluster warehouses). Raise temporarily ONLY for bulk loads (scripts/resize/databricks_resize.py --max-clusters N re-pins on the next resize)."
  type        = number
  default     = 1
}

variable "scale_factors" {
  description = "Scale-factor tokens used to derive per-suite schemas. The module emits tpcds_<sf> + dsb_<sf> per entry, plus a single clickbench schema (clickbench is single-scale). SF1000 is the maximum scale."
  type        = list(string)
  default     = ["sf1", "sf10", "sf100", "sf1000"]
}

variable "extra_schemas" {
  description = "Schemas in addition to per-suite per-scale: clickbench (the single-scale ClickBench block)."
  type        = list(string)
  default     = ["clickbench"]
}

# ── Azure / Unity Catalog wiring (empty on AWS workspaces) ──────────────────
# Azure's auto-provisioned regional metastores have no root storage, so
# creating the catalog requires an explicit storage_root inside a registered
# external location. Both vars come from modules/databricks_azure via the
# root; leaving them empty (the AWS case) skips the UC plumbing entirely.
# The az-cli identity running terraform must be a workspace admin — it is by
# construction, since it created the workspace — which on auto-enabled
# metastores carries CREATE STORAGE CREDENTIAL / EXTERNAL LOCATION / CATALOG.

# Gate for the UC resources. A bool (known at plan time) rather than a
# non-empty check on the two values below, which are computed attributes of
# modules/databricks_azure — counting on those would blow up with "invalid
# count argument" whenever the connector/storage account has a pending
# create or replace (e.g. a region change).
variable "azure_unity_catalog" {
  description = "Provision the Azure UC plumbing (storage credential + external location + catalog storage_root). Requires the two values below."
  type        = bool
  default     = false
}

variable "azure_access_connector_id" {
  description = "ARM resource ID of the Azure Databricks access connector (modules/databricks_azure)."
  type        = string
  default     = ""
}

variable "storage_root" {
  description = "abfss:// root for Unity Catalog managed storage (modules/databricks_azure's container). Ignored unless azure_unity_catalog; the catalog then roots under it (AWS keeps the metastore default)."
  type        = string
  default     = ""
}

variable "create_harness_pat" {
  description = "Mint a workspace PAT for the terraform caller (the operator's az-cli identity on Azure) and export it. Used on Azure where no PAT exists before the workspace does; the harness and the Estuary materialize-databricks connector (auth_type: PAT) both consume it as DATABRICKS_TOKEN."
  type        = bool
  default     = false
}

variable "pat_lifetime_seconds" {
  description = "Lifetime of the minted harness PAT. 0 = non-expiring (subject to any workspace max-lifetime policy)."
  type        = number
  default     = 0
}

locals {
  # Three-block per-suite per-scale schemas: tpcds_sf1, dsb_sf1, tpcds_sf10,
  # dsb_sf10, etc. — matching the convention the connector, the Estuary
  # materialization, and validate-loads all expect (lowercase; Spark/UC fold
  # identifiers to lowercase). ClickBench is single-scale and lives under
  # extra_schemas. Mirrors terraform/modules/snowflake.
  per_suite_scale_schemas = [
    for pair in setproduct(["tpcds", "dsb"], var.scale_factors) :
    "${pair[0]}_${pair[1]}"
  ]
}

# Provider is configured at the root so this module can take count/for_each.

# Unity Catalog storage plumbing (Azure only; count = 0 on AWS). The access
# connector's managed identity becomes a storage credential, the abfss
# container root becomes an external location, and the catalog roots its
# managed storage under it. Managed tables AND the staging volumes the Estuary
# materialize-databricks connector creates per schema all land here.
resource "databricks_storage_credential" "uc" {
  count   = var.azure_unity_catalog ? 1 : 0
  name    = "benchmark-uc"
  comment = "2026 benchmark — UC managed storage via access connector."
  azure_managed_identity {
    access_connector_id = var.azure_access_connector_id
  }
  # force_update: UC refuses in-place edits while a location depends on the
  # credential; force_destroy: let destroy drop it even if one lingers.
  force_update  = true
  force_destroy = true
}

resource "databricks_external_location" "uc" {
  count           = var.azure_unity_catalog ? 1 : 0
  name            = "benchmark-uc-root"
  url             = var.storage_root
  credential_name = databricks_storage_credential.uc[0].name
  comment         = "2026 benchmark — catalog storage root."
  # Managed-table storage under the root lingers after the catalog is dropped;
  # the ADLS container is deleted with its resource group anyway, so let UC
  # release the location without purging.
  force_destroy = true
}

resource "databricks_catalog" "benchmark" {
  name    = var.catalog_name
  comment = "2026 Cloud Warehouse Benchmark."
  # Azure: root the catalog under the registered external location (a subpath,
  # since UC forbids a catalog root exactly equal to an external location
  # another object could claim). AWS: null → metastore default root.
  storage_root = var.azure_unity_catalog ? "${var.storage_root}/${var.catalog_name}" : null
  # Disposable benchmark catalog: let destroy drop it with any residual schema.
  force_destroy = true
  depends_on    = [databricks_external_location.uc]
}

resource "databricks_schema" "per_suite_scale" {
  for_each     = toset(local.per_suite_scale_schemas)
  catalog_name = databricks_catalog.benchmark.name
  name         = each.value
  comment      = "Per-suite per-scale schema (one of tpcds_<sf>, dsb_<sf>)."
  # Schemas hold Estuary-materialised tables + staging volumes; destroy must
  # be able to drop them non-empty (Unity Catalog refuses otherwise).
  force_destroy = true
}

resource "databricks_schema" "extra" {
  for_each      = toset(var.extra_schemas)
  catalog_name  = databricks_catalog.benchmark.name
  name          = each.value
  force_destroy = true
}

# Single serverless SQL warehouse, PINNED to one cluster for benchmarks
# (min == max == 1): the benchmark measures fixed capacity per size label,
# matching Redshift's pinned max_capacity == base_capacity and Snowflake's
# single-cluster warehouses. Multi-cluster autoscaling (IWM) may be raised for
# Estuary bulk-load throughput but must stay off during measurement: it flexes
# capacity 1..max under the concurrency pass while the size label stays
# constant. Raise the ceiling for loads via
# scripts/resize/databricks_resize.py --max-clusters N; the next resize re-pins.
# The API models serverless as warehouse_type = PRO +
# enable_serverless_compute = true.
resource "databricks_sql_endpoint" "warehouse" {
  name                      = var.warehouse_name
  cluster_size              = var.warehouse_size
  warehouse_type            = "PRO"
  enable_serverless_compute = true
  # Serverless allows auto-stop as low as 5; 10 is deliberate so the
  # cold pass's stop/start cadence and idle-cost profile stay comparable to
  # the other engines (classic minimum is also 10).
  auto_stop_mins   = 10
  min_num_clusters = var.warehouse_min_clusters
  max_num_clusters = var.warehouse_max_clusters
  tags {
    custom_tags {
      key   = "Project"
      value = "benchmark-2026"
    }
  }
}

# Harness PAT (Azure bring-up). Minted for the terraform caller's identity
# once the provider can reach the workspace via az-cli auth; the token value
# lands in state + the sensitive output, same trade the redshift password and
# fabric client secret already make. Rotate by tainting this resource.
resource "databricks_token" "harness" {
  count            = var.create_harness_pat ? 1 : 0
  comment          = "2026 benchmark harness + Estuary materialize-databricks"
  lifetime_seconds = var.pat_lifetime_seconds > 0 ? var.pat_lifetime_seconds : null
}

output "catalog" {
  value = databricks_catalog.benchmark.name
}

# → DATABRICKS_TOKEN in env/databricks.env and the Estuary materialization
# (null unless create_harness_pat).
output "harness_token" {
  value     = length(databricks_token.harness) > 0 ? databricks_token.harness[0].token_value : null
  sensitive = true
}

output "sql_http_path" {
  value = databricks_sql_endpoint.warehouse.odbc_params[0].path
}

output "sql_warehouse_id" {
  value = databricks_sql_endpoint.warehouse.id
}
