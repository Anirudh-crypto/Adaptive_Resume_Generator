# Just run: ./deploy.ps1
# It finds gcloud, logs you in via browser if needed, sets the project, and deploys.
#
# Prerequisites: a GCP project (set PROJECT_ID below) with billing enabled (Cloud Run's
# usage-based free tier still requires a billing account attached, even if you stay within the
# free allowance), and a .env file with GEMINI_API_KEY / SUPABASE_URL / SUPABASE_ANON_KEY /
# SUPABASE_SERVICE_ROLE_KEY set.
#
# Note: deliberately NOT using $ErrorActionPreference = "Stop" -- gcloud (a native exe) prints
# routine warnings/info to stderr, and PowerShell 5.1 treats any stderr from a native command as
# a terminating error under that preference, even on success. Real failures are caught below via
# explicit $LASTEXITCODE checks instead.

function Assert-LastExitCode($message) {
    if ($LASTEXITCODE -ne 0) {
        Write-Error "$message (exit code $LASTEXITCODE)"
        exit 1
    }
}

# Piping a PowerShell string straight to a native command's stdin (e.g. `$value | gcloud ...
# --data-file=-`) goes through $OutputEncoding, which on Windows PowerShell 5.1 can silently
# prepend a UTF-8 BOM byte to the data -- this corrupted the Supabase secrets on an earlier
# deploy (showed up as a Python UnicodeEncodeError on '﻿' at position 0, deep in the
# postgrest client). Writing to a real file with an explicit no-BOM UTF8Encoding avoids it.
function Write-SecretFile($path, $value) {
    [System.IO.File]::WriteAllText($path, $value, (New-Object System.Text.UTF8Encoding($false)))
}

# Make sure gcloud is reachable even if this terminal was opened before the
# Cloud SDK installer finished updating PATH (very common on Windows -- a
# fresh terminal doesn't always pick it up right away).
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    $candidate = "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin"
    if (Test-Path "$candidate\gcloud.cmd") {
        $env:Path = "$candidate;$env:Path"
        Write-Host "Added Cloud SDK to PATH for this session: $candidate"
    } else {
        Write-Error "gcloud not found on PATH and not at the expected install location ($candidate). Re-run the Google Cloud SDK installer, or find gcloud.cmd manually and adjust this script."
        exit 1
    }
}

$PROJECT_ID = "resume-builder-503517"
$REGION = "us-central1"
$REPO = "resume-builder"
$SERVICE = "resume-builder"

$activeAccount = gcloud auth list --filter="status:ACTIVE" --format="value(account)"
if (-not $activeAccount) {
    Write-Host "Not logged in -- opening browser for gcloud auth login..."
    gcloud auth login
    Assert-LastExitCode "gcloud auth login failed"
}

gcloud config set project $PROJECT_ID
Assert-LastExitCode "Could not set project to $PROJECT_ID -- does it exist and do you have access?"

# Read secrets from .env so you don't have to paste them twice
$envVars = @{}
Get-Content .env | ForEach-Object {
    if ($_ -match '^([A-Z_]+)=(.*)$') { $envVars[$matches[1]] = $matches[2] }
}

Write-Host "Enabling required APIs..."
gcloud services enable run.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com --project $PROJECT_ID
Assert-LastExitCode "Failed to enable required APIs -- check billing is enabled on the project"

Write-Host "Creating Artifact Registry repo (ok if it already exists)..."
gcloud artifacts repositories create $REPO --repository-format=docker --location=$REGION --project $PROJECT_ID
# Non-fatal: it's fine if this fails because the repo already exists from a previous run.

Write-Host "Creating/updating secrets..."
$geminiKeyFile = [System.IO.Path]::GetTempFileName()
$supabaseKeyFile = [System.IO.Path]::GetTempFileName()
try {
    Write-SecretFile $geminiKeyFile $envVars["GEMINI_API_KEY"]
    gcloud secrets create gemini-api-key --data-file=$geminiKeyFile --project $PROJECT_ID
    if ($LASTEXITCODE -ne 0) {
        gcloud secrets versions add gemini-api-key --data-file=$geminiKeyFile --project $PROJECT_ID
        Assert-LastExitCode "Failed to create or update the gemini-api-key secret"
    }

    Write-SecretFile $supabaseKeyFile $envVars["SUPABASE_SERVICE_ROLE_KEY"]
    gcloud secrets create supabase-service-role-key --data-file=$supabaseKeyFile --project $PROJECT_ID
    if ($LASTEXITCODE -ne 0) {
        gcloud secrets versions add supabase-service-role-key --data-file=$supabaseKeyFile --project $PROJECT_ID
        Assert-LastExitCode "Failed to create or update the supabase-service-role-key secret"
    }
} finally {
    Remove-Item $geminiKeyFile, $supabaseKeyFile -ErrorAction SilentlyContinue
}

Write-Host "Building and pushing the image (this is the slow step, usually a few minutes)..."
$IMAGE = "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/app:latest"
gcloud builds submit --tag $IMAGE --project $PROJECT_ID .
Assert-LastExitCode "Cloud Build failed -- scroll up for the build log"

Write-Host "Deploying to Cloud Run..."
gcloud run deploy $SERVICE `
  --image $IMAGE `
  --region $REGION --project $PROJECT_ID `
  --allow-unauthenticated `
  --no-cpu-throttling `
  --min-instances=0 --max-instances=2 --memory=512Mi --cpu=1 `
  --set-env-vars "SUPABASE_URL=$($envVars['SUPABASE_URL']),SUPABASE_ANON_KEY=$($envVars['SUPABASE_ANON_KEY']),GEMINI_MODEL=$($envVars['GEMINI_MODEL']),DAILY_GENERATION_LIMIT=$($envVars['DAILY_GENERATION_LIMIT']),PHOTO_BUCKET=$($envVars['PHOTO_BUCKET']),PDF_BUCKET=$($envVars['PDF_BUCKET'])" `
  --set-secrets "GEMINI_API_KEY=gemini-api-key:latest,SUPABASE_SERVICE_ROLE_KEY=supabase-service-role-key:latest"
Assert-LastExitCode "Cloud Run deploy failed"

Write-Host ""
Write-Host "Deployed. IMPORTANT: copy the Service URL printed above, then add it as an allowed"
Write-Host "redirect URL in Supabase -> Authentication -> Providers -> Google."
