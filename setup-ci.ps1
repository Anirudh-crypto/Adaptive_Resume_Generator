#!/usr/bin/env powershell
# ^ A no-op comment to PowerShell, but Git Bash honours it -- so `./setup-ci.ps1` hands itself to
# powershell.exe instead of failing with `syntax error near unexpected token '('` when someone
# runs it from the wrong shell. This script is Windows-oriented anyway (it falls back to the
# LOCALAPPDATA Cloud SDK path, same as deploy.ps1).
#
# One-time setup so GitHub Actions can deploy to Cloud Run without any long-lived credential.
#
# Run this ONCE, from this directory, signed in as a project Owner:  ./setup-ci.ps1 -GitHubRepo "you/resume_builder"
#
# It creates:
#   * two service accounts, split by blast radius --
#       github-deployer         : what GitHub impersonates. Pushes images, rolls revisions,
#                                 shifts traffic. Cannot read your API keys.
#       resume-builder-runtime  : what the container runs as. Reads exactly two secrets.
#     Without a dedicated runtime SA, Cloud Run falls back to the default compute service
#     account, which holds project Editor -- and CI would need actAs on an Editor identity.
#   * a Workload Identity Federation pool + GitHub OIDC provider, restricted to your repo.
#   * the minimum IAM bindings, and nothing else.
#
# It prints the `gh secret set` / `gh variable set` commands to run afterwards.
#
# Note: deliberately NOT using $ErrorActionPreference = "Stop" -- gcloud is a native exe that
# prints routine info to stderr, and PowerShell 5.1 treats that as terminating under Stop even on
# success. Real failures are caught with explicit $LASTEXITCODE checks instead (same approach as
# deploy.ps1).

param(
    # Exactly as GitHub created it, including capitalisation -- a casing mismatch makes the
    # attribute condition silently never match, which surfaces much later as an opaque
    # "Unable to acquire impersonated credentials".
    [Parameter(Mandatory = $true)]
    [string]$GitHubRepo,

    [string]$ProjectId = "resume-builder-503517",
    [string]$Region = "us-central1",
    [string]$Repo = "resume-builder",
    [string]$Service = "resume-builder",
    [string]$PoolId = "github",
    [string]$ProviderId = "github-oidc"
)

function Assert-LastExitCode($message) {
    if ($LASTEXITCODE -ne 0) {
        Write-Error "$message (exit code $LASTEXITCODE)"
        exit 1
    }
}

if ($GitHubRepo -notmatch '^[^/]+/[^/]+$') {
    Write-Error "GitHubRepo must look like 'owner/repo' (got '$GitHubRepo')"
    exit 1
}
$GitHubOwner = $GitHubRepo.Split('/')[0]

# Same PATH repair as deploy.ps1: a terminal opened before the Cloud SDK installer finished
# doesn't always pick gcloud up.
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    $candidate = "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin"
    if (Test-Path "$candidate\gcloud.cmd") {
        $env:Path = "$candidate;$env:Path"
        Write-Host "Added Cloud SDK to PATH for this session: $candidate"
    } else {
        Write-Error "gcloud not found on PATH and not at $candidate. Re-run the Google Cloud SDK installer."
        exit 1
    }
}

$activeAccount = gcloud auth list --filter="status:ACTIVE" --format="value(account)"
if (-not $activeAccount) {
    Write-Host "Not logged in -- opening browser for gcloud auth login..."
    gcloud auth login
    Assert-LastExitCode "gcloud auth login failed"
}

Write-Host ""
Write-Host "=== Project ==="
$projectNumber = gcloud projects describe $ProjectId --format="value(projectNumber)"
Assert-LastExitCode "Could not read project $ProjectId -- does it exist and do you have access?"
$projectNumber = $projectNumber.Trim()
Write-Host "Project $ProjectId has number $projectNumber"

$deployerSa = "github-deployer@$ProjectId.iam.gserviceaccount.com"
$runtimeSa = "resume-builder-runtime@$ProjectId.iam.gserviceaccount.com"

