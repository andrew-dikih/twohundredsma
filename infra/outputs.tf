output "application_insights_connection_string" {
  description = <<-EOT
    Set this as the APPLICATIONINSIGHTS_CONNECTION_STRING environment variable
    in your app (or in docker-compose.yml / App Service / Container App
    environment settings) to enable telemetry.

    Keep this value secret – it grants write access to your Application
    Insights resource.  Store it in Azure Key Vault or a CI/CD secret, never
    in source code.
  EOT
  value     = azurerm_application_insights.main.connection_string
  sensitive = true
}

output "application_insights_instrumentation_key" {
  description = "Legacy instrumentation key – prefer connection_string for new deployments."
  value       = azurerm_application_insights.main.instrumentation_key
  sensitive   = true
}

output "log_analytics_workspace_id" {
  description = "Resource ID of the Log Analytics Workspace."
  value       = azurerm_log_analytics_workspace.main.id
}

output "resource_group_name" {
  description = "Name of the resource group that contains all monitoring resources."
  value       = azurerm_resource_group.main.name
}
