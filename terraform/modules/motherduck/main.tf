// MotherDuck module — there's no first-party Terraform provider, so this
// module manages what *can* be templated: a placeholder ``terraform_data``
// resource that holds the operator-supplied database name + tier, and
// outputs them with the name of the token env var the harness reads.
//
// The MotherDuck token itself is provisioned by the operator out-of-band
// (https://app.motherduck.com — Settings → Tokens) and pasted into
// ``env/motherduck.env`` (MOTHERDUCK_TOKEN). Terraform deliberately does NOT manage the
// token to avoid the credential ever landing in state.

terraform {
  required_version = ">= 1.6.0"
}

variable "database_name" {
  description = "MotherDuck database the harness uses (created on first connect)."
  type        = string
  default     = "benchmark_db"
}

variable "tier" {
  description = <<-EOT
    MotherDuck Duckling size (pulse | standard | jumbo | mega | giga). Template
    value only — MotherDuck has no first-party Terraform provider, so the actual
    Duckling size is set out-of-band via the MotherDuck UI (Settings → Ducklings)
    or the REST API (PUT /v1/users/{user}/instances). Pulse is auto-scaling and
    metered per-query; the others bill for wall-clock time.
  EOT
  type        = string
  default     = "standard"

  validation {
    condition     = contains(["pulse", "standard", "jumbo", "mega", "giga"], var.tier)
    error_message = "tier must be one of: pulse, standard, jumbo, mega, giga."
  }
}

resource "terraform_data" "config" {
  input = {
    database = var.database_name
    tier     = var.tier
  }
}

output "database_name" {
  value = var.database_name
}

output "tier" {
  value = var.tier
}

output "harness_token_env_var" {
  description = "Env-var name the harness reads to obtain the MotherDuck token."
  value       = "MOTHERDUCK_TOKEN"
}