Write-Host ""
Write-Host "=== Enabling APIs ==="
# iamcredentials and sts are the ones people forget; without them Workload Identity Federation
# fails with an opaque 403 long before anything touches Cloud Run.
gcloud services enable `
    iamcredentials.googleapis.com sts.googleapis.com iam.googleapis.com `
    run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com `
    --project $ProjectId
Assert-LastExitCode "Failed to enable APIs -- check that billing is enabled on the project"

Write-Host ""
Write-Host "=== Artifact Registry ==="
# AR does not create repositories implicitly on push: a missing repo surfaces as a 403 from
# `docker push`, not a helpful 404.
gcloud artifacts repositories create $Repo `
    --repository-format=docker --location=$Region --project $ProjectId
if ($LASTEXITCODE -ne 0) { Write-Host "  (repo already exists -- fine)" }

Write-Host ""
Write-Host "=== Service accounts ==="
gcloud iam service-accounts create github-deployer `
    --display-name="GitHub Actions deployer" --project $ProjectId
if ($LASTEXITCODE -ne 0) { Write-Host "  (github-deployer already exists -- fine)" }

gcloud iam service-accounts create resume-builder-runtime `
    --display-name="resume-builder Cloud Run runtime" --project $ProjectId
if ($LASTEXITCODE -ne 0) { Write-Host "  (resume-builder-runtime already exists -- fine)" }

Write-Host ""
Write-Host "=== Workload Identity pool and provider ==="
gcloud iam workload-identity-pools create $PoolId `
    --location=global --display-name="GitHub Actions" --project $ProjectId
if ($LASTEXITCODE -ne 0) { Write-Host "  (pool already exists -- fine)" }

# The attribute condition is the security boundary, not a nicety: without it, ANY repository on
# GitHub could mint a token for this provider. attribute.repository must appear in the mapping
# for both the condition and the principalSet binding below to be able to reference it.
#
# Both values are built into variables first: PowerShell 5.1 mangles quoting when a literal like
# --flag="a == 'b'" is passed straight to a native exe, splitting it into several arguments.
$mapping = "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner,attribute.ref=assertion.ref"
$condition = "assertion.repository == '$GitHubRepo' && assertion.repository_owner == '$GitHubOwner'"

gcloud iam workload-identity-pools providers create-oidc $ProviderId `
    --location=global --workload-identity-pool=$PoolId --project $ProjectId `
    --issuer-uri="https://token.actions.githubusercontent.com" `
    --attribute-mapping=$mapping `
    --attribute-condition=$condition
if ($LASTEXITCODE -ne 0) {
    Write-Host "  (provider already exists -- updating its condition instead)"
    gcloud iam workload-identity-pools providers update-oidc $ProviderId `
        --location=global --workload-identity-pool=$PoolId --project $ProjectId `
        --attribute-mapping=$mapping `
        --attribute-condition=$condition
    Assert-LastExitCode "Could not update the existing OIDC provider"
}

Write-Host ""
Write-Host "=== IAM: let only $GitHubRepo impersonate the deployer ==="
# Scoped to attribute.repository. A pool-wide binding here would undo the attribute condition.
$principalSet = "principalSet://iam.googleapis.com/projects/$projectNumber/locations/global/workloadIdentityPools/$PoolId/attribute.repository/$GitHubRepo"
gcloud iam service-accounts add-iam-policy-binding $deployerSa `
    --project $ProjectId --role="roles/iam.workloadIdentityUser" `
    --member=$principalSet
Assert-LastExitCode "Could not bind workloadIdentityUser on $deployerSa"

Write-Host ""
Write-Host "=== IAM: deployer permissions (three, all narrowly scoped) ==="
# a) Push images -- on the repository, not the project.
gcloud artifacts repositories add-iam-policy-binding $Repo `
    --location=$Region --project $ProjectId `
    --member="serviceAccount:$deployerSa" --role="roles/artifactregistry.writer"
Assert-LastExitCode "Could not grant artifactregistry.writer"

