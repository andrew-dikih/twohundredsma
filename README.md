# twohundredsma

A FastAPI-based portfolio advisor that rebalances around the 200-week simple
moving average of the S&P 500. Runs locally in Docker against SQLite or in
Azure Container Apps against Cosmos DB.

## Local development

```bash
docker compose up -d --build portfolio_advisor
# open http://localhost:8001
```

The advisor uses SQLite at `/data/advisor.db` (mounted volume `advisor_data`)
when `COSMOS_CONNECTION_STRING` is unset. First user registered with the
`ADMIN_USERNAME` (default `andrewdikih`) becomes the admin.

## Deploying to Azure

The repository is wired for Azure Container Apps with Cosmos DB serverless.
Both backends are implemented behind the `advisor.state.Store` factory: set
`COSMOS_CONNECTION_STRING` and the app uses Cosmos; leave it unset and it
falls back to SQLite. The local docker-compose service leaves it unset; the
Container App sets it to a secret reference.

### One-time per environment: provision

```powershell
scripts\provision-azure.ps1 `
  -EnvironmentName develop `
  -SubscriptionId  "<your-azure-sub-id>" `
  -Location        westus3 `
  -GitHubRepo      "andrew-dikih/twohundredsma"
```

This creates the resource group, ACR (Basic), Cosmos DB (serverless, single
`Documents` container partitioned by `/documentType`, TTL enabled), the
Container Apps environment, a single Container App on port 8001
(min/max-replicas=1, single-revision mode), a service principal scoped to the
resource group, and seeds the GitHub Actions secrets and variables listed
below.

The advisor runs background jobs in-process, so replicas are pinned to 1/1
(scale-to-zero would interrupt running advisor jobs and split brain on the
in-memory portfolio cache).

### Continuous deployment

`.github/workflows/deploy.yml` triggers on push to `develop`:
1. Builds the image from `dockerfile` and pushes to ACR
   (`<acr>/twohundredsma:<sha>` and `:latest`).
2. Logs into Azure via `AZURE_CREDENTIALS`.
3. Syncs `cosmos-connection-string` (and optional `app-github-token`) into the
   Container App's secret store.
4. Updates the Container App with the new image and the env vars
   `COSMOS_CONNECTION_STRING`, `COSMOS_DATABASE`, `COSMOS_CONTAINER`,
   `ADMIN_USERNAME`, `AI_REVIEW_MODEL`, and (if configured) `GITHUB_TOKEN`.

`.github/workflows/ci.yml` runs on PRs to `main` or `develop`: byte-compiles
the `advisor/` package, smoke-imports every module, and verifies the Docker
image builds.

### GitHub repository configuration

The provisioning script writes these to the repository:

| Kind     | Name                       | Purpose                                          |
| -------- | -------------------------- | ------------------------------------------------ |
| Secret   | `AZURE_CREDENTIALS`        | Service principal JSON for `azure/login`         |
| Secret   | `COSMOS_CONNECTION_STRING` | Cosmos DB account connection string              |
| Secret   | `AZURE_REGISTRY_PASSWORD`  | ACR admin password                               |
| Variable | `AZURE_RESOURCE_GROUP`     | RG containing the Container App + Cosmos         |
| Variable | `AZURE_REGISTRY_URL`       | `<acr>.azurecr.io`                               |
| Variable | `AZURE_REGISTRY_USERNAME`  | ACR admin username (= ACR name)                  |
| Variable | `AZURE_SUBSCRIPTION_ID`    | Subscription deployed into                       |
| Variable | `AZURE_APP_NAME`           | Container App name                               |
| Variable | `AZURE_COSMOS_DATABASE`    | Cosmos database name (default `TwoHundredSMA`)   |
| Variable | `ADMIN_USERNAME`           | Username that becomes admin on first registration |

Optional, set manually if you want the deployed app to use them:

| Kind     | Name               | Purpose                                          |
| -------- | ------------------ | ------------------------------------------------ |
| Secret   | `APP_GITHUB_TOKEN` | GitHub token the AI review uses to fetch PRs/files |
| Variable | `AI_REVIEW_MODEL`  | OpenAI model id (default `openai/gpt-4.1`)       |

### Storage layout in Cosmos

Single container `Documents` partitioned by `/documentType`. Document types:
`user`, `session`, `user_account`, `ai_usage`, `run`, `job`, `contribution`,
`setting`, `counter`. See `advisor/store_cosmos.py` for the full schema and
the counter-document pattern used to allocate sequential integer IDs.

Container-level TTL is enabled (default `-1`, "items with explicit TTL only");
sessions and ai-usage rows set per-item `ttl` so they self-evict.
