// Microsoft Fabric module: F-series capacity, workspace, Data Warehouse, plus
// the AAD app registration the harness + Estuary use for service-principal auth.
//
// Provisioning model:
//   * The CAPACITY is a real ARM resource → `azurerm_fabric_capacity`.
//   * The WORKSPACE and WAREHOUSE are Fabric control-plane items
//     (api.fabric.microsoft.com), NOT ARM resources, so they're managed by the
//     typed `microsoft/fabric` provider, not azapi: Microsoft.Fabric/workspaces
//     is not an ARM type, so an azapi PUT to it fails at apply.
//   * The fabric provider authenticates off the operator's `az login`
//     (use_cli = true, configured at the root).
//
// Cost: the F-SKU capacity bills per hour while Active. There is no
// autoscale-to-zero; pause/resume the capacity (Azure portal / `az fabric
// capacity suspend|resume`) between benchmark runs to stop compute billing.
// Default SKU is F2 (~$0.36/hr). run_plan.yaml sizes the SKU per scale
// (F2/F8/F32/F128 at SF1/10/100/1000) and `benchmark prepare-scale` writes it to
// sizing.auto.tfvars.json; scripts/resize/fabric_resize.py resizes out of band.

terraform {
  required_version = ">= 1.8.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.78"
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

variable "tenant_id" {
  description = "Entra tenant ID. Echoed in the tenant_id output for env/fabric.env; actual resource targeting is done by the ROOT provider blocks (see comment below), not by this variable."
  type        = string
}

variable "resource_group_name" {
  type    = string
  default = "rg-benchmark-2026"
}

variable "location" {
  type    = string
  default = "eastus"
}

variable "capacity_sku" {
  description = "Fabric capacity tier (F2 .. F128), sized per scale from run_plan.yaml. Pause the capacity when idle to control cost."
  type        = string
  default     = "F2"
}

variable "capacity_admin_members" {
  description = "Fabric capacity administrators. Per azurerm_fabric_capacity: an Entra USER must be a UPN (email), a SERVICE PRINCIPAL an object ID (object IDs for users are rejected with 'All provided principals must be existing'). Set explicitly and share with whoever runs terraform so the admin list is deterministic regardless of who applies — do NOT derive it from the signed-in identity."
  type        = list(string)
}

variable "workspace_name" {
  type    = string
  default = "benchmark-2026"
}

variable "dw_name" {
  type    = string
  default = "BENCHMARK_DW"
}

# Subscription/tenant targeting is set on the ROOT provider blocks
# (azurerm / azapi / azuread / fabric), which read var.fabric_tenant_id +
# var.fabric_subscription_id. azurerm resources have no subscription_id
# argument — they inherit it from the provider — so this module deliberately
# does NOT take subscription_id; passing it in would be dead config. The
# module takes count/for_each at the root, which is why the providers must
# live there rather than inside the module.

# Register the Microsoft.Fabric resource provider on the subscription. azurerm
# does NOT auto-register Fabric, so the capacity create 409s
# (MissingSubscriptionRegistration) without this. Subscription comes from the
# root azurerm provider. The create blocks until the provider reaches the
# "Registered" state, so the capacity's depends_on is enough to order it.
#
# CAVEAT: azurerm errors if Microsoft.Fabric is ALREADY registered on the sub
# (e.g. registered manually via `az provider register`). If so, import it once:
#   terraform -chdir=terraform import \
#     'module.fabric[0].azurerm_resource_provider_registration.fabric' \
#     /subscriptions/<SUB_ID>/providers/Microsoft.Fabric
# Also note: `terraform destroy` of this module UNREGISTERS the provider
# subscription-wide.
resource "azurerm_resource_provider_registration" "fabric" {
  name = "Microsoft.Fabric"
}

resource "azurerm_resource_group" "rg" {
  name     = var.resource_group_name
  location = var.location
  tags = {
    Project = "benchmark-2026"
  }
}

# Estuary staging storage. Unlike Redshift (S3 COPY), the
# materialize-azure-fabric-warehouse connector stages temp files in an Azure
# Storage container and then COPY INTO's them into the DW. It authenticates to
# the storage with the account key. Globally-unique name → random suffix.
resource "random_string" "sa_suffix" {
  length  = 8
  lower   = true
  upper   = false
  numeric = true
  special = false
  # Tie the suffix to the region so changing var.location regenerates the name.
  # Storage account names are globally unique and a deleted name isn't released
  # immediately, so reusing the same name across a region-change replace makes
  # azurerm hang polling for name availability. A region-scoped name sidesteps
  # that (old and new names differ, so they never collide).
  keepers = {
    location = var.location
  }
}

resource "azurerm_storage_account" "estuary_staging" {
  name                     = "bench2026fab${random_string.sa_suffix.result}"
  resource_group_name      = azurerm_resource_group.rg.name
  location                 = azurerm_resource_group.rg.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"
  is_hns_enabled           = true # ADLS Gen2
  tags = {
    Project = "benchmark-2026"
  }
}

resource "azurerm_storage_container" "estuary_staging" {
  name               = "estuary-staging"
  storage_account_id = azurerm_storage_account.estuary_staging.id
}

# 1. Capacity (ARM resource). Admins come from var.capacity_admin_members
# (set in tfvars; user UPNs / SP object IDs) so the list is deterministic
# regardless of who runs terraform.
resource "azurerm_fabric_capacity" "benchmark" {
  name                   = "benchmarkcap2026"
  resource_group_name    = azurerm_resource_group.rg.name
  location               = azurerm_resource_group.rg.location
  administration_members = var.capacity_admin_members
  depends_on             = [azurerm_resource_provider_registration.fabric]
  sku {
    name = var.capacity_sku
    tier = "Fabric"
  }
}

# Resolve the ARM capacity into the Fabric capacity GUID the control-plane
# provider needs. depends_on defers the read to apply (after the capacity is
# created) instead of plan-time.
data "fabric_capacity" "benchmark" {
  display_name = azurerm_fabric_capacity.benchmark.name
  depends_on   = [azurerm_fabric_capacity.benchmark]
}

# 2. App registration + service principal. Estuary's materialize connector
# authenticates as this SP (FABRIC_CLIENT_ID / FABRIC_CLIENT_SECRET); the
# harness can too (or use az login via DefaultAzureCredential).
resource "azuread_application" "harness" {
  display_name = "benchmark-2026-fabric-harness"
}

resource "azuread_application_password" "harness" {
  application_id = azuread_application.harness.id
  display_name   = "harness-secret"
}

resource "azuread_service_principal" "harness" {
  client_id = azuread_application.harness.client_id
}

# The harness introspects the live capacity SKU/state via the ARM API
# (FabricConnector.runtime_compute -> capture-cost-config); the service
# principal needs Reader on the resource group for that GET.
resource "azurerm_role_assignment" "harness_rg_reader" {
  scope                = azurerm_resource_group.rg.id
  role_definition_name = "Reader"
  principal_id         = azuread_service_principal.harness.object_id
}

# 3. Workspace on the benchmark capacity.
resource "fabric_workspace" "benchmark" {
  display_name = var.workspace_name
  capacity_id  = data.fabric_capacity.benchmark.id
}

# 4. Grant the SP Admin on the workspace so Estuary can create + load tables
# into the DW. (Fabric data loading needs workspace membership rather than an IAM
# role like Redshift's; the connector reaches the staging container above with
# the storage account key.)
resource "fabric_workspace_role_assignment" "harness_sp" {
  workspace_id = fabric_workspace.benchmark.id
  principal = {
    id   = azuread_service_principal.harness.object_id
    type = "ServicePrincipal"
  }
  role = "Admin"
}

# 5. Data Warehouse: the SQL benchmark engine (all three query blocks).
# Clustered columnstore is automatic; no separate physical-design DDL (see
# physical_design.py).
resource "fabric_warehouse" "benchmark_dw" {
  display_name = var.dw_name
  workspace_id = fabric_workspace.benchmark.id
}

# ── Outputs ─────────────────────────────────────────────────────────────────

output "capacity_name" {
  value = azurerm_fabric_capacity.benchmark.name
}

output "workspace_id" {
  value = fabric_workspace.benchmark.id
}

output "dw_id" {
  value = fabric_warehouse.benchmark_dw.id
}

# → FABRIC_DW_ENDPOINT (Server= in the ODBC connection string)
output "dw_endpoint" {
  value = fabric_warehouse.benchmark_dw.properties.connection_string
}

# → FABRIC_DW_DATABASE (the warehouse display name is the T-SQL database name)
output "dw_database" {
  value = var.dw_name
}

output "harness_client_id" {
  value = azuread_application.harness.client_id
}

output "harness_client_secret" {
  value     = azuread_application_password.harness.value
  sensitive = true
}

output "harness_sp_object_id" {
  value = azuread_service_principal.harness.object_id
}

# Estuary staging storage (for materialize-azure-fabric-warehouse temp files).
# → FABRIC_STAGING_STORAGE_ACCOUNT
output "estuary_storage_account_name" {
  value = azurerm_storage_account.estuary_staging.name
}

# → FABRIC_STAGING_STORAGE_KEY
output "estuary_storage_account_key" {
  value     = azurerm_storage_account.estuary_staging.primary_access_key
  sensitive = true
}

# → FABRIC_STAGING_CONTAINER
output "estuary_storage_container" {
  value = azurerm_storage_container.estuary_staging.name
}

output "tenant_id" {
  value = var.tenant_id
}