# b) Roll revisions and shift traffic. run.developer, NOT run.admin: admin additionally grants
#    setIamPolicy, i.e. the power to make any service in the project public.
gcloud projects add-iam-policy-binding $ProjectId `
    --member="serviceAccount:$deployerSa" --role="roles/run.developer" --condition=None
Assert-LastExitCode "Could not grant run.developer"

# c) actAs the runtime SA -- required for `gcloud run deploy --service-account`, and scoped to
#    that one identity so the deployer cannot borrow any other.
gcloud iam service-accounts add-iam-policy-binding $runtimeSa `
    --project $ProjectId `
    --member="serviceAccount:$deployerSa" --role="roles/iam.serviceAccountUser"
Assert-LastExitCode "Could not grant serviceAccountUser on $runtimeSa"

Write-Host ""
Write-Host "=== IAM: runtime can read exactly two secrets ==="
# Per-secret, not project-wide. The deployer gets nothing here: it can ship code that reads the
# keys, but can never read them itself.
$secretsMissing = $false
foreach ($secret in @("gemini-api-key", "supabase-service-role-key")) {
    gcloud secrets add-iam-policy-binding $secret --project $ProjectId `
        --member="serviceAccount:$runtimeSa" --role="roles/secretmanager.secretAccessor"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  !! secret '$secret' does not exist yet"
        $secretsMissing = $true
    }
}
if ($secretsMissing) {
    Write-Host ""
    Write-Host "  Create the missing secrets by running ./deploy.ps1 once (it reads them from"
    Write-Host "  .env and writes them to Secret Manager), then re-run this script."
}

Write-Host ""
Write-Host "=== Public access to the Cloud Run service ==="
# deploy.yml intentionally omits --allow-unauthenticated, because that flag calls
# run.services.setIamPolicy which roles/run.developer does not grant. This binding lives on the
# service and survives every future revision, tag URLs included.
gcloud run services describe $Service --region=$Region --project $ProjectId --format="value(status.url)" 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    gcloud run services add-iam-policy-binding $Service `
        --region=$Region --project $ProjectId `
        --member="allUsers" --role="roles/run.invoker"
    Assert-LastExitCode "Could not grant allUsers invoker on $Service"
} else {
    Write-Host "  Service '$Service' does not exist yet. After the first successful deploy, run:"
    Write-Host "    gcloud run services add-iam-policy-binding $Service --region=$Region --project $ProjectId --member=allUsers --role=roles/run.invoker"
}

Write-Host ""
Write-Host "=== Artifact Registry cleanup policy (keeps you inside the 0.5 GB free tier) ==="
if (Test-Path "ar-cleanup-policy.json") {
    Write-Host "Dry run -- this only reports what WOULD be deleted:"
    gcloud artifacts repositories set-cleanup-policies $Repo `
        --location=$Region --project $ProjectId `
        --policy=ar-cleanup-policy.json --dry-run
    if ($LASTEXITCODE -eq 0) {
        Write-Host ""
        Write-Host "  Read the output above. If it looks right, arm the policy with:"
        Write-Host "    gcloud artifacts repositories set-cleanup-policies $Repo --location=$Region --project $ProjectId --policy=ar-cleanup-policy.json --no-dry-run"
    }
} else {
    Write-Host "  ar-cleanup-policy.json not found -- skipping."
}

$wifProvider = "projects/$projectNumber/locations/global/workloadIdentityPools/$PoolId/providers/$ProviderId"

Write-Host ""
Write-Host "============================================================"
Write-Host " GCP setup complete. Now configure GitHub."
Write-Host "============================================================"
Write-Host ""
Write-Host "Repository VARIABLES (plaintext, visible in public logs):"
Write-Host "  gh variable set WIF_PROVIDER --body `"$wifProvider`""
Write-Host "  gh variable set APPROVERS --body `"$GitHubOwner`""
Write-Host "  gh variable set GEMINI_MODEL --body `"gemini-3.5-flash`""
Write-Host "  gh variable set DAILY_GENERATION_LIMIT --body `"5`""
Write-Host "  gh variable set PHOTO_BUCKET --body `"photo`""
Write-Host "  gh variable set PDF_BUCKET --body `"resume-pdf`""
Write-Host ""
Write-Host "Repository SECRETS (masked in logs; each prompts, nothing is echoed):"
Write-Host "  gh secret set SMTP_USERNAME            # your Gmail address"
Write-Host "  gh secret set SMTP_PASSWORD            # 16-char Gmail app password, spaces stripped"
Write-Host "  gh secret set NOTIFY_EMAIL             # where reports go"
Write-Host "  gh secret set SUPABASE_URL             # https://<ref>.supabase.co"
Write-Host "  gh secret set SUPABASE_ANON_KEY        # the anon JWT"
Write-Host ""
Write-Host "GEMINI_API_KEY and SUPABASE_SERVICE_ROLE_KEY are deliberately NOT stored in GitHub."
Write-Host "They live only in Secret Manager and are injected by Cloud Run at instance start."
Write-Host ""
Write-Host "Remaining manual steps are listed in docs/ci-cd-setup.md."
