# Append GOOGLE_SERVICE_ACCOUNT_JSON to env.yaml, correctly.
#
#   .\add-service-account.ps1                       read it from Secret Manager
#   .\add-service-account.ps1 -File .\sa.json       read it from a file
#
# Why a script instead of pasting the value in by hand:
#
# * The key is one long line whose `private_key` field contains literal \n
#   escapes. A single mangled character means Sheets fails - and it fails
#   SILENTLY, because the app treats the spreadsheet as a convenience that must
#   never take the bot down. You would find out days later from an empty sheet.
#
# * The value copied out of Render is double-encoded: a JSON *string* whose
#   content is JSON, wrapped in its own quotes. `config.py` calls json.loads
#   once, so that yields a string rather than a dict and the credentials never
#   load. This unwraps it if needed.
#
# * YAML folds newlines inside quoted scalars, so the JSON has to be compacted
#   onto one line before it can be embedded at all.

param(
  [string]$File,
  [string]$Secret = "GOOGLE_SERVICE_ACCOUNT_JSON",
  [string]$EnvFile = "env.yaml"
)

$ErrorActionPreference = "Stop"

if ($File) {
  Write-Host "Reading from $File ..." -ForegroundColor Cyan
  $raw = [System.IO.File]::ReadAllText((Resolve-Path $File))
} else {
  Write-Host "Reading from Secret Manager: $Secret ..." -ForegroundColor Cyan
  $raw = (gcloud secrets versions access latest --secret=$Secret | Out-String)
}

$raw = $raw.Trim()
if (-not $raw) { throw "empty value - nothing to add" }

# Unwrap the double encoding if that is what we were given. A value starting
# with a quote is a JSON string, not a JSON object.
if ($raw.StartsWith('"')) {
  Write-Host "Value was double-encoded; unwrapping one layer." -ForegroundColor Yellow
  $raw = $raw | ConvertFrom-Json
}

# Round-trip through an object: this validates the JSON and compacts it onto a
# single line with the \n escapes intact, which is what the app expects and
# what YAML can hold.
try {
  $obj = $raw | ConvertFrom-Json
} catch {
  throw "not valid JSON after unwrapping: $($_.Exception.Message)"
}

if (-not $obj.private_key) { throw "no private_key field - wrong file?" }
if (-not $obj.client_email) { throw "no client_email field - wrong file?" }

# Validate the key actually decodes BEFORE writing it anywhere.
#
# This exists because a key arrived here one base64 character short - lost
# somewhere between Render's dashboard and a clipboard - and nothing noticed.
# It loaded, it reported enabled, and it failed on the first real write with
# "Incorrect padding", hours later and a long way from the cause.
#
# Do NOT "clean up" the PEM by trimming whitespace here. A stray space inside
# the key is evidence of a lost character, not something to tidy away.
$body = ($obj.private_key -split "`n" |
  Where-Object { $_ -notmatch 'PRIVATE KEY' -and $_.Trim() } |
  ForEach-Object { $_.Trim() }) -join ""

if ($body.Length % 4 -ne 0) {
  throw ("private_key is corrupt: $($body.Length) base64 characters, which is " +
         "not a multiple of 4 (remainder $($body.Length % 4)). Characters were " +
         "lost in transit - this cannot be repaired. Download a fresh JSON key " +
         "from the Cloud console and pass it with -File.")
}
try {
  $der = [Convert]::FromBase64String($body)
} catch {
  throw "private_key is not valid base64: $($_.Exception.Message). Download a fresh JSON key."
}
if ($der.Length -lt 600) {
  throw "private_key decoded to only $($der.Length) bytes - too short to be an RSA key."
}

$compact = $obj | ConvertTo-Json -Compress -Depth 10

# Single-quoted YAML: no escape processing, so the \n sequences survive as the
# two characters the app needs. Any literal quote must be doubled.
$line = "GOOGLE_SERVICE_ACCOUNT_JSON: '" + $compact.Replace("'", "''") + "'"

$existing = Get-Content $EnvFile -Raw
if ($existing -match "(?m)^GOOGLE_SERVICE_ACCOUNT_JSON:") {
  throw "$EnvFile already has GOOGLE_SERVICE_ACCOUNT_JSON - remove that line first"
}

Add-Content -Path $EnvFile -Value $line -Encoding utf8

Write-Host ""
Write-Host "Added to $EnvFile" -ForegroundColor Green
Write-Host "  service account : $($obj.client_email)"
Write-Host "  project         : $($obj.project_id)"
Write-Host "  private key     : $($body.Length) base64 chars -> $($der.Length) bytes, decodes OK"
Write-Host ""
Write-Host "Confirm the spreadsheet is shared with that address, or Sheets will"
Write-Host "authenticate fine and then fail on every write."
