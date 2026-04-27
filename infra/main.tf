# ---------------------------------------------------------------------------
# Resource Group
# ---------------------------------------------------------------------------
resource "azurerm_resource_group" "main" {
  name     = "${var.prefix}-rg"
  location = var.location
}

# ---------------------------------------------------------------------------
# Log Analytics Workspace
# Pricing: PerGB2018 (pay per GB ingested) – cheapest option for variable
# or low-volume workloads; no upfront commitment.
# ---------------------------------------------------------------------------
resource "azurerm_log_analytics_workspace" "main" {
  name                = "${var.prefix}-law"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  sku = "PerGB2018"

  # Workspace-level default retention (30 days is the minimum for PerGB2018).
  # Per-table overrides below bring the Application Insights tables down to
  # var.log_retention_days (default 4 days) which satisfies the <7-day goal.
  retention_in_days = 30

  # Hard cap on daily ingestion to prevent surprise bills.
  daily_quota_gb = var.law_daily_quota_gb
}

# ---------------------------------------------------------------------------
# Per-table retention overrides
# Application Insights writes to these tables inside the workspace.
# Setting each to 4 days (the Azure minimum) keeps storage costs minimal.
# ---------------------------------------------------------------------------
locals {
  appinsights_tables = [
    "AppTraces",
    "AppRequests",
    "AppExceptions",
    "AppDependencies",
    "AppPageViews",
    "AppMetrics",
    "AppBrowserTimings",
    "AppEvents",
    "AppAvailabilityResults",
    "AppPerformanceCounters",
    "AppSystemEvents",
  ]
}

resource "azurerm_log_analytics_workspace_table" "appinsights" {
  for_each = toset(local.appinsights_tables)

  workspace_id    = azurerm_log_analytics_workspace.main.id
  name            = each.key
  retention_in_days = var.log_retention_days
}

# ---------------------------------------------------------------------------
# Application Insights (workspace-based)
# Linking to the Log Analytics Workspace means all telemetry lands in the
# LAW, where the per-table retention rules above apply.  Classic (non-
# workspace) Application Insights is being retired by Microsoft.
# ---------------------------------------------------------------------------
resource "azurerm_application_insights" "main" {
  name                = "${var.prefix}-appinsights"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  workspace_id        = azurerm_log_analytics_workspace.main.id

  application_type = "web"

  # Ingestion daily cap (Application Insights layer).  Data above this limit
  # is dropped on the same day; a notification email is sent to subscription
  # owners.  Combined with the LAW daily_quota_gb this gives two independent
  # cost guardrails.
  daily_data_cap_in_gb                  = var.appinsights_daily_cap_gb
  daily_data_cap_notifications_disabled = false

  # Do NOT store sampled data for longer than the LAW table retention.
  retention_in_days = 30 # must be >= the workspace default

  # Disable IP address storage to reduce PII footprint.
  disable_ip_masking = false
}
