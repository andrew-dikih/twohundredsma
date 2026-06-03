<#
.SYNOPSIS
    Provisions Azure infrastructure for a twohundredsma deployment environment.

.DESCRIPTION
    Creates a resource group, ACR, Cosmos DB (serverless), Container Apps
    environment, one Container App (FastAPI on port 8001), a service principal,
    and wires up all GitHub Actions secrets and variables. Optimised for low
    cost: serverless Cosmos, Basic ACR. Replicas are pinned to 1/1 because the
    advisor runs background jobs in-process and ACA scale-to-zero would kill
    them mid-run.

    Adapted from the azure-container-apps-provision skill for the single-app
    twohundredsma layout.

.PARAMETER EnvironmentName
    Short name for the environment, e.g. "develop", "production". Used to derive
    all resource names.

.PARAMETER SubscriptionId
    Azure subscription ID to deploy into.

.PARAMETER Location
    Azure region, e.g. "westus3", "eastus".

.PARAMETER GitHubRepo
    GitHub repository in "owner/repo" format, e.g. "andrew-dikih/twohundredsma".

.PARAMETER AdminUsername
    Username to grant admin role to on first registration. Stored as the
    ADMIN_USERNAME env var on the Container App. Default: "andrewdikih".

.EXAMPLE
    .\provision-azure.ps1 `
        -EnvironmentName develop `
        -SubscriptionId  "7892b358-b4c1-416f-8b7d-e051911108cf" `
        -Location        westus3 `
        -GitHubRepo      "andrew-dikih/twohundredsma"
