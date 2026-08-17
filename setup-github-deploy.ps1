# One-time GCP setup for keyless deploys from GitHub Actions.
#
#   .\setup-github-deploy.ps1
#
# Run this once. It is idempotent - re-running reports "already exists" and
# changes nothing.
#
# What it builds, and why it looks like this:
#
# GitHub Actions can prove who it is with a short-lived OIDC token that says
# "I am a workflow running in repository X". Workload Identity Federation
# teaches GCP to trust those tokens and swap them for real credentials. So
# there is no service-account key to download, store in a GitHub secret, leak
# or - as happened here once already - silently corrupt in transit. It is also
# the only route available: this project blocks key downloads by org policy.
#
# Nothing here needs admin on the GitHub repository, which is the whole point.
# The trust is declared on the GCP side and names the repo; GitHub never has to
# be configured to point back.

$ErrorActionPreference = "Stop"

$PROJECT   = "iscale-chatbot-505710"
$NUMBER    = "60075110318"
$REGION    = "asia-south1"
$REPO      = "logixhunt24/iScale-Whatsapp-Chatbot"
$OWNER     = $REPO.Split("/")[0]
$POOL      = "github"
$PROVIDER  = "github-provider"
$SA        = "github-deployer"
$SA_EMAIL  = "${SA}@${PROJECT}.iam.gserviceaccount.com"
$RUNTIME_SA = "${NUMBER}-compute@developer.gserviceaccount.com"

function Step($text) { Write-Host "`n>> $text" -ForegroundColor Cyan }
function Soft($block) { try { & $block } catch { Write-Host "   (already exists)" -ForegroundColor DarkGray } }

Step "Enabling the APIs federation needs"
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com --project=$PROJECT

Step "Creating the deploy service account"
Soft { gcloud iam service-accounts create $SA --display-name="GitHub Actions deployer" --project=$PROJECT 2>$null }

Step "Granting it exactly what a deploy needs"
# run.admin      - update the service and the migration job
# serviceAccountUser - act as the runtime service account when deploying
# artifactregistry.writer - push the image
foreach ($role in @("roles/run.admin", "roles/iam.serviceAccountUser", "roles/artifactregistry.writer")) {
  gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$SA_EMAIL" --role=$role --condition=None --quiet | Out-Null
  Write-Host "   $role"
}

Step "Creating the workload identity pool"
Soft { gcloud iam workload-identity-pools create $POOL --location=global --display-name="GitHub Actions" --project=$PROJECT 2>$null }

Step "Creating the GitHub OIDC provider"
# The attribute-condition is not optional. Without it GCP would trust an OIDC
# token from ANY repository on GitHub - anyone could mint one and deploy here.
# Pinning repository_owner is the minimum; the binding below narrows it to the
# single repository.
Soft {
  gcloud iam workload-identity-pools providers create-oidc $PROVIDER `
    --location=global `
    --workload-identity-pool=$POOL `
    --display-name="GitHub" `
    --issuer-uri="https://token.actions.githubusercontent.com" `
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" `
    --attribute-condition="assertion.repository_owner=='$OWNER'" `
    --project=$PROJECT 2>$null
}

Step "Letting only $REPO impersonate the deployer"
$principal = "principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository/$REPO"
gcloud iam service-accounts add-iam-policy-binding $SA_EMAIL `
  --role=roles/iam.workloadIdentityUser `
  --member=$principal `
  --project=$PROJECT --quiet | Out-Null

Step "Letting the deployer act as the Cloud Run runtime identity"
gcloud iam service-accounts add-iam-policy-binding $RUNTIME_SA `
  --role=roles/iam.serviceAccountUser `
  --member="serviceAccount:$SA_EMAIL" `
  --project=$PROJECT --quiet | Out-Null

$providerPath = "projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER"

Write-Host ""
Write-Host "Done. The workflow file already carries these values:" -ForegroundColor Green
Write-Host ""
Write-Host "  workload_identity_provider: $providerPath"
Write-Host "  service_account:            $SA_EMAIL"
Write-Host ""
Write-Host "Next: commit .github/workflows/deploy.yml and push to main." -ForegroundColor Yellow
Write-Host "Watch it at https://github.com/$REPO/actions"
