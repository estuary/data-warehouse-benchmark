# Infrastructure Provisioning (`terraform/`)

Terraform modules for all 7 benchmark engines plus shared resources.

## Structure

```
terraform/
├── main.tf                  Root orchestration (wires all modules; aggregated outputs)
├── variables.tf             Root variables (engine triggers default to "" / false)
├── terraform.tfvars.example Example variable values
└── modules/
    ├── shared/main.tf       S3 staging bucket + Estuary IAM user (always created)
    ├── snowflake/main.tf    BENCHMARK_DB + per-suite, per-scale schemas, five
                             Gen2 warehouses (X-Small to X-Large), BENCHMARK_ROLE,
                             BENCHMARK_USER (key-pair service user), optional
                             network policy
    ├── bigquery/main.tf     Per-suite, per-scale datasets, Enterprise reservation
                             (opt-in), GCS staging bucket, runner + Estuary
                             service accounts
    ├── databricks_azure/main.tf  Azure Databricks workspace (premium) + Unity
                             Catalog access connector + ADLS Gen2 catalog
                             storage. ARM layer only — the workspace contents
                             live in databricks/. Two-stage bring-up (targeted
                             apply); see "Azure Databricks bring-up" below.
    ├── databricks/main.tf   Databricks workspace contents: UC catalog +
                             per-scale schemas + serverless SQL warehouse
                             (pinned to one cluster), plus (Azure path) the
                             storage credential / external location and a
                             minted harness PAT
    ├── redshift/main.tf     Redshift Serverless namespace + workgroup (pinned:
                             max RPU = base RPU) + inbound security group +
                             S3-read IAM role
    ├── fabric/main.tf       Azure Fabric capacity + workspace + Data Warehouse,
                             the service principal the harness and Estuary use,
                             and the Estuary staging storage account
    ├── clickhouse/main.tf   No-op module — records the pre-existing service UUID
                             via `terraform_data` for state symmetry. No
                             provider, no resources; connection details are
                             copied from the ClickHouse Cloud UI into
                             `env/clickhouse.env`.
    ├── motherduck/main.tf   Records the database name + Duckling tier
                             (always enabled; no provider, no real resources)
    └── datagen/main.tf      Opt-in EC2 box (default m8i.8xlarge + gp3 /data
                             volume) for TPC-DS/DSB generation and in-region
                             S3 staging at SF100+ — native Linux dsdgen, no
                             Docker VM disk ceiling. Instance role is scoped
                             to the staging bucket; access via SSM Session
                             Manager (SSH opt-in via key + CIDR). Enable with
                             `datagen_enabled = true`, tear down by flipping
                             it back — the box is stateless.
```

## Opt-In Activation

Each engine module uses conditional instantiation. Set the relevant variable to a non-empty string to enable it:

| Engine     | Activation Variable         | Default            |
|------------|-----------------------------|--------------------|
| Snowflake  | `snowflake_account`         | `""` (disabled)    |
| BigQuery   | `gcp_project_id`            | `""` (disabled)    |
| Databricks (Azure workspace) | `databricks_azure_enabled` | `false` (disabled) |
| Databricks (workspace contents) | `databricks_host`     | `""` (disabled)    |
| Redshift   | `redshift_enabled`          | `false` (disabled) |
| Fabric     | `fabric_subscription_id` (+ `fabric_enabled`) | `""` (disabled) |
| ClickHouse | `clickhouse_service_id`     | `""` (disabled)    |
| MotherDuck | none                        | Always enabled     |
| Datagen EC2 | `datagen_enabled`          | `false` (disabled) |

## Usage

```bash
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with credentials for engines you want to provision
terraform init -backend-config="bucket=<your-gcs-state-bucket>"
terraform plan
terraform apply
uv run benchmark export-terraform-outputs   # writes terraform/outputs.json + the SA-key files
```

Per-scale compute comes from `src/benchmark/run_plan.yaml`, not from
`terraform.tfvars`: `uv run benchmark prepare-scale --scale N` writes the
targets (`redshift_baseline_rpu`, `databricks_warehouse_size`,
`fabric_capacity_sku`, `bigquery_reservation_slot_capacity`) to
`sizing.auto.tfvars.json` (git-ignored), which terraform loads after
`terraform.tfvars`, and prints the `terraform plan` / `apply` to run. The
harness never runs `terraform apply` itself. Snowflake needs no sizing var (all
five warehouses exist), and ClickHouse and MotherDuck are resized out of band
(`scripts/resize/`).

## Azure Databricks bring-up (two-stage, targeted apply)

The databricks provider can't be configured until the workspace URL exists, so
the Azure path splits ARM resources (`modules/databricks_azure`) from workspace
contents (`modules/databricks`): same shape as the Fabric capacity bring-up.

**Prerequisites:** `fabric_subscription_id` + `fabric_tenant_id` set (the
shared root Azure providers; set `fabric_enabled = false` if you don't also
want the Fabric capacity), and `az login` against that tenant. The identity
running terraform needs to create role assignments on the catalog storage
account (Owner or User Access Administrator on the subscription/RG: plain
Contributor is not enough).

