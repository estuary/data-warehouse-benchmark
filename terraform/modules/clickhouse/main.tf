// ClickHouse Cloud module — no-op (same pattern as motherduck).
//
// The ClickHouse provider exposes `clickhouse_service` only as a managed
// resource, not as a data source. We don't want terraform to own the
// "Estuary Benchmark" service (importing it would put it under terraform's
// destroy authority), and we can't read it without managing it. So this
// module manages nothing — it just records the service UUID via a
// `terraform_data` anchor for state symmetry with the other engines.
//
// All connection details go into env/clickhouse.env by hand:
//   * CLICKHOUSE_HOST     — from the ClickHouse Cloud UI (service overview)
//   * CLICKHOUSE_PORT     — from the same overview (HTTPS = 8443)
//   * CLICKHOUSE_USER     — default 'default'
//   * CLICKHOUSE_PASSWORD — copy from the Cloud UI (API doesn't expose it)
//   * CLICKHOUSE_ORG_ID, CLICKHOUSE_SERVICE_ID, CLICKHOUSE_API_KEY_ID /
//     CLICKHOUSE_API_KEY_SECRET: the Cloud API key the size check and
//     scripts/resize/clickhouse_resize.py use

terraform {
  required_version = ">= 1.6.0"
}

variable "service_id" {
  description = "UUID of the pre-existing 'Estuary Benchmark' ClickHouse Cloud service. Recorded here for state symmetry; the harness does not consume it."
  type        = string
}

resource "terraform_data" "config" {
  input = {
    service_id = var.service_id
  }
}

output "service_id" {
  value = var.service_id
}

output "harness_env_vars" {
  description = "Connection env-var names the operator populates in env/clickhouse.env (copy values from the ClickHouse Cloud UI)."
  value = [
    "CLICKHOUSE_HOST",
    "CLICKHOUSE_PORT",
    "CLICKHOUSE_USER",
    "CLICKHOUSE_PASSWORD",
  ]
}