#>
param(
    [Parameter(Mandatory)][string] $EnvironmentName,
    [Parameter(Mandatory)][string] $SubscriptionId,
    [Parameter(Mandatory)][string] $Location,
    [Parameter(Mandatory)][string] $GitHubRepo,

    [string] $AdminUsername        = "andrewdikih",
    [string] $ResourceGroupName    = "twohundredsma-$EnvironmentName",
    [string] $AcrName              = "twohundredsma$EnvironmentName",
    [string] $CosmosAccountName    = "twohundredsma-$EnvironmentName-cosmos",
    [string] $CosmosDatabaseName   = "TwoHundredSMA",
    [string] $ContainerAppsEnvName = "twohundredsma-$EnvironmentName-env",
    [string] $AppName              = "twohundredsma-$EnvironmentName-app",
    [string] $SpName               = "twohundredsma-$EnvironmentName-deploy"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$az = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
if (-not (Test-Path $az)) { $az = "az" }

function Invoke-Az {
    & $az @args
    if ($LASTEXITCODE -ne 0) { throw "az command failed (exit $LASTEXITCODE): az $args" }
}

Write-Host "`n=== twohundredsma Azure Provisioning ===" -ForegroundColor Cyan
Write-Host "Environment   : $EnvironmentName"
Write-Host "Subscription  : $SubscriptionId"
Write-Host "Location      : $Location"
Write-Host "GitHub Repo   : $GitHubRepo"
Write-Host "Admin Username: $AdminUsername`n"

# 1. Register required providers ------------------------------------------------
Write-Host "[ 1/9 ] Registering Azure providers..." -ForegroundColor Yellow
foreach ($ns in @("Microsoft.DocumentDB", "Microsoft.App", "Microsoft.OperationalInsights")) {
    $state = Invoke-Az provider show --namespace $ns --subscription $SubscriptionId --query registrationState -o tsv
    if ($state -ne "Registered") {
        Write-Host "  Registering $ns..."
        Invoke-Az provider register --namespace $ns --subscription $SubscriptionId
        do {
            Start-Sleep 10
            $state = Invoke-Az provider show --namespace $ns --subscription $SubscriptionId --query registrationState -o tsv
            Write-Host "  $ns : $state"
        } while ($state -ne "Registered")
    } else {
        Write-Host "  $ns already registered."
    }
}

# 2. Resource group -------------------------------------------------------------
Write-Host "`n[ 2/9 ] Creating resource group '$ResourceGroupName'..." -ForegroundColor Yellow
Invoke-Az group create --name $ResourceGroupName --location $Location --subscription $SubscriptionId --output none

# 3. Azure Container Registry ---------------------------------------------------
Write-Host "`n[ 3/9 ] Creating Container Registry '$AcrName'..." -ForegroundColor Yellow
Invoke-Az acr create --name $AcrName --resource-group $ResourceGroupName --sku Basic `
    --admin-enabled true --location $Location --subscription $SubscriptionId --output none

# 4. Cosmos DB account (serverless) --------------------------------------------
Write-Host "`n[ 4/9 ] Creating Cosmos DB account '$CosmosAccountName' (serverless)..." -ForegroundColor Yellow
Invoke-Az cosmosdb create --name $CosmosAccountName --resource-group $ResourceGroupName `
    --locations regionName=$Location --capabilities EnableServerless `
    --subscription $SubscriptionId --output none

Write-Host "  Creating database '$CosmosDatabaseName'..."
Invoke-Az cosmosdb sql database create --account-name $CosmosAccountName `
    --resource-group $ResourceGroupName --name $CosmosDatabaseName `
    --subscription $SubscriptionId --output none

# `--ttl -1` enables TTL on the container with no default expiration; per-item
# `ttl` fields (sessions, ai_usage) then take effect.
Write-Host "  Creating container 'Documents' (pk: /documentType, ttl enabled)..."
Invoke-Az cosmosdb sql container create --account-name $CosmosAccountName `
    --resource-group $ResourceGroupName --database-name $CosmosDatabaseName `
    --name Documents --partition-key-path /documentType --ttl -1 `
    --subscription $SubscriptionId --output none

# 5. Container Apps environment -------------------------------------------------
Write-Host "`n[ 5/9 ] Creating Container Apps environment '$ContainerAppsEnvName'..." -ForegroundColor Yellow
Invoke-Az containerapp env create --name $ContainerAppsEnvName --resource-group $ResourceGroupName `
    --location $Location --subscription $SubscriptionId --output none

# 6. Container App --------------------------------------------------------------
# min-replicas=1/max-replicas=1 because the advisor runs jobs in-process and
# scale-to-zero would interrupt them. Single-app, single-replica also means no
# cross-replica state to worry about (in-memory portfolio cache stays valid).
$placeholder = "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest"

Write-Host "`n[ 6/9 ] Creating Container App '$AppName' (port 8001, min/max=1)..." -ForegroundColor Yellow
$appFqdn = Invoke-Az containerapp create `
    --name $AppName --resource-group $ResourceGroupName --environment $ContainerAppsEnvName `
    --image $placeholder --target-port 8001 --ingress external `
    --min-replicas 1 --max-replicas 1 --cpu 0.5 --memory 1.0Gi `
    --subscription $SubscriptionId --query "properties.configuration.ingress.fqdn" -o tsv

# Force single-revision mode so deployments cannot run two replicas in parallel
# (which would let two workers race and corrupt the in-memory job state).
Write-Host "  Setting single-revision mode..."
Invoke-Az containerapp revision set-mode --name $AppName --resource-group $ResourceGroupName `
    --mode single --subscription $SubscriptionId --output none

# 7. Service principal ----------------------------------------------------------
Write-Host "`n[ 7/9 ] Creating service principal '$SpName'..." -ForegroundColor Yellow
$scope = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName"
$spJson = Invoke-Az ad sp create-for-rbac --name $SpName --role contributor `
    --scopes $scope --json-auth --output json | ConvertFrom-Json

# 8. Wire up secrets on the Container App --------------------------------------
Write-Host "`n[ 8/9 ] Configuring Container App secrets + env vars..." -ForegroundColor Yellow

$cosmosKey  = Invoke-Az cosmosdb keys list --name $CosmosAccountName `
    --resource-group $ResourceGroupName --subscription $SubscriptionId `
    --query "primaryMasterKey" -o tsv
$cosmosConn = "AccountEndpoint=https://$CosmosAccountName.documents.azure.com:443/;AccountKey=$cosmosKey"
$acrPwd     = Invoke-Az acr credential show --name $AcrName `
    --subscription $SubscriptionId --query "passwords[0].value" -o tsv

# Note: the actual image deploy happens in the GitHub Actions workflow. Here we
# only seed the env vars + secret references so the first deploy can rely on
# them. ADMIN_USERNAME is a plain env var so it can be changed without a
# re-provision; the Cosmos connection string is a secret.
Invoke-Az containerapp secret set --name $AppName --resource-group $ResourceGroupName `
    --secrets "cosmos-connection-string=$cosmosConn" `
    --subscription $SubscriptionId --output none

Invoke-Az containerapp update --name $AppName --resource-group $ResourceGroupName `
    --set-env-vars `
        "COSMOS_CONNECTION_STRING=secretref:cosmos-connection-string" `
        "COSMOS_DATABASE=$CosmosDatabaseName" `
        "COSMOS_CONTAINER=Documents" `
        "ADMIN_USERNAME=$AdminUsername" `
    --subscription $SubscriptionId --output none

# 9. GitHub secrets & variables -------------------------------------------------
Write-Host "`n[ 9/9 ] Configuring GitHub Actions secrets and variables..." -ForegroundColor Yellow

$azureCreds = $spJson | ConvertTo-Json -Compress

# Secrets
$azureCreds | gh secret set AZURE_CREDENTIALS         --repo $GitHubRepo
$cosmosConn | gh secret set COSMOS_CONNECTION_STRING  --repo $GitHubRepo
$acrPwd     | gh secret set AZURE_REGISTRY_PASSWORD   --repo $GitHubRepo

# Variables
gh variable set AZURE_RESOURCE_GROUP    --body $ResourceGroupName    --repo $GitHubRepo
gh variable set AZURE_REGISTRY_URL      --body "$AcrName.azurecr.io" --repo $GitHubRepo
gh variable set AZURE_REGISTRY_USERNAME --body $AcrName              --repo $GitHubRepo
gh variable set AZURE_SUBSCRIPTION_ID   --body $SubscriptionId       --repo $GitHubRepo
gh variable set AZURE_APP_NAME          --body $AppName              --repo $GitHubRepo
gh variable set AZURE_COSMOS_DATABASE   --body $CosmosDatabaseName   --repo $GitHubRepo
gh variable set ADMIN_USERNAME          --body $AdminUsername        --repo $GitHubRepo

# Summary -----------------------------------------------------------------------
Write-Host "`n=== Provisioning Complete ===" -ForegroundColor Green
Write-Host "Resource Group : $ResourceGroupName"
Write-Host "ACR            : $AcrName.azurecr.io"
Write-Host "Cosmos DB      : $CosmosAccountName  (database: $CosmosDatabaseName)"
Write-Host "Container App  : $AppName"
Write-Host "App URL        : https://$appFqdn"
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Push to the 'develop' branch to trigger the deploy workflow."
Write-Host "  2. After first deploy, visit https://$appFqdn and register '$AdminUsername' as the admin."
Write-Host "  3. (Optional) Add a custom domain via 'az containerapp hostname add'."
