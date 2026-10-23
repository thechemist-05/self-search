<#
.SYNOPSIS
  Start the Self Search demo on the final catalogue: API (default port 8000) and UI (default 8502).

.DESCRIPTION
  Default catalogue (2026-10-23): the expanded catalogue fashion-v4-20261023T014937Z, 86,588 products
  and 531,796 reviews (the 50,000-product v3 catalogue plus sarees, lehengas, earrings, footwear,
  bags, jewellery, men's fashion, beauty, home and sports from Amazon Reviews 2023), with its own
  manifest-checked FAISS indexes and validated product images. The previous catalogues are kept
  unchanged and can be served instead with -Catalogue fashion-v3-20261022T085353Z (50,000) or
  -Catalogue fashion-v2-20261021T094306Z (20,000). Every price is shown in INR (documented
  exchange rate, config/exchange_rates.toml). The small production database
  (data/processed/shelfsense.db) is not used or changed.

  Synthetic INR price estimates are enabled for INR budgets (they are labelled, never sent to
  the model and never used as evidence); pass -NoEstimates to turn them off.

  Product images are shown: the UI server fetches each product's own image from Amazon's
  image host (URLs recovered from the verified source metadata into product_images.json).
  This needs an internet connection; offline, cards show a neutral placeholder. Pass
  -NoImages to never fetch remote images (fully offline).

  The index manifest is verified first; the script stops if it does not match the database.
  Stop any API or UI already using these ports before running it.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1 -Catalogue fashion-v3-20261022T085353Z
#>
param(
    [string]$Catalogue = "fashion-v4-20261023T014937Z",
    [int]$ApiPort = 8000,
    [int]$UiPort = 8502,
    [switch]$NoEstimates,
    [switch]$NoImages
)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$run = Join-Path $root (Join-Path "data\experiments" $Catalogue)
if (-not (Test-Path (Join-Path $run "shelfsense.db"))) { throw "No catalogue database at $run" }
$py = Join-Path $root "venv\Scripts\python.exe"
$logs = Join-Path $env:TEMP "shelfsense-demo"
New-Item -ItemType Directory -Force $logs | Out-Null

foreach ($port in @($ApiPort, $UiPort)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use. Stop the process listening on it first."
    }
}

$env:SHELFSENSE_DB_PATH = Join-Path $run "shelfsense.db"
$env:SHELFSENSE_VECTOR_STORE_DIR = Join-Path $run "vector_store"
$env:SHELFSENSE_ESTIMATED_PRICES_ENABLED = $(if ($NoEstimates) { "false" } else { "true" })
$env:SHELFSENSE_API_URL = "http://127.0.0.1:$ApiPort"
$env:SHELFSENSE_LOAD_REMOTE_IMAGES = $(if ($NoImages) { "false" } else { "true" })
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"

Push-Location $root
try {
    & $py scripts\verify_index_manifest.py --check | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Index manifest does not match the database; not starting." }

    Start-Process -FilePath $py -WorkingDirectory $root -WindowStyle Hidden `
        -ArgumentList "-m", "uvicorn", "src.api.app:app", "--host", "127.0.0.1", "--port", "$ApiPort" `
        -RedirectStandardOutput (Join-Path $logs "api.out.log") -RedirectStandardError (Join-Path $logs "api.log")
    Start-Process -FilePath $py -WorkingDirectory $root -WindowStyle Hidden `
        -ArgumentList "-m", "streamlit", "run", "frontend/app.py", "--server.port", "$UiPort", "--server.headless", "true" `
        -RedirectStandardOutput (Join-Path $logs "ui.log") -RedirectStandardError (Join-Path $logs "ui.err.log")
}
finally {
    Pop-Location
}

Write-Host "Self Search demo"
Write-Host "Catalogue: $Catalogue"
Write-Host "API: http://127.0.0.1:$ApiPort/health    UI: http://127.0.0.1:$UiPort    logs: $logs"
