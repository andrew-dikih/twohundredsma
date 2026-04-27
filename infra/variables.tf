variable "location" {
  description = "Azure region to deploy resources into."
  type        = string
  default     = "eastus"
}

variable "prefix" {
  description = "Short prefix used to name every resource (lowercase, no spaces)."
  type        = string
  default     = "twohundredsma"
}

variable "law_daily_quota_gb" {
  description = <<-EOT
    Maximum GB ingested per day into the Log Analytics Workspace before ingestion
    is halted for that day. Set to -1 to disable the cap. Keeping this low is the
    primary cost-control lever for Log Analytics.
  EOT
  type        = number
  default     = 0.5 # 500 MB/day – generous for a small app, cheap at ~$0.10/day
}

variable "appinsights_daily_cap_gb" {
  description = <<-EOT
    Maximum GB ingested per day into Application Insights before sampling kicks in
    and a warning email is sent. Set to 0 to disable. This caps unexpected cost
    spikes from high-traffic events.
  EOT
  type        = number
  default     = 0.1 # 100 MB/day
}

variable "log_retention_days" {
  description = <<-EOT
    Interactive (hot) retention for the Application Insights tables inside the
    Log Analytics Workspace. Minimum value is 4 days. Keeping this very low
    minimises the storage cost of retained data.
  EOT
  type        = number
  default     = 4

  validation {
    condition     = var.log_retention_days >= 4 && var.log_retention_days <= 730
    error_message = "log_retention_days must be between 4 and 730."
  }
}
