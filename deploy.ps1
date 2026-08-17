# Deploy the bot to Cloud Run.
#
#   .\deploy.ps1                            deploy the current image
#   .\deploy.ps1 -Tag v2 -Build             build, push, then deploy
#   .\deploy.ps1 -Tag v2 -Build -Migrate    ...running migrations first
#
# Run from the repository root, so env.yaml resolves.
#
# All configuration lives in env.yaml - one file, one command. That file holds
# live credentials and is gitignored; if you ever need it on another machine,
# copy it across by hand rather than committing it.

param(
  [string]$Tag = "v1",
  [switch]$Build,
  [switch]$Migrate
)

$ErrorActionPreference = "Stop"

$PROJECT = "iscale-chatbot-505710"
$REGION  = "asia-south1"
# Braces, not bare $VAR: PowerShell reads "$PROJECT:" as a scope qualifier and
# silently builds the wrong string.
$INSTANCE = "${PROJECT}:${REGION}:iscale-db"
$IMAGE    = "${REGION}-docker.pkg.dev/${PROJECT}/iscale/bot:${Tag}"

if (-not (Test-Path "env.yaml")) {
  throw "env.yaml not found - run this from the repository root"
}
if ((Get-Content "env.yaml" -Raw) -match "REPLACE_WITH_CLOUDSQL_PASSWORD") {
  throw "env.yaml still has the DATABASE_URL placeholder - set the real password first"
}
if (-not ((Get-Content "env.yaml" -Raw) -match "(?m)^GOOGLE_SERVICE_ACCOUNT_JSON:")) {
  Write-Host "WARNING: no GOOGLE_SERVICE_ACCOUNT_JSON in env.yaml." -ForegroundColor Yellow
  Write-Host "         Sheets sync will be off. Run .\add-service-account.ps1 to add it."
  Write-Host ""
}

if ($Build) {
  Write-Host "Building $IMAGE ..." -ForegroundColor Cyan
  gcloud builds submit --tag $IMAGE
  if ($LASTEXITCODE -ne 0) { throw "build failed" }
}

if ($Migrate) {
  # Migrations first, then the service: the new code expects the new schema,
  # and the old code tolerates columns it does not know about.
  Write-Host "Running migrations ..." -ForegroundColor Cyan
  gcloud run jobs update iscale-migrate --image=$IMAGE --region=$REGION
  gcloud run jobs execute iscale-migrate --region=$REGION --wait
  if ($LASTEXITCODE -ne 0) { throw "migration failed" }
}

Write-Host "Deploying $IMAGE ..." -ForegroundColor Cyan

# Every scaling flag here is load-bearing:
#
#   --no-cpu-throttling  The webhook answers Meta and THEN does the work, and
#                        the inactivity sweeper is a long-lived async loop.
#                        Cloud Run's default allocates CPU only during a
#                        request, which freezes both - the bot would accept
#                        every message and answer none.
#   --min-instances=1    Nothing runs the sweeper if the service scales to zero.
#   --max-instances=1    The console broadcaster, the rate limiters and the
#                        sweeper are all per-process. A second instance means
#                        agents miss live updates and quiet customers get two
#                        nudges instead of one.
#   --timeout=3600       The console's live-update WebSocket is a long request.
gcloud run deploy iscale-bot `
  --image=$IMAGE `
  --region=$REGION `
  --allow-unauthenticated `
  --add-cloudsql-instances=$INSTANCE `
  --no-cpu-throttling `
  --min-instances=1 `
  --max-instances=1 `
  --cpu=1 `
  --memory=512Mi `
  --timeout=3600 `
  --env-vars-file=env.yaml

if ($LASTEXITCODE -ne 0) { throw "deploy failed" }

$url = gcloud run services describe iscale-bot --region=$REGION --format="value(status.url)"

Write-Host ""
Write-Host "Deployed: $url" -ForegroundColor Green
Write-Host ""
Write-Host "Check, in this order:" -ForegroundColor Yellow
Write-Host "  curl $url/api/v1/health"
Write-Host "  curl $url/api/v1/health/ready"
Write-Host "  gcloud run services logs read iscale-bot --region=$REGION --limit=50"
Write-Host ""
Write-Host "'Inactivity follow-up active' in the logs is the line that proves"
Write-Host "background work survived the move - the whole point of the CPU flag."