```bash
# Stage 1 — ARM layer only: workspace, UC access connector, catalog storage.
# (Quote the target so zsh doesn't glob the brackets when count is involved.)
terraform apply -target='module.databricks_azure'

# Stage 2 — point the workspace-contents module at the new workspace:
terraform output -raw databricks_azure_workspace_url
#   → paste into terraform.tfvars as databricks_host; leave databricks_token
#     empty (the provider authenticates off az login and mints the harness PAT)
terraform apply

# Capture the connection details for env/databricks.env:
terraform output -raw databricks_azure_workspace_url   # → DATABRICKS_HOST (strip https://)
terraform output -raw databricks_harness_token         # → DATABRICKS_TOKEN
terraform output -raw databricks_sql_http_path         # → DATABRICKS_SQL_HTTP_PATH
terraform output -raw databricks_catalog               # → DATABRICKS_CATALOG
```

Then continue with the readiness checks (`uv run benchmark
export-terraform-outputs`, `connectivity-check --platforms databricks`,
`capture-cost-config --platforms databricks`) and publish the Estuary
materialization (`flow-publish databricks`): the connector's PAT auth and the
`materialize-databricks` staging volumes work unchanged against Azure.

Stage 2 builds the Unity Catalog plumbing automatically: Azure's
auto-provisioned regional metastore has no root storage, so the catalog is
rooted in the module's ADLS Gen2 container via a storage credential (the
access connector's managed identity) + external location. If the workspace
comes up without a metastore attached (region without automatic UC
enablement: rare), attach one in the Databricks account console before
stage 2.

**Repointing from an existing workspace:** if `module.databricks` state was
applied against a previous (e.g. AWS) workspace, drop it before changing
`databricks_host` so terraform doesn't try to reconcile old resources against
the new workspace: `terraform state rm 'module.databricks[0]'`.

## Teardown (keep the S3 staging bucket)

The staging bucket (`var.staging_bucket_name`; ~2.5 TB of TPC-DS / DSB /
ClickBench Parquet plus the Estuary collection captures) outlives the engines.
A plain `terraform destroy` would try to delete it (and fail, because
`staging_force_destroy = false` and the bucket is non-empty), so tear the
engines down with a **targeted destroy that excludes `module.shared`**. This
keeps the bucket, its versioning/encryption/public-access-block config, and the
Estuary reader IAM user + access key under Terraform management, so any capture
still pointed at S3 keeps working.

```bash
# 1. The BigQuery module's GCS bucket (Estuary's materializer scratch space)
#    is created with force_destroy = false. The provider reads that flag from
#    STATE at delete time, so flipping the tfvar alone does nothing: apply the
#    attribute change in place first.
terraform apply -var bigquery_staging_force_destroy=true \
  -target='module.bigquery[0].google_storage_bucket.estuary_staging'

# 2. Destroy every engine module. Modules with count = 0 in the current tfvars
#    (redshift_enabled / datagen_enabled = false) are still matched if their
#    instances linger in state.
terraform destroy -var bigquery_staging_force_destroy=true \
  -target=module.bigquery -target=module.snowflake \
  -target=module.databricks -target=module.databricks_azure \
  -target=module.redshift -target=module.fabric \
  -target=module.motherduck -target=module.datagen

# 3. Verify: only module.shared.* should remain, and the bucket must still list.
terraform state list
aws s3 ls s3://<staging-bucket>/ --profile benchmark
```

Notes:

- The engine modules set `delete_contents_on_destroy` (BigQuery datasets) and
  `force_destroy` (Databricks catalog, schemas, UC storage credential and
  external location; GCS bucket via the tfvar above) so materialised tables
  and their managed storage don't block the destroy. These flags are read
  from **state** at delete time: if a resource predates them, apply the
  attribute change in place (as in step 1) before destroying.
- `azurerm_resource_provider_registration.fabric` (module.fabric) may fail to
  unregister with a 409 while any `Microsoft.Fabric` resource still exists in
  the subscription. Leaving the namespace registered is harmless; drop the
  record with `terraform state rm` and move on.
- Every provider must authenticate for the plan to build: `aws_profile`
  credentials, `gcloud` ADC for BigQuery, the Snowflake admin key pair, and a
  fresh `az login` for the azurerm / azapi / azuread / fabric providers and for
  Databricks when `databricks_token` is empty (azure-cli auth).
- The Fabric warehouse's result-set cache is not under Terraform authority either: the `fabric` provider has no field for it, and a newly created warehouse can come up with it on. After `module.fabric` creates the warehouse, run `ALTER DATABASE CURRENT SET RESULT_SET_CACHING OFF` against it once as a warehouse admin; the Fabric connector refuses to connect until you do.
- Redshift's system-table export to S3 Tables (`sys_query_history`, enabled with `aws redshift-serverless update-namespace --log-destination-type s3table …`, AWS CLI 2.35.22 or later) is not under Terraform authority: the `hashicorp/aws` provider and CloudFormation have no field for it yet. Destroying the namespace ends it (data already delivered stays in the `aws-redshift` table bucket); re-enable it after re-creating the namespace.
- ClickHouse Cloud is not under Terraform authority (`modules/clickhouse` is a
  no-op); stop or delete the service in the Cloud UI. MotherDuck's module only
  records config; the database and Duckling tier are managed via the MotherDuck
  UI / REST API.
- A later `terraform apply` re-creates the destroyed engines against the
  surviving bucket; nothing needs importing.
- Use `caffeinate -i terraform destroy ...` on macOS: a full destroy runs for
  tens of minutes (Fabric capacity and the Databricks workspace are the slow
  ones).
