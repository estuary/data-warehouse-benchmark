// Azure Databricks module — resource group, premium workspace, Unity Catalog
// access connector, and ADLS Gen2 managed storage for the benchmark catalog.
//
// Provisioning model (mirrors modules/fabric — Azure control plane first,
// engine control plane second):
//   * This module owns the ARM resources ONLY (azurerm provider, authenticated
//     off the operator's `az login` via the root provider blocks). It creates
//     the workspace but nothing INSIDE it.
//   * Everything inside the workspace — UC storage credential / external
//     location, catalog, schemas, SQL warehouse, harness PAT — lives in
//     modules/databricks (databricks provider), because the databricks
//     provider can't be configured until the workspace URL exists.
//
// Bring-up is therefore two-staged, like Fabric's `-target=module.fabric`:
//   1. terraform apply -target='module.databricks_azure'
//   2. copy `databricks_azure_workspace_url` into tfvars `databricks_host`,
//      leave `databricks_token` empty (the root databricks provider falls back
//      to azure-cli auth), then a plain `terraform apply` builds the workspace
//      contents. See terraform/README.md "Azure Databricks bring-up".
//
// Unity Catalog: all current Azure regions auto-enable UC — the workspace
// attaches to the region's auto-provisioned metastore on creation. That
// metastore has NO root storage, so catalogs need an explicit storage_root;
// this module provisions the ADLS Gen2 account + access connector that
// modules/databricks turns into a storage credential + external location.
//
// Cost: the workspace resource itself is free — billing is per-DBU when the
// SQL warehouse (modules/databricks, auto_stop_mins = 10) is running, plus
// pennies for the storage account. Nothing here needs pausing between runs.

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.78"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }
}

variable "resource_group_name" {
  description = "Dedicated resource group (separate from Fabric's rg-benchmark-2026 so either engine can be torn down independently)."
  type        = string
  default     = "rg-benchmark-2026-databricks"
}

variable "location" {
  type    = string
  default = "eastus"
}

variable "workspace_name" {
  type    = string
  default = "benchmark-2026-databricks"
}

# Subscription/tenant targeting comes from the ROOT azurerm provider block
# (var.fabric_subscription_id + var.fabric_tenant_id — shared with the Fabric
# module), so this module deliberately takes no subscription_id. The module
# takes count at the root, which is why the provider must live there.
#
# Resource-provider registration: azurerm v4's default "core" set includes
# Microsoft.Databricks, so no explicit azurerm_resource_provider_registration
# (unlike Microsoft.Fabric). If the create ever 409s with
# MissingSubscriptionRegistration, run once:
#   az provider register --namespace Microsoft.Databricks

resource "azurerm_resource_group" "rg" {
  name     = var.resource_group_name
  location = var.location
  tags = {
    Project = "benchmark-2026"
  }
}

# Premium SKU is required for Unity Catalog (and for the PRO SQL warehouse
# type modules/databricks provisions).
resource "azurerm_databricks_workspace" "benchmark" {
  name                        = var.workspace_name
  resource_group_name         = azurerm_resource_group.rg.name
  location                    = azurerm_resource_group.rg.location
  sku                         = "premium"
  managed_resource_group_name = "${var.resource_group_name}-managed"
  tags = {
    Project = "benchmark-2026"
  }
}

# Access connector: the managed identity Unity Catalog uses to reach the
# catalog storage account. modules/databricks wraps it in a
# databricks_storage_credential.
resource "azurerm_databricks_access_connector" "uc" {
  name                = "${var.workspace_name}-uc"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  identity {
    type = "SystemAssigned"
  }
  tags = {
    Project = "benchmark-2026"
  }
}

# Catalog managed storage (ADLS Gen2). Globally-unique name → random suffix,
# region-keyed for the same rename-on-region-change reason as modules/fabric.
resource "random_string" "sa_suffix" {
  length  = 8
  lower   = true
  upper   = false
  numeric = true
  special = false
  keepers = {
    location = var.location
  }
}

resource "azurerm_storage_account" "unity_catalog" {
  name                     = "bench2026dbx${random_string.sa_suffix.result}"
  resource_group_name      = azurerm_resource_group.rg.name
  location                 = azurerm_resource_group.rg.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"
  is_hns_enabled           = true # ADLS Gen2 — required for UC external locations
  tags = {
    Project = "benchmark-2026"
  }
}

resource "azurerm_storage_container" "unity_catalog" {
  name               = "unity-catalog"
  storage_account_id = azurerm_storage_account.unity_catalog.id
}

# UC's standard grant for managed storage: the access connector's identity
# gets Blob Data Contributor on the account. Requires the operator identity
# running terraform to hold a role-assignment-capable role (Owner or
# User Access Administrator) on the subscription/RG — plain Contributor
# cannot create role assignments.
resource "azurerm_role_assignment" "uc_storage" {
  scope                = azurerm_storage_account.unity_catalog.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_databricks_access_connector.uc.identity[0].principal_id
}

# ── Outputs ─────────────────────────────────────────────────────────────────

output "resource_group_name" {
  value = azurerm_resource_group.rg.name
}

# → tfvars databricks_host (stage 2 of the bring-up) and, without the scheme,
# → DATABRICKS_HOST in env/databricks.env.
output "workspace_url" {
  value = "https://${azurerm_databricks_workspace.benchmark.workspace_url}"
}

# Numeric Databricks workspace ID (not the ARM resource ID).
output "workspace_id" {
  value = azurerm_databricks_workspace.benchmark.workspace_id
}

# ARM resource ID of the access connector — modules/databricks wraps this in a
# databricks_storage_credential (azure_managed_identity).
output "access_connector_id" {
  value = azurerm_databricks_access_connector.uc.id
}

# abfss root of the UC container — modules/databricks registers it as an
# external location and roots the benchmark catalog under it.
output "catalog_storage_root" {
  value = "abfss://${azurerm_storage_container.unity_catalog.name}@${azurerm_storage_account.unity_catalog.name}.dfs.core.windows.net"
}
